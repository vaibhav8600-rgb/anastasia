"""Active-window watcher (Phase 1A commit 3): the foreground app, pure ctypes.

TITLE POLICY (recorded, and enforced — not merely undisplayed):
  * The PROCESS NAME is always captured (chrome.exe, Code.exe).
  * The window TITLE is captured ONLY when `watch_window_titles` is on. Titles
    carry document names, email subjects, URLs — so title capture is opt-in
    (default OFF). With it off, the title is absent from the payload, from the
    event log, AND from the coalescing key (the key is always the process name).
    `test_phase1_active_window.py` proves that negative with a payload audit.

Emits `app_switch` (foreground app changed) and, after `watch_focus_minutes` in
one app, `focus_session{app, minutes}`.
"""

import ctypes
import os
from ctypes import wintypes

from app.watchers.base import Watcher

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_name(pid: int) -> str:
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def _window_title(hwnd) -> str:
    try:
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def _foreground_app():
    """(process_basename, title) or None if there's no foreground window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = _process_name(pid.value)
    if not name:
        return None
    return name, _window_title(hwnd)


class ActiveWindowWatcher(Watcher):
    name = "window"

    def __init__(self, config, bus, *, clock=None):
        import time as _t
        super().__init__(config, bus, clock=clock or _t.monotonic)
        self._current = None
        self._since = None
        self._focus_emitted = False

    def _titles_on(self) -> bool:
        return bool(getattr(self.config, "watch_window_titles", False))

    def run(self) -> None:
        interval = float(getattr(self.config, "watch_window_interval_s", 5.0))
        while not self.sleep(interval):
            self.poll()

    def poll(self) -> None:
        app = _foreground_app()
        if app is None:
            return
        name, title = app
        if name != self._current:
            self._on_switch(name, title)
        else:
            self._check_focus(name)

    def _on_switch(self, name, title) -> None:
        self._current = name
        self._since = self._clock()
        self._focus_emitted = False
        payload = {"app": name}
        if self._titles_on() and title:          # the ONLY place a title is added
            payload["title"] = title
        self.emit("app_switch", payload, key=name, kind="app_switch")

    def _check_focus(self, name) -> None:
        if self._focus_emitted or self._since is None:
            return
        minutes = (self._clock() - self._since) / 60.0
        if minutes >= float(getattr(self.config, "watch_focus_minutes", 20.0)):
            self._focus_emitted = True
            self.emit("focus_session", {"app": name, "minutes": round(minutes, 1)},
                      key=name, kind="focus_session")

    _SIM = {
        "app_switch": {"app": "chrome.exe"},
        "focus_session": {"app": "Code.exe", "minutes": 25.0},
    }

    def test_emit(self, kind, *, simulated=True) -> bool:
        payload = self._SIM.get(kind)
        if payload is None:
            return False
        return self.emit(kind, dict(payload), key=payload.get("app", ""),
                         simulated=simulated)
