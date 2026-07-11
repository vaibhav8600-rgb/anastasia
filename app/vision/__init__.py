"""Vision (Phase 11B) — on-demand screen and camera perception.

Design invariants, enforced in code and tested:

  * **Nothing is captured until something explicitly asks.** There is no
    background capture path; `ScreenWatcher` (Mode B) only runs after an
    explicit start and stops itself when idle.
  * **Snapshots, never a stream.** Even watching mode grabs ONE frame per
    interval, processes it, and drops it. No frame is ever held across
    iterations and no video stream is opened to any cloud provider.
  * **Frames are not persisted or logged.** Only extracted text/summaries
    reach the devlog or history. Saving a capture is a per-call opt-in.
  * **Leaving the machine needs a separate consent** (`cloud_vision_consent`),
    hard-gated in `app.llm.providers.vision_cloud_allowed`.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


class VisionUnavailable(Exception):
    """Vision can't run right now (no capture backend, no camera, ...)."""


class CameraUnavailable(VisionUnavailable):
    pass


@dataclass
class VisionFrame:
    """One captured still. `image` is a PIL Image held only for the duration
    of a single analysis, then dropped — it is never logged or persisted
    unless the caller explicitly saves it."""

    image: object                 # PIL.Image.Image
    source: str = "screen"        # "screen" | "camera"
    scope: str = "full"           # full | window | region | cursor | camera
    width: int = 0
    height: int = 0
    window_title: str = ""
    monitor: int = 0
    captured_at: float = field(default_factory=time.time)

    def describe(self) -> str:
        """Safe, loggable one-liner. Deliberately carries no pixel data."""
        where = self.window_title or self.scope
        return (f"{self.source} frame {self.width}x{self.height} "
                f"({self.scope}{': ' + where if self.window_title else ''})")

    def release(self) -> None:
        """Drop the pixels as soon as analysis is done."""
        image, self.image = self.image, None
        try:
            if image is not None and hasattr(image, "close"):
                image.close()
        except Exception:
            pass


@dataclass
class VisionResult:
    """What a look produced. `needs_ack` means we saw something sensitive and
    refused to analyze it at all until the user says so (11B.4)."""

    summary: str = ""
    text: str = ""                       # OCR text (local, never auto-uploaded)
    source: str = "screen"
    scope: str = "full"
    window_title: str = ""
    used_cloud: bool = False
    needs_ack: bool = False
    ack_reason: str = ""
    saved_path: Optional[str] = None
