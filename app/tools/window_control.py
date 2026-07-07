"""window_control — close/minimize/maximize an approved app or active window.
Always requires confirmation (enforced by agent/safety.py)."""

import re

from app.tools import ToolContext, ToolResult, tool

_ACTIONS = {
    "close": ("alt", "f4"),
    "minimize": ("win", "down"),
    "maximize": ("win", "up"),
}

_TITLE_HINTS = {
    "chrome": ("chrome",), "edge": ("edge",), "notepad": ("notepad",),
    "paint": ("paint",), "mspaint": ("paint",),
    "vscode": ("visual studio code",), "vs code": ("visual studio code",),
    "file explorer": ("file explorer",), "explorer": ("file explorer",),
    "calculator": ("calculator",), "calc": ("calculator",),
    "terminal": ("terminal",), "powershell": ("powershell",),
    "teams": ("teams",),
}


def _active_title() -> str:
    try:
        import pygetwindow
        win = pygetwindow.getActiveWindow()
        return win.title if win else ""
    except Exception:
        return ""


def _activate_app_window(app: str) -> str:
    try:
        import pygetwindow
        hints = _TITLE_HINTS.get(app, (app,))
        for win in pygetwindow.getAllWindows():
            title = str(getattr(win, "title", "") or "")
            if title and any(re.search(rf"\b{re.escape(hint)}\b", title,
                                       re.IGNORECASE) for hint in hints):
                win.activate()
                return title
    except Exception:
        pass
    return ""


@tool("window_control")
def window_control(args: dict, ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "").lower().strip()
    if action not in _ACTIONS:
        return ToolResult(False, f"I can close, minimize or maximize — not '{action}'.")
    import pyautogui
    pyautogui.FAILSAFE = True
    app = str(args.get("app") or "").lower().strip()
    title = _activate_app_window(app) if app else _active_title()
    if app and not title:
        return ToolResult(False, f"I couldn't find an open {app} window.")
    pyautogui.hotkey(*_ACTIONS[action])
    target = f" ({title[:60]})" if title else ""
    past = {"close": "closed", "minimize": "minimized", "maximize": "maximized"}[action]
    return ToolResult(True, f"Done — {past} the window{target}.")
