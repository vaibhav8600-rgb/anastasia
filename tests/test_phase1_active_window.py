"""Phase 1A commit 3: the active-window title policy — proving the NEGATIVE.

With title capture OFF (the default), a window title must be absent from the bus
payload, from the coalescing key, AND from the event log — not merely
undisplayed. The process name is always fine.
"""

import app.watchers.active_window as awmod
from app.core.eventbus import EventBus
from app.core.eventlog import EventLog
from app.watchers.active_window import ActiveWindowWatcher
from tests.fakes import make_config

SENSITIVE = "RE: Q3 layoffs — CONFIDENTIAL do-not-forward"


class FakeBus:
    def __init__(self): self.published = []
    def publish(self, type, source="", payload=None, key=""):
        self.published.append({"type": type, "payload": dict(payload or {}), "key": key})


def _watch(titles, bus, monkeypatch, title=SENSITIVE, app="outlook.exe"):
    w = ActiveWindowWatcher(make_config(watch_window_titles=titles), bus)
    monkeypatch.setattr(awmod, "_foreground_app", lambda: (app, title))
    return w


# ---- the negative: titles off → title nowhere -------------------------------

def test_title_absent_from_payload_and_key_when_disabled(monkeypatch):
    bus = FakeBus()
    _watch(False, bus, monkeypatch).poll()
    ev = bus.published[-1]
    assert ev["type"] == "app_switch"
    assert ev["payload"] == {"app": "outlook.exe"}          # ONLY the process name
    assert "title" not in ev["payload"]
    assert "CONFIDENTIAL" not in str(ev["payload"])
    assert ev["key"] == "outlook.exe"                       # coalesce key is the process
    assert "layoffs" not in ev["key"]


def test_title_absent_from_the_event_log_when_disabled(tmp_path, monkeypatch):
    """Payload audit through the REAL event log (M0.5 whole-directory grep)."""
    log = EventLog(tmp_path / "events.sqlite")
    bus = EventBus(eventlog=log, coalesce_ms=0)
    _watch(False, bus, monkeypatch).poll()
    assert log.flush(timeout=10)
    log.close()
    body = "".join(f.read_bytes().decode("utf-8", "ignore")
                   for f in tmp_path.iterdir() if f.is_file())
    assert "CONFIDENTIAL" not in body and "layoffs" not in body
    assert "outlook.exe" in body                            # the process name DID land


# ---- the positive: opt in → title present, key still the process ------------

def test_title_present_when_enabled_but_key_is_still_the_process(monkeypatch):
    bus = FakeBus()
    _watch(True, bus, monkeypatch, title="Inbox — Outlook").poll()
    ev = bus.published[-1]
    assert ev["payload"]["title"] == "Inbox — Outlook"
    assert ev["payload"]["app"] == "outlook.exe"
    assert ev["key"] == "outlook.exe"                       # NEVER the title, even when on


# ---- app_switch / focus_session behaviour -----------------------------------

def test_app_switch_only_on_change(monkeypatch):
    bus = FakeBus()
    w = ActiveWindowWatcher(make_config(watch_window_titles=False), bus)
    seq = iter([("chrome.exe", ""), ("chrome.exe", ""), ("code.exe", "")])
    monkeypatch.setattr(awmod, "_foreground_app", lambda: next(seq))
    w.poll(); w.poll(); w.poll()
    switches = [e for e in bus.published if e["type"] == "app_switch"]
    assert [e["payload"]["app"] for e in switches] == ["chrome.exe", "code.exe"]


def test_focus_session_after_the_threshold(monkeypatch):
    bus = FakeBus()
    clk = type("C", (), {"t": 0.0})()
    w = ActiveWindowWatcher(make_config(watch_focus_minutes=20.0, watch_window_titles=False),
                            bus, clock=lambda: clk.t)
    monkeypatch.setattr(awmod, "_foreground_app", lambda: ("code.exe", ""))
    clk.t = 0;      w.poll()                       # switch → in code.exe
    clk.t = 10 * 60; w.poll()                      # 10 min — not yet
    assert not [e for e in bus.published if e["type"] == "focus_session"]
    clk.t = 21 * 60; w.poll()                      # past 20 min → one focus_session
    focus = [e for e in bus.published if e["type"] == "focus_session"]
    assert len(focus) == 1 and focus[0]["payload"]["app"] == "code.exe"
    clk.t = 40 * 60; w.poll()                      # still in code — not emitted twice
    assert len([e for e in bus.published if e["type"] == "focus_session"]) == 1
