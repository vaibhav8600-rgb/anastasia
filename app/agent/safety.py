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


# Tools that may run without confirmation (unless rules below escalate them).
SAFE_TOOLS = {
    "open_app", "open_folder", "take_screenshot", "search_files",
    "clipboard_read", "clipboard_write", "summarize_clipboard",
    "type_text", "press_hotkey", "browser_open", "speak",
    "ask_clarification", "no_action",
}

# Tools that always require explicit user confirmation.
CONFIRM_TOOLS = {"run_terminal", "window_control", "delete_files"}

# Tools/intents that are refused outright in the MVP.
BLOCKED_TOOLS = {
    "shutdown", "restart", "shutdown_computer", "restart_computer",
    "send_email", "send_message", "submit_form", "make_payment",
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
    requires = bool(plan.requires_confirmation)
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
        app = str(args.get("app") or "").lower().strip()
        if app and app not in {key.lower() for key in config.app_aliases}:
            return blocked(f"Window target '{app}' is not an approved app alias.")
        requires = True
        risk = _escalate(risk, "medium")
        reason = "Window control affects your active window."

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
