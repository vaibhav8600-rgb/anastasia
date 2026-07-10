"""Mode B — low-frequency screen watching (11B.1).

Explicitly NOT a stream. Each tick opens a fresh capture, hands that single
frame to the callback, and releases the pixels before sleeping. Nothing is
retained between ticks and no persistent capture handle exists, so there is
never a live video feed — to Gemini or to anyone else.

Starts only on an explicit request, shows a persistent badge for its entire
life, and stops itself after `screen_watch_idle_timeout_s` of no activity.
"""

import threading
import time

from app.agent.devlog import devlog


class ScreenWatcher:
    def __init__(self, config, on_frame, *, capture=None, on_indicator=None,
                 interval_s: float = None, idle_timeout_s: float = None):
        self.config = config
        self.on_frame = on_frame          # callback(frame) — frame dies after
        self._capture = capture           # callable() -> VisionFrame
        self.on_indicator = on_indicator  # callback(active: bool) — the badge
        self.interval_s = float(interval_s if interval_s is not None
                                else getattr(config, "screen_watch_interval_s", 1.5))
        self.idle_timeout_s = float(idle_timeout_s if idle_timeout_s is not None
                                    else getattr(config, "screen_watch_idle_timeout_s", 120.0))
        self.frames_captured = 0
        self._stop = threading.Event()
        self._thread = None
        self._last_activity = 0.0
        self._lock = threading.Lock()

    @property
    def watching(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def note_activity(self) -> None:
        """A real interaction — restart the idle countdown."""
        self._last_activity = time.monotonic()

    def start(self) -> bool:
        with self._lock:
            if self.watching:
                return False
            if self._capture is None:
                raise RuntimeError("ScreenWatcher needs a capture callable")
            self._stop.clear()
            self.frames_captured = 0
            self.note_activity()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="anna-screen-watch")
            self._thread.start()
        self._indicate(True)              # persistent badge for the whole life
        devlog.log(f"Vision: screen watching ON "
                   f"(1 frame / {self.interval_s:.1f}s, "
                   f"auto-stop after {self.idle_timeout_s:.0f}s idle)")
        return True

    def stop(self, reason: str = "user") -> None:
        """Idempotent. The badge goes out here and only here."""
        with self._lock:
            was = self.watching
            self._stop.set()
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if was:
            self._indicate(False)
            devlog.log(f"Vision: screen watching OFF ({reason}); "
                       f"{self.frames_captured} frames seen, none kept.")

    def _indicate(self, active: bool) -> None:
        if self.on_indicator is not None:
            try:
                self.on_indicator(active)
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() - self._last_activity > self.idle_timeout_s:
                # Stop from inside the loop: hand off so join() can't self-wait.
                threading.Thread(target=self.stop, args=("idle timeout",),
                                 daemon=True).start()
                return
            frame = None
            try:
                frame = self._capture()          # ONE independent still
                self.frames_captured += 1
                self.on_frame(frame)
            except Exception as e:
                devlog.warn(f"Vision: watch tick failed "
                            f"({' '.join(str(e).split())[:120]})")
            finally:
                if frame is not None:
                    frame.release()              # pixels dropped every tick
            if self._stop.wait(self.interval_s):
                return
