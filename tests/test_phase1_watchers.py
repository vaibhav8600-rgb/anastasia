"""Phase 1A commit 2: watcher base (breaker, rate-limit, hysteresis, simulated)
+ the system watcher (per-metric probe → degrade; thresholds via hysteresis)."""

import app.watchers.system as sysmod
from app.watchers.base import Watcher
from app.watchers.system import SystemWatcher
from tests.fakes import make_config


class FakeBus:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    def publish(self, type, source="", payload=None, key=""):
        if self.fail:
            raise RuntimeError("bus down")
        self.published.append({"type": type, "source": source,
                               "payload": dict(payload or {}), "key": key})


class Clk:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


class _W(Watcher):
    name = "test"
    def run(self): pass
    def test_emit(self, kind, *, simulated=True):
        return self.emit("watch_system", {"kind": kind}, simulated=simulated)


# ---- rider 2: hysteresis — no boundary-hover spam ----------------------------

def test_low_side_hysteresis_fires_once_and_rearms_past_margin():
    w = _W(make_config(), FakeBus())
    assert w.crossed_below("disk", 9.5, 10, 12) is True    # crosses in → alert
    assert w.crossed_below("disk", 10.5, 10, 12) is False   # above alert, not re-armed
    assert w.crossed_below("disk", 9.8, 10, 12) is False    # disarmed → NO re-alert (the spam guard)
    assert w.crossed_below("disk", 11.0, 10, 12) is False    # between alert & re-arm
    assert w.crossed_below("disk", 12.5, 10, 12) is False    # re-arms (≥12), not below 10
    assert w.crossed_below("disk", 9.0, 10, 12) is True     # armed again → alert


def test_high_side_hysteresis():
    w = _W(make_config(), FakeBus())
    assert w.crossed_above("ram", 91, 90, 87) is True
    assert w.crossed_above("ram", 92, 90, 87) is False      # disarmed → no spam
    assert w.crossed_above("ram", 89, 90, 87) is False      # above re-arm, still disarmed
    assert w.crossed_above("ram", 86, 90, 87) is False      # re-arms (≤87)
    assert w.crossed_above("ram", 91, 90, 87) is True       # alert again


# ---- base: rate-limit, breaker, simulated tag --------------------------------

def test_rate_limit_throttles_same_kind():
    clk = Clk(); bus = FakeBus()
    w = _W(make_config(), bus, clock=clk)
    assert w.emit("watch_system", {"kind": "x"}, kind="x", min_interval_s=60) is True
    assert w.emit("watch_system", {"kind": "x"}, kind="x", min_interval_s=60) is False
    clk.t = 61
    assert w.emit("watch_system", {"kind": "x"}, kind="x", min_interval_s=60) is True
    assert len(bus.published) == 2


def test_breaker_benches_after_three_failures():
    w = _W(make_config(), FakeBus(fail=True))
    for _ in range(3):
        w.emit("watch_system", {"kind": "x"})
    assert w.benched is True


def test_bench_records_a_doctor_visible_error():
    bus = FakeBus()
    w = _W(make_config(), bus)
    w._bench("sensor exploded")
    errs = [p for p in bus.published if p["type"] == "error"]
    assert errs and errs[0]["payload"]["component"] == "watch-test"
    assert w.benched is True


def test_simulated_tag_only_when_asked():
    bus = FakeBus()
    w = _W(make_config(), bus)
    w.emit("watch_system", {"kind": "a"}, simulated=True)
    w.emit("watch_system", {"kind": "b"})
    assert bus.published[0]["payload"]["simulated"] is True
    assert "simulated" not in bus.published[1]["payload"]


# ---- rider 1: per-metric probe → absent sensors are skipped, not errored ------

def test_absent_metrics_are_never_polled(monkeypatch):
    bus = FakeBus()
    w = SystemWatcher(make_config(), bus,
                      available={"disk": True, "ram": False, "battery": False, "temp": False})
    touched = []
    monkeypatch.setattr(sysmod, "_disk_free_pct", lambda: touched.append("disk") or 55.0)
    monkeypatch.setattr(sysmod, "_ram_used_pct", lambda: touched.append("ram") or 50.0)
    monkeypatch.setattr(sysmod, "_battery", lambda: touched.append("bat") or (50, False))
    w.poll()
    assert touched == ["disk"], "an absent sensor must never be probed"


def test_probe_marks_temp_absent_on_windows():
    m = sysmod.probe_system_metrics()
    assert m["temp"] is False            # no cheap Windows temp API — absent by design
    assert set(m) == {"disk", "ram", "battery", "temp"}


# ---- system watcher: threshold crossings honour hysteresis + rate-limit ------

def test_disk_low_fires_on_crossing_not_on_hover(monkeypatch):
    bus = FakeBus(); clk = Clk()
    w = SystemWatcher(make_config(watch_disk_min_pct=10.0), bus, clock=clk,
                      available={"disk": True, "ram": False, "battery": False, "temp": False})
    vals = iter([8.0, 8.5, 15.0, 7.0])   # low · still low · recover · low again
    monkeypatch.setattr(sysmod, "_disk_free_pct", lambda: next(vals))
    for t in (0, 100, 200, 300):         # advance past the 60s rate-limit each step
        clk.t = t
        w._check_disk()
    lows = [p for p in bus.published if p["payload"].get("kind") == "disk_low"]
    assert len(lows) == 2, "one alert per real crossing — hovering must not spam"
    assert lows[0]["payload"]["value"] == 8.0 and lows[1]["payload"]["value"] == 7.0


def test_system_test_emit_marks_simulated_and_rejects_unknown():
    bus = FakeBus()
    w = SystemWatcher(make_config(), bus,
                      available={"disk": True, "ram": True, "battery": True, "temp": False})
    assert w.test_emit("ram_high", simulated=True) is True
    assert bus.published[-1]["payload"]["simulated"] is True
    assert bus.published[-1]["payload"]["kind"] == "ram_high"
    assert w.test_emit("nonsense") is False


def test_disabled_watcher_does_not_start():
    w = SystemWatcher(make_config(watch_system_enabled=False), FakeBus(),
                      available={"disk": True, "ram": True, "battery": True, "temp": False})
    assert w.start() is False


def test_simulatable_kinds_lists_the_system_events():
    from app.watchers import simulatable_kinds
    ks = simulatable_kinds()
    assert {"disk_low", "ram_high", "battery_low", "battery_full"} <= set(ks)
