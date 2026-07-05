"""window_control — close/minimize/maximize the ACTIVE window only.
Always requires confirmation (enforced by agent/safety.py)."""

from app.tools import ToolContext, ToolResult, tool

_ACTIONS = {
    "close": ("alt", "f4"),
    "minimize": ("win", "down"),
    "maximize": ("win", "up"),
}


def _active_title() -> str:
    try:
        import pygetwindow
        win = pygetwindow.getActiveWindow()
        return win.title if win else ""
    except Exception:
        return ""


@tool("window_control")
def window_control(args: dict, ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "").lower().strip()
    if action not in _ACTIONS:
        return ToolResult(False, f"I can close, minimize or maximize — not '{action}'.")
    import pyautogui
    pyautogui.FAILSAFE = True
    title = _active_title()
    pyautogui.hotkey(*_ACTIONS[action])
    target = f" ({title[:60]})" if title else ""
    return ToolResult(True, f"Done — {action}d the active window{target}.")
