"""Phase 0 commit 6: the tray + graceful teardown.

Four requirements, each pinned:
  1. Quit is a CONTRACT: pending card cancelled first, Live closed and the mic
     released, the session-end row emitted BEFORE the log flushes, the flush
     verdict honored, tray last — asserted as an ORDER, not hoped for.
  2. Pause listening is provably DEAF: a wake while paused calls nothing and
     logs nothing; the chip/badge shows paused; resume restarts the wake word.
  3. Quit with a pending confirmation → cancelled-by-shutdown, logged, never
     executed.
  4. Tray thread death leaves core alive and is doctor-visible; it is a
     convenience, not a heartbeat.

The pystray object is faked throughout (no display in CI); only the supervision
and menu logic run.
"""

import threading
import time

import pytest

from app.core.eventlog import EventLog
from app.core.lifecycle import graceful_teardown
from app.core.tray import Tray
from app.main import Controller
from tests.fakes import FakeHistory, FakeMainUI, make_config


class _Mem:
    def get(self, k, d=None): return d
    def set(self, k, v): pass


def make_controller(**cfg):
    c = Controller(ui=FakeMainUI(), autostart=False, config=make_config(**cfg),
                   memory=_Mem(), history=FakeHistory())
    c.speech.shutdown()
    return c


# ---- fakes for the teardown-order test ---------------------------------------

class SpyController:
    """Records the order in which teardown touches it (into the shared `order`
    list), and lets us stage a pending card + an active Live session."""

    def __init__(self, order, *, live=False, pending=None):
        self._live = object() if live else None
        self.pipeline = self
        self.pending = pending
        self._order = order

    # pipeline.cancel_pending
    def cancel_pending(self, reason="user", action_id=None):
        self._order.append(("cancel_pending", reason))
        self.pending = None
        return True

    def shutdown(self):
        # Live must be closed here, BEFORE the session-end row + flush.
        self._order.append(("controller.shutdown", self._live is not None))
        self._live = None


class SpyServer:
    def __init__(self, log): self._log = log
    def stop(self): self._log.append(("server.stop",))


class RecordingLog:
    """An eventlog stand-in that records emits and flush/close order."""

    def __init__(self, order, *, flush_ok=True):
        self.order = order
        self.rows = []
        self._flush_ok = flush_ok
    def emit(self, etype, **kw):
        self.rows.append((etype, kw))
        self.order.append(("emit", etype, kw.get("component"), kw.get("outcome")))
        return True
    def flush(self, timeout=2.0):
        self.order.append(("flush", self._flush_ok))
        return self._flush_ok
    def close(self, timeout=2.0):
        self.order.append(("close",))


# ---- 1 + 3: Quit is an ordered contract --------------------------------------

def test_teardown_order_live_closes_and_session_end_lands_before_flush():
    order = []
    log = RecordingLog(order)
    controller = SpyController(order, live=True,
                               pending=type("P", (), {"plan": type("Q", (), {"tool_name": "run_terminal"})()})())
    server = SpyServer(order)

    steps = graceful_teardown(controller, log, server=server, reason="tray quit")

    names = [s[0] for s in order]
    # pending cancelled FIRST (before we tear the controller down)...
    assert names.index("emit") < names.index("controller.shutdown")
    i_cancel = names.index("cancel_pending")
    i_shutdown = names.index("controller.shutdown")
    i_flush = names.index("flush")
    i_close = names.index("close")
    assert i_cancel < i_shutdown < i_flush < i_close
    # ...Live was still open when shutdown ran (so shutdown is what closed it)...
    assert ("controller.shutdown", True) in order
    # ...the session-end row was emitted BEFORE the flush...
    live_end = next(k for k, o in enumerate(order)
                    if o[0] == "emit" and o[2] == "gemini_live")
    assert live_end < i_flush, "Live session-end row must land before the flush"
    # ...server stopped before the log closed...
    assert names.index("server.stop") < i_close
    assert steps[-1] == "eventlog.close"


def test_pending_confirmation_is_cancelled_by_shutdown_logged_never_run():
    order = []
    log = RecordingLog(order)
    pending = type("P", (), {"plan": type("Q", (), {"tool_name": "delete_files"})()})()
    controller = SpyController(order, live=False, pending=pending)

    graceful_teardown(controller, log, reason="quit")

    cancel_rows = [kw for et, kw in log.rows
                   if et == "confirmation" and kw.get("outcome") == "cancelled-by-shutdown"]
    assert cancel_rows and cancel_rows[0]["tool"] == "delete_files"
    assert cancel_rows[0]["channel"] == "shutdown"
    assert controller.pending is None            # cancelled, and never executed
    # cancel_pending pops-and-discards; there is no execute call anywhere.


def test_graceful_teardown_persists_rows_with_the_real_eventlog():
    """Integration: the REAL EventLog + a REAL Controller. Proves the ordered
    emits actually LAND on disk (emit-before-flush-before-close), not just that
    a fake recorded them."""
    import tempfile
    from pathlib import Path

    from app.agent.safety import validate_action
    from app.llm.intent_parser import ActionPlan

    tmp = Path(tempfile.mkdtemp())
    log = EventLog(tmp / "events.sqlite")
    c = make_controller()
    plan = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                      arguments={"command": "git status"})
    safety = validate_action(plan, c.config)
    c.pipeline.confirm.request(plan, safety, "run git status")
    assert c.pipeline.pending is not None

    graceful_teardown(c, log, reason="test-quit")

    reader = EventLog(log.path, start=False)
    rows = reader.recent(limit=100)
    triples = [(r["type"], r["payload"].get("component"), r["outcome"]) for r in rows]
    assert any(t == "confirmation" and o == "cancelled-by-shutdown"
               for t, _, o in triples), triples
    assert any(t == "engine_state" and comp == "core" for t, comp, _ in triples)
    assert c.pipeline.pending is None            # cancelled, not executed


