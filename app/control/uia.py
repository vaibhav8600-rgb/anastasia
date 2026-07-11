"""UIA backend — native Windows controls via `uiautomation` (11C.3).

Returns real control objects: accessible name, control type, bounding box.
Never a pixel guess, so every hit is `confidence == 1.0`.

Accessibility-tree gaps are real (some Electron apps expose almost nothing,
games and custom-drawn UI expose nothing at all). This backend simply returns
None then, and the resolver falls through to the vision fallback — which is
unconditionally confirmation-gated.
"""

import re

from app.agent.devlog import devlog
from app.control import (ActionResult, BackendUnavailable, ResolvedTarget,
                         Scope)

SEARCH_DEPTH = 12          # deep enough for ribbons/toolbars, bounded for speed
MAX_CONTROLS = 4000        # never walk a pathological tree forever

# Control types worth clicking or typing into.
INTERACTIVE = ("Button", "MenuItem", "TabItem", "ListItem", "Hyperlink",
               "CheckBox", "RadioButton", "Edit", "ComboBox", "Text",
               "SplitButton", "TreeItem", "Document")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class UIABackend:
    name = "uia"

    def __init__(self, config=None):
        self.config = config

    def available(self) -> bool:
        try:
            import uiautomation  # noqa: F401
            return True
        except Exception:
            return False

    # ----------------------------------------------------------- internals
    def _auto(self):
        try:
            import uiautomation
            return uiautomation
        except Exception as e:                     # pragma: no cover - env
            raise BackendUnavailable(f"uiautomation unavailable: {e}") from e

    def _root_window(self, scope: Scope):
        auto = self._auto()
        if scope and scope.window_title:
            window = auto.WindowControl(searchDepth=2,
                                        SubName=scope.window_title)
            if window.Exists(maxSearchSeconds=1):
                return window
        if scope and scope.app:
            window = auto.WindowControl(searchDepth=2, SubName=scope.app)
            if window.Exists(maxSearchSeconds=1):
                return window
        return auto.GetForegroundControl()

    def _walk(self, root):
        auto = self._auto()
        count = 0
        for control, _depth in auto.WalkControl(root, maxDepth=SEARCH_DEPTH):
            count += 1
            if count > MAX_CONTROLS:
                devlog.warn("UIA: control tree too large, stopping the walk.")
                return
            yield control

    @staticmethod
    def _to_target(control, scope: Scope) -> ResolvedTarget:
        rect = control.BoundingRectangle
        return ResolvedTarget(
            name=control.Name or "",
            control_type=(control.ControlTypeName or "").replace("Control", ""),
            bbox=(rect.left, rect.top, rect.right, rect.bottom),
            backend="uia", confidence=1.0,
            app=(scope.app if scope else ""),
            window_title=(scope.window_title if scope else ""),
            is_password=bool(getattr(control, "IsPassword", False)),
            handle=control)

    # ------------------------------------------------------------- lookup
    def find_control(self, hint: str, scope: Scope = None):
        """Exact name match wins, then a contained match, then fuzzy. All are
        real controls, so all are confidence 1.0 — 'fuzzy' here means the
        NAME matched loosely, not that the location was guessed."""
        scope = scope or Scope()
        want = _norm(hint)
        if not want:
            return None
        try:
            root = self._root_window(scope)
        except Exception as e:
            devlog.warn(f"UIA: no window for {scope.app or 'foreground'} ({e})")
            return None
        if root is None:
            return None

        exact, contained, fuzzy_best, fuzzy_score = None, None, None, 0.0
        try:
            from rapidfuzz import fuzz
        except Exception:                              # pragma: no cover
            fuzz = None

        try:
            for control in self._walk(root):
                type_name = (control.ControlTypeName or "")
                if not any(t in type_name for t in INTERACTIVE):
                    continue
                name = _norm(control.Name)
                if not name:
                    continue
                if name == want:
                    exact = control
                    break
                if contained is None and (want in name or name in want):
                    contained = control
                elif fuzz is not None:
                    score = fuzz.token_sort_ratio(want, name)
                    if score > fuzzy_score:
                        fuzzy_best, fuzzy_score = control, score
        except Exception as e:
            devlog.warn(f"UIA: walk failed ({' '.join(str(e).split())[:100]})")

        control = exact or contained or (fuzzy_best if fuzzy_score >= 88 else None)
        if control is None:
            return None
        return self._to_target(control, scope)

    def read_window_text(self, scope: Scope = None) -> str:
        """Visible text of a window. Password fields are SKIPPED entirely —
        their contents are never read, logged or transmitted (principle 8)."""
        scope = scope or Scope()
        root = self._root_window(scope)
        if root is None:
            return ""
        lines, skipped = [], 0
        for control in self._walk(root):
            if getattr(control, "IsPassword", False):
                skipped += 1
                continue
            name = (control.Name or "").strip()
            if name and name not in lines:
                lines.append(name)
        if skipped:
            devlog.log(f"UIA: skipped {skipped} password field(s) while reading.")
        return "\n".join(lines)

    # ------------------------------------------------------------ actions
    def click(self, target: ResolvedTarget) -> ActionResult:
        control = target.handle
        try:
            if control is not None and hasattr(control, "Click"):
                control.Click(simulateMove=False)
            else:
                import pyautogui
                pyautogui.click(*target.center())
            return ActionResult(True, f"Clicked “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't click that: "
                                       f"{' '.join(str(e).split())[:120]}")

    def type_into(self, target: ResolvedTarget, text: str) -> ActionResult:
        control = target.handle
        try:
            if control is not None and hasattr(control, "SetFocus"):
                control.SetFocus()
            import pyautogui
            pyautogui.typewrite(text, interval=0.01)
            # NEVER echo the text for a password field.
            shown = "•" * len(text) if target.is_password else f"“{text[:40]}”"
            return ActionResult(True, f"Typed {shown} into “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't type there: "
                                       f"{' '.join(str(e).split())[:120]}")
