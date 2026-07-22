"""Phase 1A commit 4: presence watcher — idle transitions, lock/unlock, the
idle-only degrade, and the real message-only-window monitor (synthetic message,
no real lock)."""

import time

import app.watchers.presence as pm
from app.watchers.presence import PresenceWatcher, SessionMonitor
from tests.fakes import make_config


class FakeBus:
    def __init__(self): self.published = []
    def publish(self, type, source="", payload=None, key=""):
        self.published.append({"type": type, "payload": dict(payload or {}), "key": key})


class FakeMonitor:
    def __init__(self, cb, ok=True): self.cb = cb; self.ok = ok; self.stopped = False
    def start(self): return self.ok
    def stop(self): self.stopped = True


def _pw(bus, monitor_ok=True, **cfg):
    return PresenceWatcher(make_config(**cfg), bus,
                           monitor_factory=lambda cb: FakeMonitor(cb, monitor_ok))


def _kinds(bus):
    return [e["payload"]["kind"] for e in bus.published]


def test_idle_then_return_transitions(monkeypatch):
    bus = FakeBus()
    w = _pw(bus, presence_idle_minutes=5.0)
    seq = iter([60, 6 * 60, 6 * 60, 60])          # idle seconds per poll
    monkeypatch.setattr(pm, "idle_seconds", lambda: next(seq))
    w._poll_idle()      # 1 min → present, nothing
    w._poll_idle()      # 6 min → user_idle
    w._poll_idle()      # 6 min → still away, no repeat
    w._poll_idle()      # 1 min → user_returned
    assert _kinds(bus) == ["user_idle", "user_returned"]
    assert bus.published[0]["payload"]["minutes"] == 6.0


def test_lock_overrides_idle(monkeypatch):
    bus = FakeBus()
    w = _pw(bus)
    monkeypatch.setattr(pm, "idle_seconds", lambda: 10 * 60)   # very idle
    w._on_session("locked")
    assert w.state == "locked"
    n = len(bus.published)
    w._poll_idle()                                # locked → idle poll is a no-op
    assert len(bus.published) == n
    w._on_session("unlocked")
    assert w.state == "present"
    assert _kinds(bus) == ["session_locked", "session_unlocked"]


def test_idle_only_degrade_never_crashes():
    bus = FakeBus()
    w = _pw(bus, monitor_ok=False)
    mon = w._monitor_factory(w._on_session)
    assert mon.start() is False                   # lock monitor unavailable...
    w._monitor = None                             # ...run() degrades to idle-only
    w._poll_idle()                                # must not raise


def test_real_session_monitor_dispatches_synthetic_lock():
    got = []
    m = SessionMonitor(lambda what: got.append(what), register=False)
    assert m.start() is True
    try:
        m.post_synthetic(True)
        m.post_synthetic(False)
        end = time.time() + 2
        while time.time() < end and len(got) < 2:
            time.sleep(0.02)
    finally:
        m.stop()
    assert got == ["locked", "unlocked"]


def test_presence_test_emit():
    bus = FakeBus()
    w = _pw(bus)
    assert w.test_emit("session_locked", simulated=True) is True
    assert bus.published[-1]["payload"]["simulated"] is True
    assert w.test_emit("not_a_kind") is False
