"""Screen capture — full desktop, active window, region, or cursor area.

Uses PIL.ImageGrab (what pyautogui calls on Windows anyway, without the
multi-second import cost) and the existing multi-monitor rect helper from
the screenshot tool, so "screen 2" means the same thing everywhere.
"""

from app.agent.devlog import devlog
from app.vision import VisionFrame, VisionUnavailable

CURSOR_BOX = 480          # px square around the pointer for scope="cursor"


def _downscale(image, max_pixels: int, max_edge: int):
    """Shrink for OCR/transport by an AREA budget, with a long-edge backstop.

    A long-edge-only rule ruins wide desktops: two 2560x1440 monitors are
    5120px across, and capping the long edge at 1280 leaves 1280x360 — far
    too short for OCR to read anything. Scaling by area keeps the aspect and
    the legibility.
    """
    width, height = image.size
    scale = 1.0
    if max_pixels and width * height > max_pixels:
        scale = (max_pixels / float(width * height)) ** 0.5
    if max_edge and max(width, height) * scale > max_edge:
        scale = max_edge / float(max(width, height))
    if scale < 1.0:
        image = image.resize((max(1, round(width * scale)),
                              max(1, round(height * scale))))
    return image


def active_window_title() -> str:
    try:
        import pygetwindow
        window = pygetwindow.getActiveWindow()
        return (getattr(window, "title", "") or "").strip()
    except Exception:
        return ""


def active_window_bbox():
    """(left, top, right, bottom) of the focused window, or None."""
    try:
        import pygetwindow
        window = pygetwindow.getActiveWindow()
        if window is None or window.width <= 0 or window.height <= 0:
            return None
        return (window.left, window.top,
                window.left + window.width, window.top + window.height)
    except Exception:
        return None


def _cursor_bbox():
    try:
        import pyautogui
        x, y = pyautogui.position()
        half = CURSOR_BOX // 2
        return (x - half, y - half, x + half, y + half)
    except Exception:
        return None


def capture(config, scope: str = "full", region=None, screen: int = 0) -> VisionFrame:
    """Grab exactly one frame. Raises VisionUnavailable rather than guessing
    if the requested scope can't be resolved."""
    try:
        from PIL import ImageGrab
    except Exception as e:                      # pragma: no cover - env issue
        raise VisionUnavailable(f"Screen capture needs Pillow: {e}") from e

    from app.tools.screenshot import _monitor_rects
    scope = (scope or "full").lower()
    title = active_window_title()
    bbox = None

    if scope == "window":
        bbox = active_window_bbox()
        if bbox is None:
            raise VisionUnavailable("I couldn't tell which window is focused.")
    elif scope == "region":
        if not region or len(region) != 4:
            raise VisionUnavailable("I need a region as left, top, right, bottom.")
        bbox = tuple(int(v) for v in region)
    elif scope == "cursor":
        bbox = _cursor_bbox()
        if bbox is None:
            raise VisionUnavailable("I couldn't find the mouse pointer.")
    elif screen:
        monitors = _monitor_rects()
        if 1 <= screen <= len(monitors):
            bbox = monitors[screen - 1]
        else:
            raise VisionUnavailable(
                f"You asked for screen {screen} but I only see {len(monitors)}.")

    image = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox is not None \
        else ImageGrab.grab(all_screens=True)
    image = _downscale(image.convert("RGB"),
                       int(getattr(config, "vision_max_pixels", 1_600_000) or 0),
                       int(getattr(config, "vision_max_edge", 2600) or 0))
    frame = VisionFrame(image=image, source="screen", scope=scope,
                        width=image.size[0], height=image.size[1],
                        window_title=title, monitor=screen or 0)
    # devlog carries the description ONLY — never pixels (11B.5).
    devlog.log(f"Vision: captured {frame.describe()}")
    return frame
