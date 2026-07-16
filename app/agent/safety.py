"""Safety validator — every action plan passes through here before execution.

Policy summary (MVP):
  * Unknown tools            -> blocked
  * Dangerous terminal cmds  -> blocked
  * run_terminal             -> always requires confirmation
  * window_control           -> always requires confirmation
  * delete_files             -> confirmation shown, but executor is a stub
                                (destructive delete is NOT enabled in the MVP)
  * shutdown/email/etc.      -> blocked outright
  * type_text > N chars      -> requires confirmation
  * folders outside safe_folders -> blocked
  * The LLM's own requires_confirmation flag is honored (never downgraded).
"""

import re
from pathlib import Path

from pydantic import BaseModel


class SafetyResult(BaseModel):
    allowed: bool
    requires_confirmation: bool
    risk_level: str  # low | medium | high | blocked
    reason: str = ""
    # 11C: set when the resolved click target's name is on the destructive
    # list, or when the target was only a vision guess. Both make 11A demand
    # the strong approval phrase.
    destructive_target: bool = False
    confidence: float = 1.0
    target: dict = None


# Tools that may run without confirmation (unless rules below escalate them).
SAFE_TOOLS = {
    "open_app", "open_folder", "take_screenshot", "search_files",
    "clipboard_read", "clipboard_write", "summarize_clipboard",
    "type_text", "press_hotkey", "browser_open", "speak",
    "ask_clarification", "no_action",
    # Vision (11B): capture is user-triggered and never persists a frame.
    # Overriding the sensitive-content refusal is escalated below.
    "look_at_screen", "screen_capture", "active_window_capture",
    "region_capture", "camera_look",
    "start_screen_watch", "stop_screen_watch", "privacy_mode",
    # App control (11C). Read-only lookups are safe; clicking and typing are
    # gated below by the resolved target, not by the caller's say-so.
    "find_control", "read_window_text", "browser_read_page_text",
    "browser_get_visible_links", "browser_navigate",
    "click_control", "type_into_control",
    "browser_find_and_click", "browser_type_into",
    # Email/messaging (11E). Opening a DRAFT is safe (nothing is sent); the
    # actual send is gated in its own branch (recipient check + confirmation).
    "compose_email", "read_window_text",
}

# Vision tools that will analyze a frame — overriding the sensitive-content
# refusal on these is what needs the user's explicit OK.
VISION_LOOK_TOOLS = {"look_at_screen", "screen_capture",
                     "active_window_capture", "region_capture", "camera_look"}

# ---- 11C: app control -----------------------------------------------------
CLICK_TOOLS = {"click_control", "browser_find_and_click"}
TYPE_TOOLS = {"type_into_control", "browser_type_into"}
CONTROL_TOOLS = CLICK_TOOLS | TYPE_TOOLS

# HARDCODED here, in the validator — not in the planner. Any resolved target
# whose accessible name contains one of these forces a confirmation at high
# risk, no matter what the plan (or a cloud model's tool call) claimed. A
# misfiring planner therefore cannot click "Send" without asking.
DESTRUCTIVE_TARGETS = ("send", "submit", "pay", "delete", "confirm", "install",
                       "post", "purchase", "transfer", "approve")

# 11E / principle 9: actual money movement is BLOCKED, not merely confirmed.
# A bare "Pay" button is a destructive-target (confirm); an explicit payment
# phrase — "Pay $500", "confirm payment", "transfer funds", "place order" —
# is refused outright. Reading a banking page is fine; MOVING money is not.
PAYMENT_BLOCK_PATTERNS = (
    r"\bpay\s*(?:now|bill|\$|₹|€|£|\d)",
    r"\bconfirm\s+(?:payment|purchase|order|transfer)",
    r"\bcomplete\s+(?:payment|purchase|order|checkout)",
    r"\bplace\s+(?:the\s+)?order\b",
    r"\bproceed\s+to\s+(?:pay|checkout|payment)",
    r"\b(?:transfer|send|wire)\s+(?:money|funds|\$|₹|€|£|\d)",
    r"\bmake\s+(?:a\s+)?payment\b",
    r"\bbuy\s+now\b", r"\bcheckout\b", r"\bpay\s+and\b",
)


def is_payment_action(text: str) -> bool:
    if not text:
        return False
    lowered = str(text).lower()
    return any(re.search(pat, lowered) for pat in PAYMENT_BLOCK_PATTERNS)


def destructive_targets(config) -> tuple:
    """The list is configurable, but never empty-able below the hardcoded set
    unless the user deliberately replaces it in config."""
    configured = getattr(config, "destructive_targets", None)
    if isinstance(configured, (list, tuple)) and configured:
        return tuple(str(word).lower() for word in configured)
    return DESTRUCTIVE_TARGETS


