"""System watcher (Phase 1A commit 2): disk / RAM / battery, pure ctypes.

No new dependency (see DECISIONS D-1.1 — chosen over psutil to stay zero-dep and
avoid packaging a C extension). Each metric is PROBED once at startup on the
real hardware; an absent sensor degrades to absent-and-doctor-noted and is
never polled again (no recurring error). CPU temperature has no cheap
documented Windows API, so it is absent by design here.

Thresholds fire through the base's hysteresis (disk <10% alerts, re-arms >12%),
so a value hovering on the boundary can't spam the feed.
"""

import ctypes

from app.agent.devlog import devlog
from app.watchers.base import Watcher

METRICS = ("disk", "ram", "battery", "temp")


# ---- raw readers (Windows kernel32; None when unavailable) --------------------

def _disk_free_pct(path: str = "C:\\"):
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(path), None, ctypes.byref(total), ctypes.byref(free))
        if not ok or total.value == 0:
            return None
        return free.value / total.value * 100.0
    except Exception:
        return None


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _ram_used_pct():
    try:
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return float(m.dwMemoryLoad)          # % physical RAM in use
    except Exception:
        return None


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def _battery():
    """(percent, plugged) or None when there's no battery / it's unknown."""
    try:
        s = _SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return None
        pct = s.BatteryLifePercent & 0xFF
        if pct == 255:                        # unknown / desktop
            return None
        return pct, (s.ACLineStatus == 1)
    except Exception:
        return None


def probe_system_metrics() -> dict:
    """{metric: available} — read once at startup AND by --doctor (fresh probe,
    always accurate, no cross-process state)."""
    return {
        "disk": _disk_free_pct() is not None,
        "ram": _ram_used_pct() is not None,
        "battery": _battery() is not None,
        "temp": False,                        # no cheap documented Windows API
    }


class SystemWatcher(Watcher):
    name = "system"

    def __init__(self, config, bus, *, clock=None, available=None):
        import time as _t
        super().__init__(config, bus, clock=clock or _t.monotonic)
        self.available = available if available is not None else probe_system_metrics()
        absent = sorted(m for m, ok in self.available.items() if not ok)
        if absent:
            devlog.log(f"system watcher: sensors absent, skipped: {absent}")

    def run(self) -> None:
        interval = float(getattr(self.config, "watch_system_interval_s", 30.0))
        while not self.sleep(interval):
            self.poll()

    def poll(self) -> None:
        if self.available.get("disk"):
            self._check_disk()
        if self.available.get("ram"):
            self._check_ram()
        if self.available.get("battery"):
            self._check_battery()
        # temp: absent on Windows — nothing to poll.

    def _check_disk(self) -> None:
        pct = _disk_free_pct()
        if pct is None:
            return
        lo = float(getattr(self.config, "watch_disk_min_pct", 10.0))
        if self.crossed_below("disk", pct, lo, lo + 2.0):
            self.emit("watch_system", {"kind": "disk_low", "value": round(pct, 1)},
                      key="disk", kind="disk_low", min_interval_s=60)

    def _check_ram(self) -> None:
        pct = _ram_used_pct()
        if pct is None:
            return
        hi = float(getattr(self.config, "watch_ram_max_pct", 90.0))
        if self.crossed_above("ram", pct, hi, hi - 3.0):
            self.emit("watch_system", {"kind": "ram_high", "value": round(pct, 1)},
                      key="ram", kind="ram_high", min_interval_s=60)

    def _check_battery(self) -> None:
        b = _battery()
        if b is None:
            return
        pct, plugged = b
        lo = float(getattr(self.config, "watch_battery_min_pct", 20.0))
        if not plugged and self.crossed_below("battery", pct, lo, lo + 5.0):
            self.emit("watch_system", {"kind": "battery_low", "value": pct},
                      key="battery", kind="battery_low", min_interval_s=120)
        elif plugged and self.crossed_above("battery", pct, 99.0, 95.0):
            self.emit("watch_system", {"kind": "battery_full", "value": pct},
                      key="battery", kind="battery_full", min_interval_s=300)

    _SIM = {
        "disk_low": {"kind": "disk_low", "value": 7.0},
        "ram_high": {"kind": "ram_high", "value": 93.0},
        "battery_low": {"kind": "battery_low", "value": 15},
        "battery_full": {"kind": "battery_full", "value": 100},
    }

    def test_emit(self, kind, *, simulated=True) -> bool:
        payload = self._SIM.get(kind)
        if payload is None:
            return False
        return self.emit("watch_system", dict(payload), key="",
                         kind=kind, simulated=simulated)
