"""Gemini Live tool calling — through the LOCAL safety validator (Phase 10B).

The cloud model never executes anything. It may only *request* a tool by
name; every request lands here, where:

  1. the request becomes a plain ActionPlan (same shape as local plans),
  2. app/agent/safety.py validates it — the model's own risk framing was
     never even transmitted: a run_terminal call that "forgets" to ask for
     confirmation is still escalated by the local policy,
  3. anything requiring confirmation goes to the user (no hook wired = deny),
  4. execution happens only via the whitelisted registry executor,
  5. the model gets back {"success", "message"} — NEVER ToolResult.data,
     so screenshots, file contents and clipboard payloads physically cannot
     ride a tool response to the cloud.

The *eligible set* is owned by the tool registry, not by this file. Phase 0's
`app.tools.cloud_manifest(config)` is the single authority on what may be named
to a cloud model: it drops `Tier.BLOCKED`, drops `cloud_declarable=False`
(delete_files), and — when the clipboard opt-in is off — drops the
clipboard-exporting tools. This module only supplies the engine-specific schema
prose and intersects it with that manifest, so a tool the registry forbids can
never be declared here no matter what is added to `_DECLARATIONS`. An undeclared
tool the model hallucinates anyway still hits the same validator.
"""

import json
import threading

from app.agent.devlog import devlog
from app.agent.safety import validate_action
from app.llm.intent_parser import ActionPlan
from app.llm.providers import DataClass, cloud_allowed

# Destructive stub in the MVP — pointless and provocative to advertise. The
# registry now ENFORCES this (delete_files is cloud_declarable=False); this
# constant remains as the human-readable statement of intent, and a test pins
# the two together so they cannot drift.
NEVER_DECLARE = {"delete_files"}

# These tools' result MESSAGE contains clipboard text; a tool response goes to
# Google, so they follow the 8C clipboard opt-in — gated at declaration time (by
# the registry, via exports_clipboard) AND again at call time here, in case the
# model calls one undeclared. A test pins this set to the registry's.
CLIPBOARD_EXPORTING = {"clipboard_read", "summarize_clipboard"}


def _params(props: dict, required: list) -> dict:
    return {"type": "OBJECT",
            "properties": {key: {"type": typ, "description": desc}
                           for key, (typ, desc) in props.items()},
            "required": required}


# Argument names match what each tool actually reads (app/tools/*.py).
_DECLARATIONS = {
    "open_app": {
        "description": "Open an application on the user's Windows PC.",
        "parameters": _params(
            {"app_name": ("STRING", "Application name, e.g. 'notepad' or 'chrome'.")},
            ["app_name"]),
    },
    "open_folder": {
        "description": "Open a folder in File Explorer (the user's safe folders only).",
        "parameters": _params(
            {"folder": ("STRING", "Folder name or path, e.g. 'downloads'.")},
            ["folder"]),
    },
    "search_files": {
        "description": "Search for files by name inside the user's safe folders.",
        "parameters": _params(
            {"query": ("STRING", "The file name, or part of it."),
             "folder": ("STRING", "Optional folder to search in, e.g. 'documents'.")},
            ["query"]),
    },
    "take_screenshot": {
        "description": ("Take a screenshot. It is saved locally on the PC; "
                        "the image itself is never shared with you."),
        "parameters": _params(
            {"screen": ("INTEGER", "Monitor number (1 = leftmost). Omit to capture all screens.")},
            []),
    },
    "type_text": {
        "description": "Type text into the currently focused window.",
        "parameters": _params({"text": ("STRING", "The exact text to type.")}, ["text"]),
    },
    "press_hotkey": {
        "description": ("Press a keyboard shortcut from the allowed list "
                        "(e.g. ctrl+c, ctrl+v, ctrl+s, alt+tab)."),
        "parameters": _params({"keys": ("STRING", "Shortcut like 'ctrl+c'.")}, ["keys"]),
    },
    "browser_open": {
        "description": "Open a URL or a web search in the user's browser.",
        "parameters": _params(
            {"url": ("STRING", "Full URL to open."),
             "query": ("STRING", "Or a web search query instead of a URL.")},
            []),
    },
    "clipboard_write": {
        "description": "Put text on the user's clipboard.",
        "parameters": _params({"text": ("STRING", "Text to copy.")}, ["text"]),
    },
    "clipboard_read": {
        "description": "Read the user's clipboard text (shared only because the user opted in).",
        "parameters": _params({}, []),
    },
    "summarize_clipboard": {
        "description": "Summarize the user's clipboard text and return the summary.",
        "parameters": _params({}, []),
    },
    "run_terminal": {
        "description": ("Run a shell command on the user's PC. This ALWAYS "
                        "requires the user's on-screen confirmation first — "
                        "tell them you're waiting for their approval."),
        "parameters": _params(
            {"command": ("STRING", "The command to run."),
             "cwd": ("STRING", "Optional working folder.")},
            ["command"]),
    },
    "window_control": {
        "description": ("Minimize, maximize, focus or close an app's window. "
                        "Requires the user's on-screen confirmation."),
        "parameters": _params(
            {"action": ("STRING", "'minimize', 'maximize', 'focus' or 'close'."),
             "app": ("STRING", "Which app's window, e.g. 'chrome'.")},
            ["action", "app"]),
    },
}


