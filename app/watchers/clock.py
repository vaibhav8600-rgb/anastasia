"""Clock watcher (Phase 1A commit 4): time-of-day markers.

Fires a `clock{kind: "briefing_time"}` event once per day when the configured
local time passes — the hook the 1B morning briefing will hang on. In 1A it just
lands in the feed/log (no speech).

WALLTIME-based, deliberately: it compares the wall clock to the target and
de-dupes by calendar date. So it survives sleep/resume — waking at 10:00 when
briefing_time was 08:00 fires it once on the next poll (not a monotonic timer
that stalls through sleep, and not a burst of stale firings). `now_fn` is
injectable for tests and the clock-override testing tool.
"""

from datetime import datetime

from app.watchers.base import Watcher


def _parse_hhmm(text, default=(8, 0)):
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return default


class ClockWatcher(Watcher):
    name = "clock"

    def __init__(self, config, bus, *, clock=None, now_fn=None):
        import time as _t
        super().__init__(config, bus, clock=clock or _t.monotonic)
        self._now = now_fn or datetime.now      # wall clock (injectable for tests)
        self._fired_on = {}                      # kind -> date it last fired

    def run(self) -> None:
        interval = float(getattr(self.config, "clock_interval_s", 30.0))
        while not self.sleep(interval):
            self.tick()

    def tick(self) -> None:
        now = self._now()
        h, m = _parse_hhmm(getattr(self.config, "briefing_time", "08:00"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # Fire once per calendar day, the first poll AT or AFTER the target
        # time (so a machine asleep across the target still gets it once on wake).
        if now >= target and self._fired_on.get("briefing_time") != now.date():
            self._fired_on["briefing_time"] = now.date()
            self.emit("clock", {"kind": "briefing_time", "time": f"{h:02d}:{m:02d}"},
                      key="", kind="briefing_time")

    _SIM = {"briefing_time": {"kind": "briefing_time", "time": "08:00"}}

    def test_emit(self, kind, *, simulated=True) -> bool:
        payload = self._SIM.get(kind)
        if payload is None:
            return False
        return self.emit("clock", dict(payload), key="", kind=kind, simulated=simulated)
