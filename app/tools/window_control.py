"""window_control — close/minimize/maximize an approved app or active window.
Always requires confirmation (enforced by agent/safety.py)."""

import re

from app.tools import Tier, ToolContext, ToolResult, tool

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


# Generic words for "a browser" -> the browser that's actually open.
_BROWSER_WORDS = {"browser", "the browser", "web browser", "my browser",
                  "internet", "internet browser"}
_KNOWN_BROWSERS = ("chrome", "edge", "firefox", "brave", "opera")


def normalize_window_app(app: str, config) -> str:
    """Resolve a generic 'browser' to a concrete, approved browser, and map
    common spoken/model synonyms ('code' -> vscode) to their alias key.
    Non-alias names pass through unchanged so 'python'/'anastasia' still hit
    the own-window / alias guards."""
    app = (app or "").lower().strip()
    # Synonym map (same source the router uses): 'code' -> 'vscode', etc.
    from app.agent.router import APP_SYNONYMS, _norm
    canon = APP_SYNONYMS.get(_norm(app))
    if canon and canon in {key.lower() for key in getattr(config, "app_aliases", {})}:
        return canon
    if app not in _BROWSER_WORDS:
        return app
    default = (getattr(config, "default_browser", "") or "").lower().strip()
    if default:
        return default
    aliases = {key.lower() for key in getattr(config, "app_aliases", {})}
    try:
        import pygetwindow
        titles = " ".join(str(getattr(w, "title", "") or "").lower()
                          for w in pygetwindow.getAllWindows())
        for browser in _KNOWN_BROWSERS:
            if browser in titles and browser in aliases:
                return browser
    except Exception:
        pass
    for browser in _KNOWN_BROWSERS:
        if browser in aliases:
            return browser
    return app          # no browser configured -> let the alias guard refuse it


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


def _is_own_window(title: str, ctx: ToolContext) -> bool:
    """True if the window is Anna herself or the terminal running her — never
    close those."""
    low = (title or "").lower()
    nick = str(getattr(ctx.config, "assistant_nickname", "Anna")).lower()
    name = str(getattr(ctx.config, "assistant_name", "Anastasia")).lower()
    own = (nick, name, "anastasia", "anna",
           "windows powershell", "command prompt", "python")
    return any(marker and marker in low for marker in own)


@tool("window_control", tier=Tier.CONFIRM, offline_ok=True,
      description="Close, minimize or maximize a named app window (needs "
                  "confirmation; never closes Anna's own window).",
      schema={"action": ("string", "close | minimize | maximize"),
              "app": ("string", "the app whose window to act on")},
      required=("action",))
def window_control(args: dict, ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "").lower().strip()
    if action not in _ACTIONS:
        return ToolResult(False, f"I can close, minimize or maximize — not '{action}'.")
    app = str(args.get("app") or args.get("app_name") or args.get("target") or "").lower().strip()
    app = normalize_window_app(app, ctx.config)   # 'the browser' -> chrome/edge

    # A bare "close" with no named app would Alt+F4 whatever is focused — which
    # is usually Anna herself or the terminal. Require an explicit target for
    # closing so Anna never closes an unintended (or her own) window.
    if action == "close" and not app:
        return ToolResult(False, "Which window should I close? Name the app — "
                          "like “close Chrome” or “close Notepad”. "
                          "I won't close whatever happens to be focused.")
    if app and app not in {key.lower() for key in ctx.config.app_aliases}:
        return ToolResult(False, f"I can only {action} approved app names. "
                          f"I don't have '{app}' registered yet.")

    import pyautogui
    pyautogui.FAILSAFE = True
    title = _activate_app_window(app) if app else _active_title()
    if app and not title:
        return ToolResult(False, f"I couldn't find an open {app} window to {action}.")
    if action == "close" and _is_own_window(title, ctx):
        return ToolResult(False, "That looks like my own window (or the terminal "
                          "running me) — I'll leave it be. Tell me another app to close.")

    pyautogui.hotkey(*_ACTIONS[action])
    target = f" ({title[:60]})" if title else ""
    past = {"close": "closed", "minimize": "minimized", "maximize": "maximized"}[action]
    return ToolResult(True, f"Done — {past} the window{target}.")