def live_tool_declarations(config) -> list:
    """Tools declared to the Live session, in Gemini SDK dict form.

    The eligible SET is the registry's cloud manifest — the single authority on
    what may reach a cloud model. `cloud_manifest(config)` has already removed
    every blocked tool, every `cloud_declarable=False` tool (delete_files), and,
    when the clipboard opt-in is off, the clipboard-exporting tools. This
    function only supplies the per-engine schema prose and keeps the
    intersection with what it can describe. A blocked tool cannot appear here
    even if someone adds a schema for it to `_DECLARATIONS`, because it was never
    in the manifest.
    """
    from app.tools import cloud_manifest
    eligible = {spec.name for spec in cloud_manifest(config)}
    decls = [{"name": name, **_DECLARATIONS[name]}
             for name in sorted(eligible & set(_DECLARATIONS))]
    return [{"function_declarations": decls}] if decls else []


class LiveToolBridge:
    """Receives on_tool_call from a GeminiLiveSession and answers via
    session.send_tool_response — with the local validator in between."""

    def __init__(self, config, agent, history, *, respond=None,
                 ask_confirmation=None, skip_check=None, run_async: bool = True):
        self.config = config
        self.agent = agent            # router.Agent — execute() is the ONLY executor
        self.history = history
        self.respond = respond        # callable(call_id, name, result_dict)
        # callable(plan, safety) -> bool. None (nothing wired) = deny — a
        # missing confirmation UI must fail closed, never open.
        self.ask_confirmation = ask_confirmation
        # callable(name, args) -> message|None. Non-None = the action was
        # already performed locally (rule short-circuit, 10C) — answer the
        # model without executing again.
        self.skip_check = skip_check
        self.run_async = run_async
        # Signatures of tool calls currently being processed. A slow
        # confirmation makes the model retry the SAME call (it hasn't had a
        # tool_response yet); without dedup that retry becomes a second
        # confirmation card that strands after the first is approved.
        self._inflight = set()
        self._inflight_lock = threading.Lock()

    def attach_session(self, session) -> None:
        self.respond = session.send_tool_response

    def handle_tool_call(self, name: str, args: dict, call_id: str) -> None:
        """Session callback. Never blocks the session's receive loop — a
        confirmation can take 30s, so the work runs on a worker thread."""
        if self.run_async:
            threading.Thread(target=self._process, args=(name, args, call_id),
                             daemon=True, name="anna-live-tool").start()
        else:
            self._process(name, args, call_id)

    @staticmethod
    def _signature(name, args) -> tuple:
        try:
            return (name, json.dumps(args or {}, sort_keys=True, default=str))
        except Exception:
            return (name, str(args))

    def _process(self, name, args, call_id) -> None:
        sig = self._signature(name, args)
        with self._inflight_lock:
            duplicate = sig in self._inflight
            if not duplicate:
                self._inflight.add(sig)
        if duplicate:
            # Same call already mid-flight (awaiting confirmation or running).
            # Tell the model to wait instead of spawning a second card.
            devlog.log(f"[gemini_live] tool_call {name} deduped "
                       "(identical call already in flight)")
            self._send(call_id, name, True,
                       "I'm already handling that exact request — hold on, "
                       "I haven't finished it yet. Don't repeat it.")
            return
        try:
            self._validate_and_run(name, args, call_id)
        except Exception as e:
            devlog.exception(e, context="live_tool")
            self._send(call_id, name, False,
                       "The tool failed locally. Nothing was executed.")
        finally:
            with self._inflight_lock:
                self._inflight.discard(sig)

    def _validate_and_run(self, name, args, call_id) -> None:
        plan = ActionPlan(intent=name, tool_name=name,
                          arguments=args if isinstance(args, dict) else {})
        transcript = f"[gemini_live] {plan.tool_name}"

        # Rule short-circuit dedup (10C): the local router already did this
        # an instant ago — nothing executes, the model just gets told.
        if self.skip_check is not None:
            try:
                skip_msg = self.skip_check(plan.tool_name, plan.arguments)
            except Exception:
                skip_msg = None
            if skip_msg:
                devlog.log(f"[gemini_live] tool_call {plan.tool_name} deduped "
                           "(already handled by the local rule router)")
                self._send(call_id, name, True, str(skip_msg))
                return

        # LOCAL safety validation — every Live tool call, no exceptions.
        safety = validate_action(plan, self.config)
        devlog.log(f"[gemini_live] tool_call {plan.tool_name} args={plan.arguments}"
                   f" -> allowed={safety.allowed}"
                   f" confirm={safety.requires_confirmation} risk={safety.risk_level}")
        if not safety.allowed:
            self._log(transcript, plan, safety, executed=False, error=safety.reason)
            self._send(call_id, name, False,
                       f"Blocked by Anna's local safety policy: {safety.reason} "
                       "Nothing was executed.")
            return

        # 8C clipboard gate: these results carry clipboard text back to the
        # cloud model, so the cloud-brain opt-in governs them too.
        if plan.tool_name in CLIPBOARD_EXPORTING:
            clip_ok, why = cloud_allowed({DataClass.CLIPBOARD}, self.config)
            if not clip_ok:
                self._log(transcript, plan, safety, executed=False, error=why)
                self._send(call_id, name, False,
                           "The clipboard stays on this PC unless the user turns on "
                           "'clipboard to cloud' in Settings. Nothing was shared.")
                return

        if safety.requires_confirmation:
            approved = False
            if self.ask_confirmation is not None:
                try:
                    approved = bool(self.ask_confirmation(plan, safety))
                except Exception as e:
                    devlog.exception(e, context="live_tool_confirm")
            if not approved:
                self._log(transcript, plan, safety, executed=False,
                          result="cancelled (live tool call not approved)")
                self._send(call_id, name, False,
                           "The user did not approve this action, so it was not "
                           "executed. Don't retry unless they ask again.")
                return

        result = self.agent.execute(plan)   # whitelisted registry executor ONLY
        self._log(transcript, plan, safety, executed=result.success,
                  result=result.message if result.success else "",
                  error="" if result.success else result.message)
        self._send(call_id, name, bool(result.success), str(result.message or ""))

    def _send(self, call_id, name, ok: bool, message: str) -> None:
        # success + message ONLY. ToolResult.data (screenshot thumbnails,
        # file contents, full clipboard text) NEVER rides a tool response
        # to the cloud.
        if self.respond is not None:
            try:
                self.respond(call_id, name, {"success": ok, "message": message})
            except Exception as e:
                devlog.exception(e, context="live_tool_respond")

    def _log(self, *args, **kwargs) -> None:
        """Best-effort history row; the transcript prefix carries the
        source (the interactions schema has no source column)."""
        try:
            self.history.log(*args, **kwargs)
        except Exception as e:
            devlog.warn(f"live tool history skipped: {' '.join(str(e).split())[:150]}")
