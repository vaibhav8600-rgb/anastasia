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

Declarations are derived from TOOL_REGISTRY ∩ (SAFE_TOOLS | CONFIRM_TOOLS):
a tool the local policy would block can never be declared, and an undeclared
tool the model hallucinates anyway still hits the same validator.
"""

import threading

from app.agent.devlog import devlog
from app.agent.safety import (BLOCKED_TOOLS, CONFIRM_TOOLS, SAFE_TOOLS,
                              validate_action)
from app.llm.intent_parser import ActionPlan
from app.llm.providers import DataClass, cloud_allowed

# Destructive stub in the MVP — pointless and provocative to advertise.
NEVER_DECLARE = {"delete_files"}

# These tools' result MESSAGE contains clipboard text; a tool response goes
# to Google, so they follow the 8C clipboard opt-in (gated at declaration
# time AND again at call time in case the model calls them undeclared).
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
    """Tools declared to the Live session, in SDK dict form. Triple-gated:
    a name must be in the registry AND the local safety whitelist AND have a
    schema here. BLOCKED_TOOLS can never appear (they're outside the
    whitelist; the explicit subtraction documents the intent)."""
    from app.tools import TOOL_REGISTRY, _load_all
    _load_all()
    allowed = (SAFE_TOOLS | CONFIRM_TOOLS) - BLOCKED_TOOLS - NEVER_DECLARE
    clip_ok, _ = cloud_allowed({DataClass.CLIPBOARD}, config)
    if not clip_ok:
        allowed -= CLIPBOARD_EXPORTING
    decls = [{"name": name, **_DECLARATIONS[name]}
             for name in sorted(allowed & set(TOOL_REGISTRY) & set(_DECLARATIONS))]
    return [{"function_declarations": decls}] if decls else []


class LiveToolBridge:
    """Receives on_tool_call from a GeminiLiveSession and answers via
    session.send_tool_response — with the local validator in between."""

    def __init__(self, config, agent, history, *, respond=None,
                 ask_confirmation=None, run_async: bool = True):
        self.config = config
        self.agent = agent            # router.Agent — execute() is the ONLY executor
        self.history = history
        self.respond = respond        # callable(call_id, name, result_dict)
        # callable(plan, safety) -> bool. None (nothing wired) = deny — a
        # missing confirmation UI must fail closed, never open.
        self.ask_confirmation = ask_confirmation
        self.run_async = run_async
        self._lock = threading.Lock()  # one live tool call at a time

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

    def _process(self, name, args, call_id) -> None:
        with self._lock:
            try:
                self._validate_and_run(name, args, call_id)
            except Exception as e:
                devlog.exception(e, context="live_tool")
                self._send(call_id, name, False,
                           "The tool failed locally. Nothing was executed.")

    def _validate_and_run(self, name, args, call_id) -> None:
        plan = ActionPlan(intent=name, tool_name=name,
                          arguments=args if isinstance(args, dict) else {})
        transcript = f"[gemini_live] {plan.tool_name}"

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
