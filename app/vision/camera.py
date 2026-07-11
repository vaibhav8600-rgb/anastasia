"""Camera — the Mira consent pattern (11B.3).

Never auto-starts. On an explicit request only: open the camera, grab exactly
ONE frame, stop the stream immediately. A visible red indicator is on for
precisely that window and no longer. Nothing is recorded, nothing is kept,
and no person recognition happens (deliberately out of scope — adding it
would need its own consent-gated enrollment step).

The default backend is the browser's `getUserMedia` inside the WebView, which
is exactly Mira's open -> capture -> `track.stop()` flow and needs no extra
Python dependency. An OpenCV opener is used instead when one is supplied.
"""

import base64
import io
import threading

from app.agent.devlog import devlog
from app.vision import CameraUnavailable, VisionFrame

CAMERA_TIMEOUT_S = 12.0
INSTALL_HINT = ("I couldn't reach a camera. The app window needs camera "
                "permission, or install OpenCV (pip install opencv-python).")


def image_from_data_url(data_url: str):
    """Decode the `data:image/...;base64,...` the browser hands back."""
    if not data_url or "," not in data_url:
        raise CameraUnavailable("The camera returned an empty frame.")
    from PIL import Image
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def is_blank(image) -> bool:
    """True for a single-colour frame — a webcam that hasn't warmed up yet
    hands back pure black, which must never be described or saved as if it
    were a real photo."""
    try:
        low, high = image.convert("L").getextrema()
    except Exception:
        return False
    return low == high


# A real scene — even a dim one — has plenty of luminance variation. A virtual
# camera that isn't connected (Camo/OBS/DroidCam) or a webcam another app has
# seized hands back a near-featureless grey card instead.
FLAT_STD = 5.0


def flatness(image) -> float:
    """Luminance standard deviation. Near zero = a featureless frame."""
    try:
        import numpy as np
        return float(np.asarray(image.convert("L"), dtype="float32").std())
    except Exception:
        return 255.0        # can't tell -> assume it's fine


def looks_flat(image) -> bool:
    """A grey/blank placeholder rather than a real view of the world."""
    return flatness(image) < FLAT_STD


class BrowserCameraStream:
    """getUserMedia in the WebView: the JS side opens the camera, draws one
    frame, and stops every track before returning the data URL. `stop()` is
    the belt-and-braces second kill."""

    def __init__(self, request, stop_fn=None, timeout_s: float = CAMERA_TIMEOUT_S):
        self._request = request          # callable(timeout) -> data URL
        self._stop_fn = stop_fn
        self._timeout = timeout_s

    def read(self):
        return image_from_data_url(self._request(self._timeout))

    def stop(self) -> None:
        if self._stop_fn is not None:
            try:
                self._stop_fn()
            except Exception:
                pass


class OpenCVCameraStream:                       # pragma: no cover - optional dep
    def __init__(self, index: int = 0):
        import cv2
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise CameraUnavailable(INSTALL_HINT)

    def read(self):
        from PIL import Image
        ok, frame = self._cap.read()
        if not ok:
            raise CameraUnavailable("The camera gave no frame.")
        return Image.fromarray(self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB))

    def stop(self) -> None:
        try:
            self._cap.release()
        except Exception:
            pass


class CameraSession:
    """Owns the red indicator and the guarantee that the stream is open for
    exactly one frame. `active` is False before and after every capture."""

    def __init__(self, config, *, opener=None, on_indicator=None):
        self.config = config
        self.opener = opener              # callable() -> stream (read/stop)
        self.on_indicator = on_indicator  # callable(active: bool)
        self._active = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def _indicate(self, active: bool) -> None:
        self._active = active
        if self.on_indicator is not None:
            try:
                self.on_indicator(active)
            except Exception:
                pass

    def capture_once(self, attempts: int = 2) -> VisionFrame:
        """Open -> one frame -> stop. The stream is closed in `finally`, so a
        failure mid-read can never leave the camera (or its light) on.

        Many webcams emit a black frame for the first few hundred ms after
        opening, so a blank frame is retried once (a fresh open each time,
        with a short warm-up) before we give up honestly."""
        if self.opener is None:
            raise CameraUnavailable(INSTALL_HINT)
        import time as _t
        image = None
        with self._lock:                  # never two cameras at once
            self._indicate(True)
            try:
                for attempt in range(max(1, attempts)):
                    stream = None
                    try:
                        stream = self.opener()
                        candidate = stream.read()
                    finally:
                        if stream is not None:
                            stream.stop()     # immediately, per Mira's pattern
                    if not is_blank(candidate):
                        image = candidate
                        break
                    if attempt + 1 < max(1, attempts):
                        devlog.log(f"Vision: camera gave a blank frame "
                                   f"(attempt {attempt + 1}) — retrying.")
                        _t.sleep(0.5)         # let the sensor warm up
            finally:
                self._indicate(False)
        if image is None:
            raise CameraUnavailable(
                "The camera only gave me blank frames — it may be covered, "
                "disabled, or in use by another app. Try again in a moment?")
        frame = VisionFrame(image=image, source="camera", scope="camera",
                            width=image.size[0], height=image.size[1])
        devlog.log(f"Vision: captured {frame.describe()} (stream closed)")
        return frame

    def stop(self) -> None:
        """Privacy-mode kill switch. A single-frame capture is already closed;
        this only clears a stuck indicator."""
        if self._active:
            self._indicate(False)
