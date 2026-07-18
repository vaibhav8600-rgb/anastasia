"""Phase 1A commit 1: the in-process event bus.

Coalescing/back-pressure LOGIC is tested deterministically with a fake clock
(no sleeping); the threaded delivery/breaker/tee are tested with generous
waits.
"""

import time

from app.core.eventbus import Event, EventBus, _Coalescer


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


# ---- coalescer: the storm-collapsing logic, deterministic --------------------

def test_same_key_storm_collapses_to_the_latest():
    clk = Clock()
    c = _Coalescer(window_ms=200, max_hold_ms=1000, capacity=100, clock=clk)
    c.offer(Event("watch_fs", key="/a", payload={"n": 1}))
    clk.t = 0.05; c.offer(Event("watch_fs", key="/a", payload={"n": 2}))
    clk.t = 0.10; c.offer(Event("watch_fs", key="/a", payload={"n": 3}))
    assert c.ready() == []                     # inside the debounce window
    clk.t = 0.35                               # 0.10 + 0.20 window elapsed
    ready = c.ready()
    assert len(ready) == 1 and ready[0].payload["n"] == 3   # one event, latest


def test_distinct_keys_are_separate_events():
    clk = Clock()
    c = _Coalescer(200, 1000, 100, clk)
    c.offer(Event("watch_fs", key="/a"))
    c.offer(Event("watch_fs", key="/b"))
    clk.t = 0.25
    assert len(c.ready()) == 2


def test_no_key_events_are_ready_immediately():
    clk = Clock()
    c = _Coalescer(200, 1000, 100, clk)
    c.offer(Event("app_switch"))               # key="" — never coalesced
    c.offer(Event("app_switch"))
    assert len(c.ready()) == 2                 # both, right now


def test_max_hold_caps_a_continuously_busy_key():
    clk = Clock()
    c = _Coalescer(200, 1000, 100, clk)
    for i in range(50):                        # bump the same key every 100ms
        clk.t = i * 0.1
        c.offer(Event("watch_fs", key="/a", payload={"n": i}))
    clk.t = 1.05                               # first_seen 0 + max_hold 1.0
    ready = c.ready()
    assert len(ready) == 1, "a busy key must still land within max_hold"


def test_drop_oldest_beyond_capacity_is_counted():
    clk = Clock()
    c = _Coalescer(200, 1000, capacity=3, clock=clk)
    for i in range(5):
        c.offer(Event("watch_fs", key=f"/f{i}"))
    assert c.take_dropped() == 2               # 5 distinct keys, cap 3
    clk.t = 0.25
    assert len(c.ready()) == 3


# ---- bus: threaded delivery / breaker / tee ----------------------------------

def test_publish_delivers_to_a_subscriber():
    bus = EventBus(coalesce_ms=0)
    got = []
    bus.subscribe("s", lambda ev: got.append(ev))
    bus.publish("app_switch", "window", {"app": "chrome"})
    assert _wait(lambda: got)
    assert got[0].type == "app_switch" and got[0].payload["app"] == "chrome"
    bus.close()


def test_storm_over_the_real_bus_coalesces_to_one():
    bus = EventBus(coalesce_ms=40)
    got = []
    bus.subscribe("s", lambda ev: got.append(ev))
    for i in range(200):
        bus.publish("watch_fs", "fs", {"n": i}, key="/same")
    assert _wait(lambda: got)
    time.sleep(0.25)                           # let any straggler land
    assert len(got) == 1 and got[0].payload["n"] == 199
    bus.close()


def test_subscriber_breaker_benches_after_three_and_bus_stays_healthy():
    bus = EventBus(coalesce_ms=0)
    healthy = []
    bus.subscribe("bad", lambda ev: (_ for _ in ()).throw(ValueError("boom")))
    bus.subscribe("good", lambda ev: healthy.append(ev))
    for i in range(3):                         # distinct keys → 3 real deliveries
        bus.publish("app_switch", "w", {}, key=f"k{i}")
    assert _wait(lambda: "bad" in bus.benched)
    assert "bad" in bus.benched                # benched + doctor-visible
    n = len(healthy)
    bus.publish("app_switch", "w", {}, key="more")
    assert _wait(lambda: len(healthy) > n)     # the healthy subscriber runs on
    bus.close()


def test_publish_tees_every_event_to_the_event_log():
    class FakeLog:
        def __init__(self): self.rows = []
        def emit(self, t, source="", **p): self.rows.append((t, source, p))
    log = FakeLog()
    bus = EventBus(eventlog=log, coalesce_ms=0)
    bus.publish("watch_system", "sys", {"kind": "battery_low", "level": 18})
    assert log.rows and log.rows[0][0] == "watch_system"
    assert log.rows[0][2]["kind"] == "battery_low"
    bus.close()


def test_a_bad_eventlog_never_breaks_publish():
    class BoomLog:
        def emit(self, *a, **k): raise RuntimeError("disk full")
    bus = EventBus(eventlog=BoomLog(), coalesce_ms=0)
    got = []
    bus.subscribe("s", lambda ev: got.append(ev))
    bus.publish("app_switch", "w", {})         # tee raises — publish must not
    assert _wait(lambda: got)
    bus.close()


def test_many_publishers_thread_safe():
    import threading
    bus = EventBus(coalesce_ms=0)
    got = []
    bus.subscribe("s", lambda ev: got.append(ev))

    def spam(n):
        for i in range(50):
            bus.publish("app_switch", f"t{n}", {}, key=f"t{n}-{i}")

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert _wait(lambda: len(got) == 400, timeout=5)   # 8×50, none lost
    bus.close()
