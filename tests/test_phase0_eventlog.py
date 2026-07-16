"""Phase 0 · commit 1 — the append-only event log.

The security tests here are the point of the module. M0.5 requires that a human
can grep the log for their API keys, passwords, screen text and clipboard and
get ZERO hits — so we prove that by construction, not by convention: we throw
real-shaped secrets at every entry point and assert the DB file on disk does not
contain them.
"""

import json
import sqlite3
import threading
import time

import pytest

from app.core.eventlog import (CONTENT_TOOLS, DENY_KEYS, EVENT_FIELDS, EventLog,
                               sanitize)

# Real-SHAPED (invalid) secrets — the exact things M0.5 says to grep for.
FAKE_GROQ = "gsk_ABCDEFGHIJKLMNOPQRSTUVWX0123456789abcd"
FAKE_GEMINI = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
FAKE_OPENAI = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
FAKE_CARD = "4111 1111 1111 1111"
FAKE_B64_IMAGE = "data:image/jpeg;base64," + ("A" * 5000)


@pytest.fixture
def log(tmp_path):
    el = EventLog(tmp_path / "events.sqlite")
    yield el
    el.close()


def db_text(el) -> str:
    """EVERY byte the log has put on disk — the .sqlite, the -wal, the -shm and
    any spill file.

    Reading only `events.sqlite` would be a trap: in WAL mode the rows live in
    `events.sqlite-wal` until a checkpoint, so a naive 'secret not in db file'
    assertion passes vacuously against an empty file — and a human grepping the
    DB for their API key would be reassured by nothing. M0.5 means the whole
    directory.
    """
    el.flush()
    body = []
    for path in sorted(el.path.parent.iterdir()):
        if path.is_file():
            body.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(body)


# ---- it works at all --------------------------------------------------------

def test_append_only_wal_schema(log):
    log.emit("user_turn", source="voice", text="open paint", route="rule")
    # flush() waits for the row to be COMMITTED, not merely dequeued, and
    # admits a timeout instead of pretending — assert it, don't hope.
    assert log.flush(timeout=10)
    rows = log.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "user_turn" and row["source"] == "voice"
    assert row["payload"]["text"] == "open paint"
    assert set(row) >= {"id", "ts", "type", "source", "salience", "outcome"}

    conn = sqlite3.connect(str(log.path))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_one_writer_survives_many_threads(log):
    """SQLite + many writer threads is a bug generator (9.1A was that crash).
    Callers emit from anywhere; exactly one thread writes."""
    def spam(n):
        for i in range(25):
            log.emit("tool_call", source=f"t{n}", tool="open_app",
                     args={"app_name": "paint"}, success=True)

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Generous ceiling; flush returns the moment all 200 are settled. Under
    # full-suite CPU load the old queue-empty check timed out silently and
    # this assertion read a half-written log.
    assert log.flush(timeout=30)
    assert len(log.recent(limit=500)) == 200      # 8 x 25, none lost, no corruption


# ---- layer 1: the allowlist -------------------------------------------------

def test_unlisted_fields_cannot_be_logged(log):
    log.emit("user_turn", text="hello", route="chat",
             screen_text="EVERYTHING ON THE USER'S SCREEN",   # not allowlisted
             audio=b"\x00\x01", clipboard="secret note")
    body = db_text(log)
    assert "hello" in body
    for leaked in ("EVERYTHING ON", "secret note", "clipboard", "screen_text"):
        assert leaked not in body, f"{leaked!r} leaked through the allowlist"


def test_unknown_event_type_fails_closed(log):
    """An event type nobody allowlisted can leak nothing."""
    assert sanitize("not_a_real_type", {"text": FAKE_GROQ}) == {}
    log.emit("not_a_real_type", text=FAKE_GROQ, anything=FAKE_GEMINI)
    assert FAKE_GROQ not in db_text(log)
    assert FAKE_GEMINI not in db_text(log)


def test_every_event_type_has_an_allowlist():
    # the phase requires these to be logged; each must declare its fields
    for required in ("user_turn", "tool_call", "validator_verdict",
                     "confirmation", "engine_state", "circuit_state", "error"):
        assert required in EVENT_FIELDS and EVENT_FIELDS[required]


# ---- layer 2: the key denylist ----------------------------------------------

def test_denylisted_keys_are_redacted_inside_args(log):
    log.emit("tool_call", tool="browser_find_and_click",
             args={"hint": "Sign in", "password": "hunter2",
                   "api_key": FAKE_GROQ, "token": "abc"})
    body = db_text(log)
    assert "hunter2" not in body and FAKE_GROQ not in body and "abc" not in body
    assert "Sign in" in body                      # the useful part survives
    assert "password" in DENY_KEYS


