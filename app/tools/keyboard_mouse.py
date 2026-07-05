"""type_text / press_hotkey — via PyAutoGUI (lazy-imported, failsafe on)."""

from app.agent.safety import ALLOWED_HOTKEYS, CONFIRM_HOTKEYS, _norm_hotkey
from app.tools import ToolContext, ToolResult, tool


def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = True  # slam mouse into a corner to abort
    return pyautogui


@tool("type_text")
def type_text(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text") or "")
    if not text:
        return ToolResult(False, "What should I type?")
    gui = _pyautogui()
    try:
        text.encode("ascii")
        gui.write(text, interval=0.02)
    except UnicodeEncodeError:
        # pyautogui.write can't type non-ASCII — paste via clipboard instead
        import pyperclip
        old = pyperclip.paste()
        pyperclip.copy(text)
        gui.hotkey("ctrl", "v")
        pyperclip.copy(old)
    return ToolResult(True, f"Typed {len(text)} characters into the active window.")


@tool("press_hotkey")
def press_hotkey(args: dict, ctx: ToolContext) -> ToolResult:
    keys = _norm_hotkey(args.get("keys") or args.get("hotkey") or "")
    if keys not in ALLOWED_HOTKEYS | CONFIRM_HOTKEYS:
        return ToolResult(False, f"Hotkey '{keys}' isn't on my allowed list.")
    parts = keys.split("+")
    _pyautogui().hotkey(*parts)
    return ToolResult(True, f"Pressed {keys}.")
