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
    log.flush()
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
    log.flush(timeout=5)
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


def test_internal_underscore_args_are_dropped(log):
    """11C stamps `_resolved` on the plan — it carries a base64 SCREENSHOT of
    the click target. That must never reach a log."""
    log.emit("tool_call", tool="click_control",
             args={"hint": "Send",
                   "_resolved": {"name": "Send",
                                 "crop_data_url": FAKE_B64_IMAGE}})
    body = db_text(log)
    assert "_resolved" not in body and "crop_data_url" not in body
    assert "base64" not in body and "AAAA" not in body
    assert "Send" in body


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