def test_resolved_target_is_audited_without_its_pixels(log):
    """11C stamps `_resolved` on the plan. It carries BOTH the audit gold (which
    control, which backend, how confident) AND a base64 screenshot of the target.
    Keep the former; reduce the latter to presence-without-content — dropping it
    silently would leave the audit quietly incomplete."""
    log.emit("tool_call", tool="click_control",
             args={"hint": "Send",
                   "_resolved": {"name": "Send", "control_type": "button",
                                 "backend": "playwright", "confidence": 1.0,
                                 "crop_data_url": FAKE_B64_IMAGE}})
    log.flush()
    resolved = log.recent()[0]["payload"]["args"]["_resolved"]

    # the audit survives, in full
    assert resolved["name"] == "Send"
    assert resolved["backend"] == "playwright"
    assert resolved["confidence"] == 1.0

    # ...and the image is described, not stored
    crop = resolved["crop_data_url"]
    assert crop["dropped"] == "image"
    assert crop["bytes"] == len(FAKE_B64_IMAGE)
    assert len(crop["sha256"]) == 16

    body = db_text(log)
    assert "base64" not in body and "AAAA" not in body


def test_raw_bytes_become_a_descriptor(log):
    """Raw audio is bytes. It can never be logged — but its presence can be."""
    log.emit("tool_call", tool="run_terminal", args={"blob": b"\x00\xff" * 4000})
    log.flush()
    blob = log.recent()[0]["payload"]["args"]["blob"]
    assert blob["dropped"] == "binary" and blob["bytes"] == 8000
    assert "\\x00" not in db_text(log)


# ---- layer 3: value scrubbing (reuses the Phase-11 secret patterns) ---------

@pytest.mark.parametrize("secret", [FAKE_GROQ, FAKE_GEMINI, FAKE_OPENAI, FAKE_CARD])
def test_secret_shaped_values_are_scrubbed_anywhere(log, secret):
    """M0.5: grep the log for your keys -> zero hits. Even when the secret rides
    in an allowlisted free-text field."""
    log.emit("user_turn", text=f"my key is {secret} ok", route="chat")
    log.emit("error", component="brain", message=f"auth failed for {secret}")
    log.emit("tool_call", tool="run_terminal", args={"command": f"curl -H {secret}"})
    body = db_text(log)
    assert secret not in body
    assert "[redacted]" in body


def test_embedded_image_data_never_stored(log):
    log.emit("tool_call", tool="take_screenshot", args={"thumb": FAKE_B64_IMAGE})
    body = db_text(log)
    assert "base64" not in body and "AAAAAAA" not in body


# ---- layer 4: bulk content cannot fit through --------------------------------

def test_screen_text_and_file_contents_cannot_be_logged(log):
    """Even an allowlisted text field is length-capped, so a screenful of OCR or
    a file's contents physically cannot be stored."""
    from app.core.eventlog import VALUE_MAX_CHARS

    screenful = "CONFIDENTIAL LINE. " * 500          # ~9500 chars
    log.emit("user_turn", text=screenful, route="chat")
    log.flush()

    stored = log.recent()[0]["payload"]["text"]
    assert len(stored) < VALUE_MAX_CHARS + 40        # capped, not stored whole
    assert stored.endswith("chars]")                 # and it says it truncated
    assert db_text(log).count("CONFIDENTIAL LINE") < 20


def test_capture_events_record_shape_never_content(log):
    """A vision capture logs THAT it happened and how big — never what it saw."""
    assert "text" not in EVENT_FIELDS["capture"]
    assert "content" not in EVENT_FIELDS["capture"]
    log.emit("capture", source="voice", kind="screen", scope="monitor",
             used_cloud=True, chars=1200,
             text="THE SECRET CONTENTS OF MY SCREEN")
    body = db_text(log)
    assert "SECRET CONTENTS" not in body
    assert "monitor" in body and "1200" in body
    assert "look_at_screen" in CONTENT_TOOLS


def test_typed_text_keeps_shape_not_words(log):
    """A password typed into a field is still a password."""
    log.emit("tool_call", tool="type_text", args={"text": "hunter2 my-bank-pw"})
    body = db_text(log)
    assert "hunter2" not in body and "my-bank-pw" not in body
    assert "type_text" in body and "redacted" in body   # we know it happened


