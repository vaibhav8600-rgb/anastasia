"""Presence watcher (Phase 1A commit 4): idle/away + screen lock/unlock.

Two signals, both pure ctypes (zero deps):
  * **Idle/away** — `GetLastInputInfo` polled; emits `user_idle{minutes}` when
    input has been quiet past the threshold and `user_returned` when it resumes.
  * **Lock/unlock** — a message-only window on its own pump thread receives
    `WM_WTSSESSION_CHANGE` via `WTSRegisterSessionNotification` (proven headless
    in the Phase-1 spike). Emits `session_locked` / `session_unlocked`.

If the lock monitor can't set up (WTS registration fails on some box), it
degrades to **idle-only** — a locked screen is then just "away" (still silent,
just coarser) — and never benches core. `self.state` (present/away/locked) is
exposed for the 1B interruption policy; lock overrides idle.

ctypes note: every Win32 call has an explicit restype/argtypes. On 64-bit,
ctypes defaults an unset restype to 32-bit `int`, which TRUNCATES a returned
handle/pointer — a latent access violation that only fires when ASLR places the
module high enough (it did, under pytest). The prototypes below are mandatory,
not decoration.
"""

import ctypes
import threading
from ctypes import wintypes

from app.agent.devlog import devlog
from app.watchers.base import Watcher

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
WM_QUIT = 0x0012
HWND_MESSAGE = wintypes.HWND(-3)
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)
_class_seq = 0
_class_lock = threading.Lock()

# ---- ctypes prototypes (set ONCE; 64-bit-safe) -------------------------------
_WIN = True
try:
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    except Exception:
        _wts = None

    _u32.DefWindowProcW.restype = _LRESULT
    _u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.CreateWindowExW.restype = wintypes.HWND
    _u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _u32.DestroyWindow.argtypes = [wintypes.HWND]
    _u32.RegisterClassW.restype = ctypes.c_ushort            # ATOM
    _u32.RegisterClassW.argtypes = [ctypes.c_void_p]
    _u32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _u32.GetMessageW.restype = ctypes.c_int
    _u32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _u32.TranslateMessage.argtypes = [ctypes.c_void_p]
    _u32.DispatchMessageW.restype = _LRESULT
    _u32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    _u32.PostMessageW.restype = wintypes.BOOL
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.GetLastInputInfo.restype = wintypes.BOOL
    _u32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
    _k32.GetModuleHandleW.restype = wintypes.HMODULE
    _k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _k32.GetTickCount.restype = wintypes.DWORD
    if _wts is not None:
        _wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
        _wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        _wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