def is_destructive_target(text: str, config=None) -> bool:
    """Whole-word, case-insensitive: "Send" and "Send message" match,
    "Sender" and "Resend" do not."""
    if not text:
        return False
    words = destructive_targets(config) if config is not None else DESTRUCTIVE_TARGETS
    lowered = str(text).lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


# Anna's own resolver stamps the target. Injectable for tests; a model can
# never supply one (see _resolve_target, which strips model-supplied values).
_TARGET_RESOLVER = None
_DEFAULT_RESOLVER = None


def set_target_resolver(resolver) -> None:
    """resolver(plan, config) -> ResolvedTarget | None"""
    global _TARGET_RESOLVER
    _TARGET_RESOLVER = resolver


def _default_resolve(plan, config):
    global _DEFAULT_RESOLVER
    from app.control.resolver import TargetResolver
    if _DEFAULT_RESOLVER is None:
        _DEFAULT_RESOLVER = TargetResolver(config)
    args = plan.arguments or {}
    hint = str(args.get("hint") or args.get("target") or args.get("text")
               or args.get("field") or "")
    scope = _DEFAULT_RESOLVER.current_scope(app=str(args.get("app") or ""))
    return _DEFAULT_RESOLVER.resolve(hint, scope)


def _resolve_target(plan, config):
    """Resolve the control FRESH, inside the validator.

    Any `_resolved` already on the plan is discarded first: it could only have
    come from an LLM tool call, and trusting it would let a model click "Send"
    while claiming it had resolved a harmless "Save" button.
    """
    args = plan.arguments if isinstance(plan.arguments, dict) else {}
    args.pop("_resolved", None)
    resolver = _TARGET_RESOLVER or _default_resolve
    try:
        target = resolver(plan, config)
    except Exception:
        return None
    if target is None:
        return None
    resolved = target.to_public() if hasattr(target, "to_public") else dict(target)
    args["_resolved"] = resolved      # the executor clicks exactly THIS
    return resolved

# Tools that always require explicit user confirmation.
CONFIRM_TOOLS = {"run_terminal", "window_control", "delete_files",
                 "send_email", "send_message"}   # 11E: gated in their own branch

# Tools/intents that are refused outright in the MVP.
BLOCKED_TOOLS = {
    "shutdown", "restart", "shutdown_computer", "restart_computer",
    # 11E: send_email / send_message are NO LONGER blocked outright — they run
    # only after a recipient check + strong confirmation (see EMAIL_SEND_TOOLS
    # below), and the actual send is a validated click on the real Send button.
    # Payments/form-submits stay blocked: Anna never moves money.
    "submit_form", "make_payment", "pay", "make_transfer", "bank_transfer",
    "install_software", "kill_process", "stop_process",
    "change_settings", "change_system_settings",
    "move_files", "rename_files",
    "read_passwords", "export_passwords", "disable_security",
    "run_python", "execute_code", "run_code",
}

ALLOWED_HOTKEYS = {"ctrl+c", "ctrl+v", "ctrl+a", "ctrl+s", "alt+tab", "win+d"}
CONFIRM_HOTKEYS = {"ctrl+shift+esc"}

DANGEROUS_TERMINAL_PATTERNS = [
    r"\bdel\b[^\n]*/s",              # recursive delete
    r"\bformat\b",
    r"\brm\s+-r?f",                  # rm -rf / rm -fr
    r"\brmdir\b[^\n]*/s",
    r"remove-item[^\n]*-recurse",
    r"\bshutdown\b",
    r"stop-computer|restart-computer",
    r"\breg\s+(delete|add)\b",
    r"\bnet\s+user\b",
    r"\bcipher\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\bvssadmin\b",
    r"\bmkfs\b",
    r"-enc(odedcommand)?\b",         # encoded powershell
    r"invoke-expression|\biex\b",
    r"downloadstring|downloadfile",
    r"(curl|wget|invoke-webrequest|\biwr\b)[^\n]*\.(exe|msi|bat|cmd|ps1|scr)",
    r"\bschtasks\b",
    r"\bnetsh\b[^\n]*firewall",
    r"set-mppreference|\bdefender\b",
    r"\btaskkill\b",
]

_RISK_ORDER = ["low", "medium", "high", "blocked"]


def _escalate(current: str, minimum: str) -> str:
    if current not in _RISK_ORDER:
        current = "low"
    return max(current, minimum, key=_RISK_ORDER.index)