# ---- OVER-redaction: a log that redacts everything is also useless -----------
# These matter as much as the secret tests. The four layers must let a rich,
# legitimate event through INTACT and still useful to a human reading the audit.

def test_ordinary_events_survive_all_four_layers_intact(log):
    log.emit("user_turn", source="voice", text="open chrome and search for flights",
             route="rule", engine="pipeline", confidence=0.94)
    log.emit("tool_call", source="voice", tool="browser_open",
             args={"url": "https://www.google.com/search?q=flights+to+goa"},
             success=True, duration_ms=142)
    log.emit("validator_verdict", tool="run_terminal", allowed=True, risk="medium",
             requires_confirmation=True,
             reason="Terminal commands always require confirmation.")
    log.emit("confirmation", tool="run_terminal", outcome="approved",
             strong_required=True, channel="voice", salience=0.8)
    log.flush()

    rows = {r["type"]: r for r in log.recent()}

    turn = rows["user_turn"]["payload"]
    assert turn["text"] == "open chrome and search for flights"   # verbatim
    assert turn["route"] == "rule" and turn["confidence"] == 0.94  # types kept

    call = rows["tool_call"]["payload"]
    assert call["args"]["url"] == "https://www.google.com/search?q=flights+to+goa"
    assert call["success"] is True and call["duration_ms"] == 142

    verdict = rows["validator_verdict"]["payload"]
    assert verdict["reason"] == "Terminal commands always require confirmation."
    assert verdict["allowed"] is True and verdict["risk"] == "medium"

    # the verdict rides in the `outcome` COLUMN (first-class), not the payload
    assert rows["confirmation"]["outcome"] == "approved"
    assert rows["confirmation"]["salience"] == 0.8
    assert rows["confirmation"]["payload"]["strong_required"] is True
    assert rows["confirmation"]["payload"]["channel"] == "voice"


def test_no_event_field_shadows_a_column():
    """`source`, `salience` and `outcome` are emit() kwargs that become COLUMNS.
    An event type that also declared one as a payload field would be
    structurally unfillable — emit() would swallow it into the column and the
    payload key would never appear. (This is not hypothetical: `confirmation`
    shipped with exactly that bug and the over-redaction test caught it.)"""
    from app.core.eventlog import RESERVED_COLUMNS
    for event_type, fields in EVENT_FIELDS.items():
        clash = set(fields) & set(RESERVED_COLUMNS)
        assert not clash, f"{event_type} payload shadows column(s) {clash}"


def test_talking_about_a_password_is_not_a_password(log):
    """The sharp distinction: the user's WORDS are the audit (keep them); a
    credential in an argument VALUE is a secret (kill it)."""
    log.emit("user_turn", text="how do I reset my password on GitHub", route="chat")
    log.emit("tool_call", tool="browser_type_into",
             args={"field": "password", "password": "hunter2"})
    log.flush()
    rows = log.recent()

    turn = next(r for r in rows if r["type"] == "user_turn")
    assert turn["payload"]["text"] == "how do I reset my password on GitHub"

    call = next(r for r in rows if r["type"] == "tool_call")
    assert call["payload"]["args"]["password"] == "[redacted]"
    assert "hunter2" not in db_text(log)


def test_a_full_length_legitimate_value_is_not_truncated(log):
    """Right up to the cap, real content must arrive whole."""
    from app.core.eventlog import VALUE_MAX_CHARS
    sentence = "a" * (VALUE_MAX_CHARS - 1)
    log.emit("error", component="brain", message=sentence)
    log.flush()
    assert log.recent()[0]["payload"]["message"] == sentence   # untouched


# ---- it can never cost the user a word ---------------------------------------

def test_emit_never_blocks_and_never_raises(tmp_path):
    el = EventLog(tmp_path / "e.sqlite", queue_max=5, start=False)  # writer NOT running
    started = time.perf_counter()
    for i in range(200):                       # overflow the queue 40x over
        assert el.emit("user_turn", text=f"turn {i}") in (True, False)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, "emit() blocked a turn"
    assert el.dropped > 0                      # dropped, honestly counted
    el.close()


def test_queue_is_bounded_and_drops_the_OLDEST(tmp_path):
    """Bounded RAM, and under pressure we keep what a human is about to read."""
    el = EventLog(tmp_path / "e.sqlite", queue_max=5, start=False)
    for i in range(50):
        el.emit("user_turn", text=f"turn {i}")

    assert el._q.qsize() <= 5                  # bounded — no unbounded RAM
    kept = []
    while not el._q.empty():
        kept.append(el._q.get_nowait()["payload"]["text"])
    assert kept[-1] == "turn 49"               # the NEWEST survived
    assert "turn 0" not in kept                # the oldest was the one dropped
    assert el.dropped == 45
    el.close()