except Exception:                    # non-Windows / no Win32 — degrade, don't crash import
    _WIN = False
    _u32 = _k32 = _wts = None


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def idle_seconds():
    """Seconds since the last keyboard/mouse input (0.0 if unavailable)."""
    if not _WIN:
        return 0.0
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not _u32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return max(0.0, (_k32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


class SessionMonitor:
    """Message-only window + WTS session notifications. `on_session('locked' |
    'unlocked')` fires from the pump thread. `register=False` (tests) skips the
    real WTS registration so synthetic messages can drive the WndProc."""

    def __init__(self, on_session, *, register=True):
        self._on = on_session
        self._register = register
        self._thread = None
        self._hwnd = None
        self._wndproc = None          # STRONG ref — GC of this crashes the pump
        self._cls_name = None
        self._hinst = None
        self._ready = threading.Event()
        self.ok = False

    def start(self) -> bool:
        if not _WIN:
            self.ok = False
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="anna-presence-wts")
        self._thread.start()
        self._ready.wait(2.0)
        return self.ok

    def _pump(self) -> None:
        global _class_seq
        try:
            def _proc(hwnd, msg, wparam, lparam):
                if msg == WM_WTSSESSION_CHANGE:
                    if wparam == WTS_SESSION_LOCK:
                        self._safe("locked")
                    elif wparam == WTS_SESSION_UNLOCK:
                        self._safe("unlocked")
                return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = _WNDPROC(_proc)
            self._hinst = _k32.GetModuleHandleW(None)
            with _class_lock:
                _class_seq += 1
                cls_name = f"AnnaPresence{_class_seq}"
            wc = _WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = self._hinst
            wc.lpszClassName = cls_name
            if not _u32.RegisterClassW(ctypes.byref(wc)):
                raise OSError(f"RegisterClassW failed ({ctypes.get_last_error()})")
            self._cls_name = cls_name
            hwnd = _u32.CreateWindowExW(0, cls_name, "anna-presence", 0, 0, 0, 0, 0,
                                        HWND_MESSAGE, None, self._hinst, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            self._hwnd = hwnd
            if self._register and _wts is not None:
                _wts.WTSRegisterSessionNotification(hwnd, 0)   # NOTIFY_FOR_THIS_SESSION
            self.ok = True
            self._ready.set()

            msg = wintypes.MSG()
            while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _u32.TranslateMessage(ctypes.byref(msg))
                _u32.DispatchMessageW(ctypes.byref(msg))

            if self._register and _wts is not None:
                _wts.WTSUnRegisterSessionNotification(hwnd)
            _u32.DestroyWindow(hwnd)
            _u32.UnregisterClassW(cls_name, self._hinst)
        except Exception as e:
            devlog.warn(f"presence lock monitor unavailable ({e}) — idle-only.")
            self.ok = False
            self._ready.set()

    def _safe(self, what) -> None:
        try:
            self._on(what)
        except Exception:
            pass

    def post_synthetic(self, locked: bool) -> None:
        """Test hook: drive the WndProc without touching the real screen."""
        if self._hwnd and _WIN:
            _u32.PostMessageW(self._hwnd, WM_WTSSESSION_CHANGE,
                              WTS_SESSION_LOCK if locked else WTS_SESSION_UNLOCK, 0)

    def stop(self) -> None:
        if self._hwnd and _WIN:
            try:
                _u32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(1.0)


class PresenceWatcher(Watcher):
    name = "presence"

    def __init__(self, config, bus, *, clock=None, monitor_factory=None):
        import time as _t
        super().__init__(config, bus, clock=clock or _t.monotonic)
        self.state = "present"
        self._idle = False
        self._monitor = None
        self._monitor_factory = monitor_factory or (lambda cb: SessionMonitor(cb))

    def run(self) -> None:
        self._monitor = self._monitor_factory(self._on_session)
        if not self._monitor.start():
            self._monitor = None          # idle-only degrade; core untouched
        interval = float(getattr(self.config, "presence_interval_s", 15.0))
        while not self.sleep(interval):
            self._poll_idle()
        if self._monitor is not None:
            self._monitor.stop()

    def _poll_idle(self) -> None:
        if self.state == "locked":
            return                        # lock overrides idle; wait for unlock
        idle_min = idle_seconds() / 60.0
        threshold = float(getattr(self.config, "presence_idle_minutes", 5.0))
        if not self._idle and idle_min >= threshold:
            self._idle = True
            self.state = "away"
            self.emit("presence", {"kind": "user_idle", "minutes": round(idle_min, 1)},
                      key="", kind="user_idle")
        elif self._idle and idle_min < threshold:
            self._idle = False
            self.state = "present"
            self.emit("presence", {"kind": "user_returned"}, key="", kind="user_returned")

    def _on_session(self, what) -> None:
        if what == "locked":
            self.state = "locked"
            self._idle = True
            self.emit("presence", {"kind": "session_locked"}, key="", kind="session_locked")
        elif what == "unlocked":
            self.state = "present"
            self._idle = False
            self.emit("presence", {"kind": "session_unlocked"}, key="", kind="session_unlocked")

    _SIM = {
        "user_idle": {"kind": "user_idle", "minutes": 12.0},
        "user_returned": {"kind": "user_returned"},
        "session_locked": {"kind": "session_locked"},
        "session_unlocked": {"kind": "session_unlocked"},
    }

    def test_emit(self, kind, *, simulated=True) -> bool:
        payload = self._SIM.get(kind)
        if payload is None:
            return False
        return self.emit("presence", dict(payload), key="", kind=kind,
                         simulated=simulated)


def lock_detection_available() -> bool:
    """For --doctor: can we set up the WTS lock monitor on this box?"""
    if not _WIN:
        return False
    m = SessionMonitor(lambda _w: None, register=False)
    try:
        return m.start()
    finally:
        m.stop()
