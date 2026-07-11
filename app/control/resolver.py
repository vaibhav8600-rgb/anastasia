"""Target resolver (11C.1) — decides WHICH backend locates a control.

Order, always:

    active app is a browser  -> Playwright (DOM)  -> UIA -> vision
    active app is native     -> UIA (control tree) -> Playwright -> vision

Vision only ever runs when both structured backends came back empty, and its
result is always `confidence < 1.0` + a cropped screenshot, which the safety
validator converts into a mandatory confirmation.

Every resolution is logged with backend, hint, resolved name, control type,
coordinates and confidence (11C.1).
"""

from app.agent.devlog import devlog
from app.control import ResolvedTarget, Scope
from app.control.playwright_backend import PlaywrightBackend, looks_like_browser
from app.control.uia import UIABackend
from app.control.vision_fallback import VisionFallbackBackend


class TargetResolver:
    def __init__(self, config, *, uia=None, playwright=None, vision=None):
        self.config = config
        self.uia = uia if uia is not None else UIABackend(config)
        self.playwright = (playwright if playwright is not None
                           else PlaywrightBackend(config))
        self.vision = (vision if vision is not None
                       else VisionFallbackBackend(config))

    # ------------------------------------------------------------- scope
    def current_scope(self, app: str = "", window_title: str = "") -> Scope:
        title = window_title
        if not title:
            try:
                from app.vision.screen import active_window_title
                title = active_window_title()
            except Exception:
                title = ""
        return Scope(app=app, window_title=title,
                     is_browser=looks_like_browser(title, app))

    def _order(self, scope: Scope):
        structured = ([self.playwright, self.uia] if scope.is_browser
                      else [self.uia, self.playwright])
        return structured

    # ----------------------------------------------------------- resolve
    def resolve(self, hint: str, scope: Scope = None, *,
                allow_vision: bool = True) -> ResolvedTarget:
        """Returns a ResolvedTarget or None. Never raises."""
        scope = scope or self.current_scope()
        for backend in self._order(scope):
            try:
                if not backend.available():
                    continue
                target = backend.find_control(hint, scope)
            except Exception as e:
                devlog.warn(f"{backend.name}: resolution error "
                            f"({' '.join(str(e).split())[:100]})")
                continue
            if target is not None:
                self._log(hint, target)
                return target

        if not allow_vision:
            return None
        # Last resort only: both structured backends found nothing.
        try:
            if self.vision.available():
                target = self.vision.find_control(hint, scope)
                if target is not None:
                    self._log(hint, target)
                    return target
        except Exception as e:
            devlog.warn(f"vision: resolution error "
                        f"({' '.join(str(e).split())[:100]})")
        devlog.log(f"Resolve {hint!r}: no backend located it.")
        return None

    def backend_for(self, target: ResolvedTarget):
        return {"uia": self.uia, "playwright": self.playwright,
                "vision": self.vision}.get(target.backend)

    @staticmethod
    def _log(hint: str, target: ResolvedTarget) -> None:
        devlog.log(f"Resolve {hint!r} -> backend={target.backend} "
                   f"name={target.name!r} type={target.control_type} "
                   f"coords={tuple(target.bbox)} "
                   f"confidence={target.confidence:.2f}")