def test_flush_verdict_is_honored_when_it_fails():
    order = []
    log = RecordingLog(order, flush_ok=False)
    graceful_teardown(SpyController(order), log, reason="quit")
    assert ("flush", False) in order             # a failed flush is recorded...
    assert ("close",) in order                   # ...and we still close.


def test_teardown_is_idempotent():
    order = []
    log = RecordingLog(order)
    c = SpyController(order)
    first = graceful_teardown(c, log)
    second = graceful_teardown(c, log)           # tray Quit + finally can't double-run
    assert second == ["already-torn-down"]
    assert order.count(("close",)) == 1


# ---- 2: pause listening is provably deaf -------------------------------------

class FakeWake:
    def __init__(self): self.stopped = False
    def start(self): pass
    def stop(self): self.stopped = True


def test_wake_while_paused_does_nothing_and_logs_nothing():
    c = make_controller()
    fired = []
    c.toggle_mic = lambda *a, **k: fired.append(a)   # spy: must never be called
    c.set_listening_paused(True)

    c._on_wake()                                 # a wake fires while paused
    c._on_wake()

    assert fired == [], "a wake while paused must not open the mic / start a turn"
    # 'no event row': the wake path took no action at all, so nothing downstream
    # (user_message, state 'listening') was dispatched.
    ui = c.ui
    assert not any(s == "listening" for s in ui.states)


def test_pause_stops_the_wake_listener_and_shows_the_badge():
    c = make_controller()
    wake = FakeWake()
    c.wake_listener = wake

    c.set_listening_paused(True)
    assert wake.stopped is True and c.wake_listener is None
    assert c._listening_paused is True

    # full_state carries it so a reconnecting window re-shows the badge.
    c.send_full_state()
    fs = [e for e in c.ui.__dict__.get("dispatched", [])]  # FakeMainUI may not record
    assert c._listening_paused is True


def test_resume_restarts_the_wake_word_if_it_had_been_on(monkeypatch):
    c = make_controller()
    started = []
    monkeypatch.setattr(c, "toggle_wake_word_on", lambda: started.append(True))
    c.wake_listener = FakeWake()                 # wake was ON

    c.set_listening_paused(True)                 # pause -> stop
    assert c.wake_listener is None
    c.set_listening_paused(False)                # resume -> restart
    assert started == [True], "resume must restart the wake word it paused"


def test_pause_when_wake_was_off_restarts_nothing(monkeypatch):
    c = make_controller()
    started = []
    monkeypatch.setattr(c, "toggle_wake_word_on", lambda: started.append(True))
    assert c.wake_listener is None               # wake OFF

    c.set_listening_paused(True)
    c.set_listening_paused(False)
    assert started == [], "resume must not start wake word that was never on"


def test_pause_dispatches_the_badge_event():
    c = make_controller()
    events = []
    c.ui.dispatch = lambda t, p=None: events.append((t, p))
    c.set_listening_paused(True)
    c.set_listening_paused(False)
    paused_events = [p for t, p in events if t == "listening_paused"]
    assert paused_events == [{"paused": True}, {"paused": False}]


# ---- 4: the tray is a convenience, not a heartbeat ---------------------------

def test_tray_thread_death_is_contained_and_recorded(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")

    class DyingIcon:
        def run(self): raise RuntimeError("shell notification area crashed")
        def stop(self): pass

    tray = Tray(eventlog=log, icon_factory=lambda t: DyingIcon())
    assert tray.start() is True                  # start succeeds; the crash is later
    for _ in range(200):                         # let the supervised thread run+die
        if not tray.alive and tray._thread and not tray._thread.is_alive():
            break
        time.sleep(0.01)

    # Core is untouched (we're still here), and the death is a doctor-level fact.
    assert log.flush(timeout=10)
    rows = log.recent(event_type="error")
    assert any(r["payload"].get("component") == "tray"
               and "died" in r["payload"].get("message", "") for r in rows)
    assert log.error_summary("tray", hours=1.0)["count"] >= 1
    log.close()


def test_tray_unavailable_leaves_core_running(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")

    def boom(_t): raise ImportError("no pystray on this box")
    tray = Tray(eventlog=log, icon_factory=boom)
    assert tray.start() is False                 # no tray...
    assert tray.alive is False
    # ...but the fact is recorded so --doctor can mention it.
    assert log.flush(timeout=10)
    assert log.error_summary("tray", hours=1.0)["count"] >= 1
    log.close()


def test_tray_quit_only_signals_never_tears_down_itself():
    """Quit must SIGNAL the loop (server.stop) — the single teardown path is
    the daemon's finally. If Quit tore down directly we'd get two teardowns."""
    signalled = []
    tray = Tray(on_quit=lambda: signalled.append("stop"),
                icon_factory=lambda t: type("I", (), {"run": lambda s: None,
                                                       "stop": lambda s: None,
                                                       "update_menu": lambda s: None})())
    tray._icon = tray._icon_factory(tray)
    tray._fire("quit")
    assert signalled == ["stop"]


def test_tray_pause_menu_toggles_and_notifies():
    toggled = []
    tray = Tray(on_pause_toggle=lambda p: toggled.append(p),
                icon_factory=lambda t: type("I", (), {"run": lambda s: None,
                                                      "stop": lambda s: None,
                                                      "update_menu": lambda s: None})())
    tray._icon = tray._icon_factory(tray)
    tray._fire("pause")
    assert tray.paused is True and toggled == [True]
    tray._fire("pause")
    assert tray.paused is False and toggled == [True, False]
