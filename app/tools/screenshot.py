"""take_screenshot — saved to the configured screenshots folder, with a small
inline thumbnail for the chat (9.1D). The full-resolution image is NEVER
inlined into chat/history/cloud — only a capped-size local thumbnail data URL
that stays in the session."""

import base64
import io
import threading
from datetime import datetime
from pathlib import Path

from app.tools import ToolContext, ToolResult, tool

THUMB_MAX_WIDTH = 320
THUMB_BUDGET_S = 0.5   # if generation is slower, show the card without preview


def _make_thumbnail(image_path: Path) -> str | None:
    """Return a small PNG data URL, or None if it can't be made in budget.
    Runs the resize in a worker with a hard time cap so a huge multi-monitor
    grab never stalls the pipeline."""
    result = {"url": None}

    def build():
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                img = img.convert("RGB")
                w, h = img.size
                if w > THUMB_MAX_WIDTH:
                    img = img.resize((THUMB_MAX_WIDTH,
                                      max(1, round(h * THUMB_MAX_WIDTH / w))))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=70)
            result["url"] = "data:image/jpeg;base64," + \
                base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            result["url"] = None

    t = threading.Thread(target=build, daemon=True)
    t.start()
    t.join(THUMB_BUDGET_S)
    return result["url"]   # None if the thread is still running past the cap


@tool("take_screenshot")
def take_screenshot(args: dict, ctx: ToolContext) -> ToolResult:
    # PIL.ImageGrab directly — pyautogui's import chain costs seconds on
    # first use; ImageGrab is what it calls on Windows anyway.
    from PIL import ImageGrab
    out_dir = Path(ctx.config.screenshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"anna_{datetime.now():%Y%m%d_%H%M%S}.png"
    ImageGrab.grab().save(str(path), "PNG")

    payload = {
        "type": "screenshot",
        "full_path": str(path),
        "thumb_data_url": _make_thumbnail(path),   # may be None (still fine)
        "timestamp": datetime.now().strftime("%I:%M %p").lstrip("0"),
    }
    return ToolResult(True, "Screenshot captured.", data=payload)
