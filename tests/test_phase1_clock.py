"""Phase 1A commit 4: clock watcher — fires the daily briefing marker once,
de-dupes by date, and survives sleep-across-the-target (walltime, not a stalled
monotonic timer)."""

from datetime import datetime

from app.watchers.clock import ClockWatcher
from tests.fakes import make_config


class FakeBus:
    def __init__(self): self.published = []
    def publish(self, type, source="", payload=None, key=""):
        self.published.append({"type": type, "payload": dict(payload or {}), "key": key})


def _clock(bus, box, **cfg):
    cfg.setdefault("briefing_time", "08:00")
    return ClockWatcher(make_config(**cfg), bus, now_fn=lambda: box[0])


def _briefs(bus):
    return [e for e in bus.published if e["payload"]["kind"] == "briefing_time"]


def test_fires_once_at_the_time_then_not_again_same_day():
    bus = FakeBus(); box = [datetime(2026, 7, 20, 7, 59)]
    w = _clock(bus, box)
    w.tick()                                        # 07:59 → no
    assert _briefs(bus) == []
    box[0] = datetime(2026, 7, 20, 8, 0); w.tick()  # 08:00 → fire
    box[0] = datetime(2026, 7, 20, 8, 5); w.tick()  # 08:05 same day → no repeat
    box[0] = datetime(2026, 7, 20, 23, 0); w.tick()
    assert len(_briefs(bus)) == 1


def test_next_day_fires_again():
    bus = FakeBus(); box = [datetime(2026, 7, 20, 8, 1)]
    w = _clock(bus, box)
    w.tick()                                        # day 1 → fire
    box[0] = datetime(2026, 7, 21, 8, 1); w.tick()  # day 2 → fire again
    assert len(_briefs(bus)) == 2


def test_sleep_across_the_target_fires_once_on_wake():
    bus = FakeBus(); box = [datetime(2026, 7, 20, 7, 0)]
    w = _clock(bus, box)
    w.tick()                                        # 07:00 → no
    box[0] = datetime(2026, 7, 20, 10, 30); w.tick()  # asleep past 08:00, wake 10:30 → one
    assert len(_briefs(bus)) == 1
    assert _briefs(bus)[0]["payload"]["time"] == "08:00"


def test_before_the_target_nothing():
    bus = FakeBus(); box = [datetime(2026, 7, 20, 6, 0)]
    _clock(bus, box).tick()
    assert _briefs(bus) == []


def test_clock_test_emit():
    bus = FakeBus()
    w = ClockWatcher(make_config(), bus)
    assert w.test_emit("briefing_time", simulated=True) is True
    assert bus.published[-1]["payload"]["simulated"] is True
    assert w.test_emit("nope") is False
