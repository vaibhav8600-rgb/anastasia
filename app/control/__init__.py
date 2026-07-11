"""Control backends (Phase 11C) — how Anna finds something to click.

Structured backends first, always:

  * **UIA** (`uiautomation`) for native Windows apps — real control objects
    with real names, types and bounding boxes.
  * **Playwright** over CDP for web page content — the real DOM.
  * **Vision** coordinates are the LAST RESORT and are never trusted: any
    vision-resolved target carries `confidence < 1.0`, which the safety
    validator turns into a mandatory confirmation showing a cropped
    screenshot of exactly what would be clicked.

A `ResolvedTarget` is produced only by Anna's own resolver. Nothing a model
says can manufacture one — the validator strips any model-supplied target and
resolves afresh (see `app/agent/safety.py`).
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol

# Confidence below this is a guess and always needs a human.
CERTAIN = 1.0


class BackendUnavailable(Exception):
    """This backend can't run here (missing dep, no browser attached, ...)."""


class TargetNotFound(Exception):
    """No backend could locate the control."""


@dataclass
class Scope:
    """Where to look. Empty = the foreground window."""

    app: str = ""              # alias key, e.g. "chrome" / "notepad"
    window_title: str = ""     # substring of the window title
    is_browser: bool = False


@dataclass
class ResolvedTarget:
    name: str                  # accessible name / visible text
    control_type: str          # Button, Edit, Link, MenuItem, ...
    bbox: tuple = (0, 0, 0, 0)  # left, top, right, bottom (screen coords)
    backend: str = "uia"       # uia | playwright | vision
    confidence: float = CERTAIN
    app: str = ""
    window_title: str = ""
    is_password: bool = False
    selector: str = ""         # playwright locator, when applicable
    crop_data_url: str = ""    # vision fallback: picture of the exact target
    handle: object = field(default=None, repr=False)   # live backend object

    @property
    def certain(self) -> bool:
        return self.confidence >= CERTAIN

    def center(self) -> tuple:
        left, top, right, bottom = self.bbox
        return ((left + right) // 2, (top + bottom) // 2)

    def to_public(self) -> dict:
        """Serializable view for the validator, confirmation card and logs.
        Deliberately drops the live handle; keeps the crop so the user can see
        what a low-confidence click would hit."""
        return {"name": self.name, "control_type": self.control_type,
                "bbox": list(self.bbox), "backend": self.backend,
                "confidence": round(float(self.confidence), 3),
                "app": self.app, "window_title": self.window_title,
                "is_password": bool(self.is_password),
                "selector": self.selector,
                "crop_data_url": self.crop_data_url}

    def describe(self) -> str:
        return (f"{self.backend}: {self.control_type} “{self.name}” at "
                f"{tuple(self.bbox)} (confidence {self.confidence:.2f})")


@dataclass
class ActionResult:
    success: bool
    message: str


class ControlBackend(Protocol):
    name: str

    def available(self) -> bool: ...
    def find_control(self, hint: str, scope: Scope) -> Optional[ResolvedTarget]: ...
    def click(self, target: ResolvedTarget) -> ActionResult: ...
    def type_into(self, target: ResolvedTarget, text: str) -> ActionResult: ...
