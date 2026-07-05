"""take_screenshot — saved to the configured screenshots folder."""

from datetime import datetime
from pathlib import Path

from app.tools import ToolContext, ToolResult, tool


@tool("take_screenshot")
def take_screenshot(args: dict, ctx: ToolContext) -> ToolResult:
    # PIL.ImageGrab directly — pyautogui's import chain costs seconds on
    # first use; ImageGrab is what it calls on Windows anyway.
    from PIL import ImageGrab
    out_dir = Path(ctx.config.screenshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"anna_{datetime.now():%Y%m%d_%H%M%S}.png"
    ImageGrab.grab().save(str(path), "PNG")
    return ToolResult(True, f"Screenshot saved to {path}.", data=str(path))
