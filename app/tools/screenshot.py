"""take_screenshot — saved to the configured screenshots folder, with a small
inline thumbnail for the chat (9.1D). The full-resolution image is NEVER
inlined into chat/history/cloud — only a capped-size local thumbnail data URL
that stays in the session."""

import base64
import io
import threading
from datetime import datetime
from pathlib import Path

from app.tools import Tier, ToolContext, ToolResult, tool

THUMB_MAX_WIDTH = 320
THUMB_BUDGET_S = 1.0   # if generation is slower, show the card without preview


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


def _monitor_rects() -> list:
    """Left-to-right ordered (left, top, right, bottom) rects for each monitor.
    Empty on failure -> caller falls back to the whole virtual desktop."""
    try:
        import ctypes
        from ctypes import wintypes
        rects = []
        proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(_hmon, _hdc, lprc, _lparam):
            r = lprc.contents
            rects.append((r.left, r.top, r.right, r.bottom))
            return 1
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, proc(cb), 0)
        rects.sort(key=lambda r: (r[0], r[1]))   # screen 1 = leftmost
        return rects
    except Exception:
        return []


@tool("take_screenshot", tier=Tier.SAFE, offline_ok=True,
      description="Save a screenshot of the desktop (or one monitor) to disk.",
      schema={"screen": ("integer", "monitor number, or 0 for all screens")})
def take_screenshot(args: dict, ctx: ToolContext) -> ToolResult:
    # PIL.ImageGrab directly — pyautogui's import chain costs seconds on
    # first use; ImageGrab is what it calls on Windows anyway.
    from PIL import ImageGrab
    out_dir = Path(ctx.config.screenshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"anna_{datetime.now():%Y%m%d_%H%M%S}.png"

    monitors = _monitor_rects()
    try:
        screen = int(args.get("screen") or 0)
    except (TypeError, ValueError):
        screen = 0

    note = ""
    if screen and monitors:
        if 1 <= screen <= len(monitors):
            ImageGrab.grab(bbox=monitors[screen - 1], all_screens=True).save(str(path), "PNG")
            note = f" (screen {screen})"
        else:
            # asked for a monitor that doesn't exist — grab everything + say so
            ImageGrab.grab(all_screens=True).save(str(path), "PNG")
            note = (f" — you asked for screen {screen} but I only see "
                    f"{len(monitors)}, so I captured all of them")
    elif len(monitors) > 1:
        # multi-monitor, no target -> capture the whole virtual desktop so
        # nothing is missed (bare grab() only gets the primary monitor)
        ImageGrab.grab(all_screens=True).save(str(path), "PNG")
        note = f" (all {len(monitors)} screens)"
    else:
        ImageGrab.grab().save(str(path), "PNG")

    payload = {
        "type": "screenshot",
        "full_path": str(path),
        "thumb_data_url": _make_thumbnail(path),   # may be None (still fine)
        "timestamp": datetime.now().strftime("%I:%M %p").lstrip("0"),
        "screen": screen or None,
        "monitor_count": len(monitors),
    }
    return ToolResult(True, f"Screenshot captured{note}.", data=payload)
