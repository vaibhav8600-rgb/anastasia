"""Phase 1A commit 5: local salience rules.

Rules only — score every event so the feed shows a NUMBER (and WHICH rule) to
calibrate against. No cloud, no routing, no speech. The three riders are the
contract this file locks down:

  1. Provenance — score() returns which rule fired, stamped onto the event.
  2. Fail-safe hot-reload — a malformed edit keeps the last-good table, is
     doctor-visible, and never crashes or silently zeroes.
  3. Unknown kinds default to score 2 (log-only), fail quiet.

Plus the payload-audit guarantee: scoring an event and teeing it to the log
adds ONLY score/rule — never a path, never a window title.
"""

from types import SimpleNamespace

import pytest

from app.core.eventbus import Event, EventBus
from app.core.eventlog import EventLog
from app.proactive.salience import DEFAULT_SCORE, SalienceEngine


def _ev(type, **payload):
    return SimpleNamespace(type=type, payload=payload)


def _engine(tmp_path, toml, **kw):
    p = tmp_path / "salience_rules.toml"
    p.write_text(toml, encoding="utf-8")
    return SalienceEngine(path=p, seed=False, **kw)


# --- rider 3 + basic scoring -------------------------------------------------

def test_known_kind_scores_from_the_table(tmp_path):
    eng = _engine(tmp_path, "disk_low = 7\napp_switch = 1\n")
    assert eng.score(_ev("watch_system", kind="disk_low")) == (7, "disk_low")
    assert eng.score(_ev("app_switch", kind="app_switch")) == (1, "app_switch")


def test_kind_from_payload_wins_over_event_type(tmp_path):
    # watch_system is the event TYPE; the salient distinction is the KIND.
    eng = _engine(tmp_path, "disk_low = 7\nram_high = 5\n")
    assert eng.score(_ev("watch_system", kind="ram_high")) == (5, "ram_high")


def test_falls_back_to_event_type_when_no_kind(tmp_path):
    eng = _engine(tmp_path, "clock = 9\n")
    assert eng.score(_ev("clock")) == (9, "clock")   # no payload.kind → use type


def test_unknown_kind_defaults_to_2_log_only(tmp_path):
    eng = _engine(tmp_path, "disk_low = 7\n")
    assert eng.score(_ev("watch_system", kind="brand_new_signal")) == (DEFAULT_SCORE, "default")
    assert DEFAULT_SCORE == 2


def test_scores_are_clamped_to_0_10(tmp_path):
    eng = _engine(tmp_path, "a = 99\nb = -4\n")
    assert eng.score(_ev("a")) == (10, "a")
    assert eng.score(_ev("b")) == (0, "b")


def test_non_int_value_is_skipped_not_fatal(tmp_path):
    # "high" is not an int → that rule is dropped (unknown → default), the rest load.
    eng = _engine(tmp_path, 'disk_low = 7\nram_high = "high"\n')
    assert eng.error is None                          # a skipped value is not a parse error
    assert eng.score(_ev("disk_low")) == (7, "disk_low")
    assert eng.score(_ev("ram_high")) == (DEFAULT_SCORE, "default")


# --- rider 2: fail-safe hot-reload -------------------------------------------

def test_edit_hot_reloads_after_throttle(tmp_path):
    clock = [1000.0]
    eng = _engine(tmp_path, "disk_low = 7\n", clock=lambda: clock[0],
                  reload_throttle_s=2.0)
    assert eng.score(_ev("disk_low"))[0] == 7
    # bump mtime by rewriting with a new value
    (tmp_path / "salience_rules.toml").write_text("disk_low = 9\n", encoding="utf-8")
    clock[0] = 1001.0                                 # inside throttle → not yet reloaded
    assert eng.score(_ev("disk_low"))[0] == 7
    clock[0] = 1005.0                                 # past throttle → reload picks it up
    assert eng.score(_ev("disk_low"))[0] == 9


def test_malformed_edit_keeps_last_good_and_reports(tmp_path):
    clock = [1000.0]
    path = tmp_path / "salience_rules.toml"
    eng = _engine(tmp_path, "disk_low = 7\n", clock=lambda: clock[0],
                  reload_throttle_s=0.0)
    assert eng.score(_ev("disk_low"))[0] == 7
    path.write_text("disk_low = = broken ==\n", encoding="utf-8")   # invalid TOML
    clock[0] = 2000.0
    # never raises, keeps 7, surfaces the error for --doctor
    assert eng.score(_ev("disk_low"))[0] == 7
    assert eng.error is not None
    assert "salience_rules.toml" in eng.error
    assert eng.status()["error"] is not None