def test_a_dropped_event_is_admitted_in_the_log(tmp_path):
    """No silent loss: once the writer catches up it records the hole."""
    el = EventLog(tmp_path / "e.sqlite", queue_max=5, start=False)
    for i in range(50):                        # overflow while the writer is down
        el.emit("user_turn", text=f"turn {i}")
    assert el.dropped == 45

    el.start()                                 # writer comes back
    deadline = time.time() + 5
    while not el.recent(limit=1, event_type="log_gap") and time.time() < deadline:
        time.sleep(0.05)
    el.close()

    gaps = el.recent(event_type="log_gap")
    assert gaps, "the log lost 45 events and never said so"
    assert gaps[0]["payload"]["dropped"] == 45
    assert gaps[0]["payload"]["reason"] == "queue full"
    assert gaps[0]["salience"] == 1.0          # a human should notice this


def test_write_failure_opens_circuit_and_spills_to_file(tmp_path):
    """A broken DB must not silence the audit trail, and must not touch voice."""
    el = EventLog(tmp_path / "e.sqlite", start=False)
    el.path = tmp_path / "nope" / "dir" / "e.sqlite"   # unwritable: parent absent
    el.path.parent.mkdir(parents=True, exist_ok=True)
    el.path.mkdir()                                    # a DIRECTORY where the DB goes
    el.fallback_path = tmp_path / "spill.jsonl"
    el.start()
    for i in range(4):
        el.emit("user_turn", text=f"turn {i}", route="rule")
    deadline = time.time() + 5
    while el.spilled < 4 and time.time() < deadline:
        time.sleep(0.05)
    el.close()

    assert el.circuit_open()                     # 3 strikes -> open
    assert el.spilled >= 4                       # nothing lost
    lines = el.fallback_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 4
    assert json.loads(lines[0])["payload"]["text"] == "turn 0"


# ---- the audit TOOLING must not repeat the WAL trap --------------------------

def test_secret_scanner_reads_the_wal_not_just_the_sqlite(tmp_path, monkeypatch):
    """The trap, pinned. While a writer is live, rows sit in `events.sqlite-wal`
    and the main `.sqlite` does NOT contain them. A scanner that reads only the
    `.sqlite` searches an empty file, finds nothing, and tells a human their
    secrets are safe having checked NOTHING. Verified against the real files."""
    import app.core.inspect_events as inspect

    path = tmp_path / "events.sqlite"
    monkeypatch.setattr(inspect, "DEFAULT_PATH", path)

    el = EventLog(path)                       # writer stays OPEN, as in a session
    el.emit("user_turn", text="a distinctive marker phrase", route="chat")
    el.flush()

    names = [f.name for f in inspect.log_files(path)]
    assert any(n.endswith("-wal") for n in names), "the -wal file was not scanned"

    wal = next(f for f in inspect.log_files(path) if f.name.endswith("-wal"))
    assert b"a distinctive marker phrase" in wal.read_bytes()      # it's in the WAL
    assert b"a distinctive marker phrase" not in path.read_bytes()  # NOT in the db
    el.close()


def test_secret_scanner_would_actually_catch_a_leak(tmp_path, monkeypatch):
    """Prove the scanner is not vacuous: plant a real key on disk, expect a hit."""
    import app.core.inspect_events as inspect

    path = tmp_path / "events.sqlite"
    path.write_text("")
    (tmp_path / "events.fallback.jsonl").write_text(
        json.dumps({"leak": FAKE_GROQ}), encoding="utf-8")
    monkeypatch.setattr(inspect, "DEFAULT_PATH", path)

    hits = inspect.scan_secrets(path)
    assert hits, "the scanner cannot see a key sitting in plain sight"
    assert any("Groq" in label for _f, label, _s in hits)


def test_stats_are_honest(log):
    log.emit("user_turn", text="hi")
    log.flush()
    stats = log.stats()
    assert stats["written"] == 1 and stats["dropped"] == 0
    assert stats["circuit"] == "closed" and stats["db_bytes"] > 0


def test_close_is_idempotent_and_flushes(tmp_path):
    el = EventLog(tmp_path / "e.sqlite")
    el.emit("user_turn", text="last words")
    el.close()
    el.close()                                   # twice must be safe
    assert el.recent()[0]["payload"]["text"] == "last words"
