"""VisionService — the one place perception is orchestrated (11B).

Order of operations for every look, screen or camera:

    capture ONE frame
      -> local OCR (never leaves the machine)
      -> sensitive-content heuristic
           sensitive & not acknowledged -> STOP. Analyze nothing, local or
           cloud. Ask the user first (11B.4).
      -> cloud description ONLY with cloud_vision_consent, else local summary
      -> release the pixels

`privacy_mode()` is the single switch that kills screen watching, the camera,
and any live audio session at once.
"""

import threading

from app.agent.devlog import devlog
from app.llm.providers import vision_cloud_allowed
from app.vision import VisionFrame, VisionResult, VisionUnavailable
from app.vision.camera import CameraSession
from app.vision.sensitive import looks_sensitive
from app.vision.watcher import ScreenWatcher

MAX_SUMMARY_TEXT = 1200


class VisionService:
    def __init__(self, config, *, capture=None, ocr=None, cloud=None,
                 camera=None, dispatch=None, stop_live=None):
        self.config = config
        self._capture = capture or self._default_capture
        self._ocr = ocr or self._default_ocr
        self._cloud = cloud or self._default_cloud
        self.dispatch = dispatch or (lambda event, payload: None)
        self.stop_live = stop_live            # callable() — ends a Live session
        self.camera = camera or CameraSession(
            config, on_indicator=lambda on: self.dispatch(
                "camera_active", {"active": bool(on)}))
        self._watcher = None
        self._lock = threading.Lock()
        self.last_capture_at = 0.0

    # --------------------------------------------------------- defaults
    def _default_capture(self, scope="full", region=None, screen=0) -> VisionFrame:
        from app.vision import screen as screen_mod
        return screen_mod.capture(self.config, scope=scope, region=region,
                                  screen=screen)

    def _default_ocr(self, frame) -> str:
        from app.vision import ocr
        return ocr.extract_text(frame, self.config)

    def _default_cloud(self, frame, question: str) -> str:
        from app.vision import cloud
        return cloud.describe_frame(frame, question, self.config)

    # ------------------------------------------------------------ state
    @property
    def watching(self) -> bool:
        return self._watcher is not None and self._watcher.watching

    @property
    def camera_active(self) -> bool:
        return self.camera.active

    def cloud_enabled(self) -> bool:
        return vision_cloud_allowed(self.config)[0]

    # ------------------------------------------------------------- look
    def look(self, *, scope: str = "full", region=None, screen: int = 0,
             question: str = "", allow_cloud: bool = True,
             allow_sensitive: bool = False, save: bool = False) -> VisionResult:
        """Mode A/C: capture one frame on demand and describe it."""
        frame = self._capture(scope=scope, region=region, screen=screen)
        return self._analyze(frame, question=question, allow_cloud=allow_cloud,
                             allow_sensitive=allow_sensitive, save=save)

    def camera_look(self, *, question: str = "", allow_cloud: bool = True,
                    allow_sensitive: bool = False, save: bool = False) -> VisionResult:
        """11B.3: open, one frame, stream stops immediately."""
        frame = self.camera.capture_once()
        return self._analyze(frame, question=question, allow_cloud=allow_cloud,
                             allow_sensitive=allow_sensitive, save=save)

    def _analyze(self, frame: VisionFrame, *, question: str, allow_cloud: bool,
                 allow_sensitive: bool, save: bool) -> VisionResult:
        import time
        self.last_capture_at = time.time()
        try:
            text = self._ocr(frame) or ""
            sensitive, reason = looks_sensitive(text)
            if sensitive and not allow_sensitive:
                # Refuse to analyze AT ALL — not locally, not in the cloud.
                devlog.warn("Vision: sensitive content detected — not analyzed.")
                return VisionResult(source=frame.source, scope=frame.scope,
                                    window_title=frame.window_title,
                                    needs_ack=True, ack_reason=reason)

            used_cloud = False
            summary = ""
            if allow_cloud and self.cloud_enabled():
                try:
                    summary = self._cloud(frame, question)
                    used_cloud = bool(summary)
                except Exception as e:
                    devlog.warn(f"Vision: cloud description failed, staying "
                                f"local ({' '.join(str(e).split())[:120]})")
            if not summary:
                summary = self._local_summary(frame, text)

            saved_path = self._save(frame) if (save or getattr(
                self.config, "vision_save_captures", False)) else None
            return VisionResult(summary=summary, text=text[:MAX_SUMMARY_TEXT],
                                source=frame.source, scope=frame.scope,
                                window_title=frame.window_title,
                                used_cloud=used_cloud, saved_path=saved_path)
        finally:
            frame.release()          # pixels dropped, always

    def _local_summary(self, frame: VisionFrame, text: str) -> str:
        """Fully-local description: window title + OCR text, or an honest
        admission when no OCR engine is installed."""
        if frame.source == "camera":
            if text:
                return f"I can make out some text in the shot: {text[:300]}"
            return ("I took a single photo, but without cloud vision I can only "
                    "read text, not describe a scene. Turn on cloud vision in "
                    "Settings if you'd like me to describe what's in it.")
        # For a whole-desktop grab the focused window title is NOT what we're
        # looking at (it was reading "On Anastasia (Anna) I can read…" while
        # capturing the entire desktop).
        where = frame.window_title if frame.scope == "window" else "your screen"
        where = where or "your screen"
        if text:
            body = text[:MAX_SUMMARY_TEXT]
            return f"On {where} I can read:\n{body}"
        from app.vision.ocr import ocr_status
        available, reason = ocr_status(self.config)
        if not available:
            return (f"I'm looking at {where}, but I can't read the text: "
                    f"{reason}")
        return f"I'm looking at {where}, but I couldn't make out any text."

    def _save(self, frame: VisionFrame):
        """Per-capture opt-in only (11B.5). Reuses the screenshots folder."""
        try:
            from datetime import datetime
            from pathlib import Path
            out_dir = Path(self.config.screenshot_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"anna_vision_{datetime.now():%Y%m%d_%H%M%S}.png"
            frame.image.save(str(path), "PNG")
            # Be explicit about WHY a raw frame is on disk — "on request" read
            # as if the user had asked for this one capture.
            why = ("Settings → 'Save every capture' is ON"
                   if getattr(self.config, "vision_save_captures", False)
                   else "asked for this capture")
            devlog.warn(f"Vision: raw {frame.source} frame written to disk "
                        f"({why}) -> {path}")
            return str(path)
        except Exception as e:
            devlog.warn(f"Vision: save failed ({e})")
            return None

    # -------------------------------------------------- watching (Mode B)
    def start_watching(self, on_update=None) -> bool:
        """Explicit opt-in only. Never called on startup."""
        with self._lock:
            if self.watching:
                return False
            self._watcher = ScreenWatcher(
                self.config,
                on_frame=on_update or self._watch_tick,
                capture=lambda: self._capture(scope="full"),
                on_indicator=lambda on: self.dispatch(
                    "screen_vision", {"active": bool(on)}))
            return self._watcher.start()

    def _watch_tick(self, frame: VisionFrame) -> None:
        """Each frame is processed independently and then dropped by the
        watcher. We only keep the text length — never the pixels."""
        text = self._ocr(frame) or ""
        if text:
            devlog.log(f"Vision: watch tick — {len(text)} chars of text on "
                       f"{frame.window_title or 'screen'}")

    def stop_watching(self, reason: str = "user") -> bool:
        watcher = self._watcher
        if watcher is None or not watcher.watching:
            return False
        watcher.stop(reason)
        return True

    def note_activity(self) -> None:
        if self._watcher is not None:
            self._watcher.note_activity()

    # ------------------------------------------------------ privacy mode
    def privacy_mode(self) -> dict:
        """One switch: screen watching off, camera off, Live audio off."""
        stopped = {"screen": self.stop_watching("privacy mode"),
                   "camera": self.camera_active, "live": False}
        self.camera.stop()
        if self.stop_live is not None:
            try:
                stopped["live"] = bool(self.stop_live())
            except Exception as e:
                devlog.exception(e, context="privacy_mode stop_live")
        devlog.warn(f"Privacy mode: capture stopped ({stopped}).")
        self.dispatch("privacy_mode", {"stopped": stopped})
        return stopped