def _declared_floor_risk(tool: str) -> str:
    """A tool's DECLARED tier, expressed as a FLOOR the validator may only RAISE.

    This is the load-bearing half of "permission_tier is a floor, never a
    ceiling" (Phase 0). The floor is *added* to whatever the runtime rules below
    compute — it can lift a lazily-declared action up to its minimum, but it can
    never pull a genuinely dangerous one down. A tool that declares itself
    tier-0 SAFE is believed about its floor (low) and ignored about everything
    else: the destructive-target, dangerous-command, password-field and
    vision-guess checks all still run and still escalate.

    A name that isn't in the registry (``send_message``, ``speak``,
    ``no_action`` — validated intents with no @tool function) contributes no
    floor; the runtime rules alone govern it, exactly as before.
    """
    from app.tools import TIER_RISK, Tier, tool_spec
    spec = tool_spec(tool)
    if spec is None or spec.tier >= Tier.BLOCKED:
        return "low"
    return TIER_RISK.get(spec.tier, "low")


def _norm_hotkey(keys) -> str:
    if isinstance(keys, (list, tuple)):
        keys = "+".join(str(k) for k in keys)
    return str(keys).lower().replace(" ", "").replace("windows", "win")


def _path_is_safe(raw: str, safe_folders) -> bool:
    """True if raw is a safe-folder name ('downloads') or a path inside one."""
    if not raw:
        return True  # tool will use defaults, which are safe folders
    name = str(raw).lower().strip().removesuffix(" folder").strip()
    folders = [Path(f) for f in safe_folders]
    if not Path(raw).expanduser().is_absolute():
        return any(f.name.lower() == name for f in folders) or name in ("", "all")
    p = Path(raw).expanduser()
    for f in folders:
        try:
            if p.resolve().is_relative_to(f.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def validate_action(plan, config) -> SafetyResult:
    """Validate an ActionPlan against the safety policy. Never raises."""
    tool = (plan.tool_name or plan.intent or "").strip().lower()
    args = plan.arguments or {}

    def blocked(reason: str) -> SafetyResult:
        return SafetyResult(allowed=False, requires_confirmation=False,
                            risk_level="blocked", reason=reason)

    if tool in BLOCKED_TOOLS:
        return blocked(f"'{tool}' is not allowed in the MVP.")
    if tool not in SAFE_TOOLS | CONFIRM_TOOLS:
        return blocked(f"Unknown tool '{tool}' — only whitelisted tools may run.")

    risk = str(plan.risk_level or "low").lower()
    if risk not in _RISK_ORDER:
        risk = "low"
    # Apply the tool's DECLARED tier as a floor FIRST, so every runtime rule
    # below can still only push higher (never lower). A CONFIRM-tier tool starts
    # at medium-and-needs-confirmation even before its specific branch runs; a
    # SAFE-tier tool starts wherever the plan put it. See _declared_floor_risk.
    floor = _declared_floor_risk(tool)
    risk = _escalate(risk, floor)
    requires = bool(plan.requires_confirmation) or _RISK_ORDER.index(floor) >= 1
    reason = ""

    if tool == "run_terminal":
        cmd = str(args.get("command", ""))
        for pat in DANGEROUS_TERMINAL_PATTERNS:
            if re.search(pat, cmd, re.IGNORECASE):
                return blocked(f"Terminal command matches a dangerous pattern: {pat}")
        requires = True
        risk = _escalate(risk, "medium")
        reason = "Terminal commands always require confirmation."

    elif tool == "delete_files":
        requires = True
        risk = _escalate(risk, "high")
        reason = "File deletion is destructive (and disabled in the MVP)."

    elif tool == "window_control":
        app = str(args.get("app") or args.get("app_name")
                  or args.get("target") or "").lower().strip()
        # "the browser" resolves to a concrete, approved browser (chrome/edge)
        # the same way the tool will — so "close the browser" isn't blocked.
        from app.tools.window_control import normalize_window_app
        app = normalize_window_app(app, config)
        if app and app not in {key.lower() for key in config.app_aliases}:
            return blocked(f"Window target '{app}' is not an approved app alias.")
        requires = True
        risk = _escalate(risk, "medium")
        reason = "Window control affects your active window."

    elif tool in CONTROL_TOOLS:
        # 11C.4 — the hardcoded destructive-target check. This runs AFTER a
        # fresh resolution and BEFORE anything is clicked, inside the
        # validator, so no plan and no cloud model can route around it.
        resolved = _resolve_target(plan, config)
        if resolved is None:
            return blocked("I couldn't find that control on screen, so I "
                           "won't guess and click something else.")

        name = str(resolved.get("name") or "")
        confidence = float(resolved.get("confidence", 1.0))
        hint = str(args.get("hint") or args.get("target") or "")

        # 11E / principle 9: clicking an actual payment/transfer control is
        # BLOCKED outright, not merely confirmed. (A bare "Pay" button is
        # handled as a destructive-target below; an explicit money-movement
        # phrase — "Pay $500", "confirm payment", "transfer funds" — is refused.)
        if is_payment_action(name) or is_payment_action(hint):
            return blocked("That looks like a payment or money transfer — I "
                           "won't do that. I can help you get to the page, but "
                           "you complete any payment yourself.")

        result_extra = {"target": resolved, "confidence": confidence}
        if is_destructive_target(name, config) or is_destructive_target(hint, config):
            requires = True
            risk = _escalate(risk, "high")
            result_extra["destructive_target"] = True
            reason = (f"“{name or hint}” is a destructive control — that "
                      "always needs your explicit OK.")
        if confidence < 1.0:
            # A vision guess. Never clicked without a human seeing the crop.
            requires = True
            risk = _escalate(risk, "high")
            result_extra["destructive_target"] = True
            reason = (f"I couldn't find that control properly and had to go by "
                      f"what I can see ({confidence:.0%} sure). Check the "
                      f"picture before I click.")
        if tool in TYPE_TOOLS and resolved.get("is_password"):
            requires = True
            risk = _escalate(risk, "high")
            result_extra["destructive_target"] = True
            reason = "That's a password field — I won't type into it unasked."

        if risk == "blocked":
            return blocked(plan.confirmation_message or "Blocked.")
        if config.confirmation_mode == "strict" and risk in ("medium", "high"):
            requires = True
        return SafetyResult(allowed=True, requires_confirmation=requires,
                            risk_level=risk, reason=reason, **result_extra)

    elif tool == "send_email":
        # 11E: sending is ALWAYS preview + strong confirmation, and never
        # happens without a clear recipient ADDRESS. Multiple recipients are
        # allowed but the confirmation names them all. Composing a draft is a
        # different, ungated tool that sends nothing.
        from app.tools.email_tools import recipient_status
        to = args.get("to") or args.get("recipient") or args.get("recipients")
        emails, status = recipient_status(to)
        if status in ("missing", "ambiguous"):
            return blocked("I won't send an email without a clear recipient "
                           "address — tell me exactly who it goes to.")
        body = str(args.get("body") or args.get("text") or "")
        reason = (f"Send this email to {', '.join(emails)}"
                  + (" — multiple recipients" if status == "multiple" else "")
                  + "?")
        return SafetyResult(allowed=True, requires_confirmation=True,
                            risk_level="high", reason=reason,
                            destructive_target=True,
                            target={"to": emails,
                                    "subject": str(args.get("subject") or ""),
                                    "body": body[:400], "recipient_status": status})

    elif tool == "send_message":
        # Messaging (Teams/WhatsApp/etc). The recipient is a contact NAME, not
        # an address, but a message still can't go out without one, and the
        # send is always confirmed (also the app's "Send" button is a
        # destructive-target, so a click-based send is gated too).
        to = str(args.get("to") or args.get("recipient")
                 or args.get("chat") or "").strip()
        if not to:
            return blocked("Who should I message? I won't send without a "
                           "named recipient.")
        body = str(args.get("body") or args.get("text") or args.get("message") or "")
        return SafetyResult(allowed=True, requires_confirmation=True,
                            risk_level="high",
                            reason=f"Send this message to {to}?",
                            destructive_target=True,
                            target={"to": [to], "body": body[:400]})

    elif tool in VISION_LOOK_TOOLS:
        # 11B.4: looking at a screen Anna flagged as sensitive (passwords,
        # keys, banking) is only ever done with an explicit confirmation.
        if str(args.get("allow_sensitive", "")).strip().lower() in \
                ("1", "true", "yes", "on"):
            requires = True
            risk = _escalate(risk, "high")
            reason = ("That screen looks like it holds credentials or payment "
                      "details — analyzing it needs your explicit OK.")

    elif tool == "type_text":
        text = str(args.get("text", ""))
        if len(text) > config.max_type_text_no_confirm:
            requires = True
            risk = _escalate(risk, "medium")
            reason = f"Text longer than {config.max_type_text_no_confirm} characters."

    elif tool == "press_hotkey":
        keys = _norm_hotkey(args.get("keys") or args.get("hotkey") or "")
        if keys in CONFIRM_HOTKEYS:
            requires = True
            risk = _escalate(risk, "medium")
            reason = f"Hotkey {keys} requires confirmation."
        elif keys not in ALLOWED_HOTKEYS:
            return blocked(f"Hotkey '{keys}' is not on the allowed list.")

    elif tool in ("open_folder", "search_files"):
        raw = str(args.get("folder") or args.get("path") or args.get("target_path") or "")
        if not _path_is_safe(raw, config.safe_folders):
            return blocked(f"'{raw}' is outside your approved safe folders.")

    if risk == "blocked":
        return blocked(plan.confirmation_message or "The model flagged this action as blocked.")

    # Strict mode: anything medium+ needs a human.
    if config.confirmation_mode == "strict" and risk in ("medium", "high"):
        requires = True

    return SafetyResult(allowed=True, requires_confirmation=requires,
                        risk_level=risk, reason=reason)