def test_recovers_from_malformed_when_fixed(tmp_path):
    clock = [1000.0]
    path = tmp_path / "salience_rules.toml"
    eng = _engine(tmp_path, "disk_low = 7\n", clock=lambda: clock[0],
                  reload_throttle_s=0.0)
    path.write_text("nope == broken\n", encoding="utf-8")
    clock[0] = 2000.0
    eng.score(_ev("disk_low"))
    assert eng.error is not None
    path.write_text("disk_low = 4\n", encoding="utf-8")
    clock[0] = 3000.0
    assert eng.score(_ev("disk_low"))[0] == 4          # recovered
    assert eng.error is None                           # error cleared


def test_malformed_on_first_load_falls_back_to_embedded_not_zero(tmp_path):
    # No last-good table exists yet — an empty table would collapse every score
    # to the default. The engine must fall back to the embedded defaults.
    eng = _engine(tmp_path, "== not valid ==\n")
    assert eng.error is not None                        # reported
    assert eng.status()["rules"] > 0                    # NOT zeroed
    # a known embedded kind still scores from the embedded table, not the default
    assert eng.score(_ev("disk_low"))[0] != DEFAULT_SCORE


def test_missing_file_uses_embedded_defaults(tmp_path):
    eng = SalienceEngine(path=tmp_path / "does_not_exist.toml", seed=False)
    assert eng.status()["rules"] > 0
    assert eng.error is None
    assert eng.score(_ev("disk_low"))[0] == 7           # embedded default value


def test_score_never_raises_on_junk_event(tmp_path):
    eng = _engine(tmp_path, "disk_low = 7\n")
    assert eng.score(SimpleNamespace()) == (DEFAULT_SCORE, "default")
    assert eng.score(SimpleNamespace(payload=None, type=None)) == (DEFAULT_SCORE, "default")


# --- rider 1: provenance through the bus -------------------------------------

def test_bus_stamps_score_and_rule_onto_the_event(tmp_path):
    eng = _engine(tmp_path, "disk_low = 7\n")
    bus = EventBus(scorer=eng.score)
    ev = bus.publish("watch_system", payload={"kind": "disk_low", "value": 9})
    assert ev.salience == 7
    assert ev.payload["score"] == 7
    assert ev.payload["rule"] == "disk_low"            # WHICH rule, not just the number


def test_scorer_that_throws_never_blocks_the_event(tmp_path):
    def boom(ev): raise RuntimeError("scorer exploded")
    bus = EventBus(scorer=boom)
    ev = bus.publish("watch_system", payload={"kind": "disk_low"})
    assert "score" not in ev.payload                   # scoring failed, event still published
    assert ev.type == "watch_system"


# --- payload audit: scoring leaks nothing new --------------------------------

def test_scoring_adds_only_score_and_rule(tmp_path):
    eng = _engine(tmp_path, "app_switch = 1\n")
    ev = Event(type="app_switch", payload={"app": "chrome.exe"})
    before = set(ev.payload)
    eng_score, eng_rule = eng.score(ev)
    # score() is pure — it does not mutate; the BUS stamps the payload. Prove the
    # scorer itself introduces no field, and that the only added keys are score/rule.
    assert set(ev.payload) == before                   # score() did not mutate
    bus = EventBus(scorer=eng.score)
    pub = bus.publish("app_switch", payload={"app": "chrome.exe"})
    added = set(pub.payload) - {"app"}
    assert added == {"score", "rule"}                  # ONLY score + rule, nothing else


def test_scored_event_teed_to_log_leaks_no_path_and_only_score_rule_survive(tmp_path):
    # A watch_fs event carrying a full path (never in the allowlist) and a bogus
    # non-allowlisted field: scoring must not preserve either into the audit log.
    # The log's allowlist is the boundary; the scorer must not smuggle anything
    # past it, and must add exactly score + rule.
    log = EventLog(tmp_path / "events.sqlite")
    eng = _engine(tmp_path, "file_added = 3\n")
    bus = EventBus(eventlog=log, scorer=eng.score)
    bus.publish("watch_fs", payload={"kind": "file_added", "name": "report.pdf",
                                     "path": "C:/Users/secret/report.pdf",
                                     "clipboard": "leaked contents"})
    assert log.flush(timeout=2.0)
    rows = log.recent(limit=10)
    log.close()

    fs = next(r for r in rows if r["type"] == "watch_fs")
    assert fs["payload"]["score"] == 3
    assert fs["payload"]["rule"] == "file_added"
    assert "path" not in fs["payload"]                 # full path stripped by allowlist
    assert "clipboard" not in fs["payload"]            # non-allowlisted field stripped
    assert "secret" not in str(fs["payload"])
    assert "leaked" not in str(fs["payload"])
    # basename kind/name kept; the only fields the scorer added are score + rule
    assert set(fs["payload"]) == {"kind", "name", "score", "rule"}


# --- doctor status shape -----------------------------------------------------

def test_status_reports_rule_count_and_error(tmp_path):
    eng = _engine(tmp_path, "disk_low = 7\nram_high = 5\n")
    st = eng.status()
    assert st["rules"] == 2
    assert st["error"] is None
    assert st["path"].endswith("salience_rules.toml")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
