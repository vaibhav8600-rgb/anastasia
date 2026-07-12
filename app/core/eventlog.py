"""Append-only event log (Phase 0, requirement §6).

The audit trail. Later it also becomes Phase 2's memory feedstock and Phase 6's
habit-mining corpus — so it must be *complete* about what Anna did, and
*incapable* of holding what she saw, heard or was trusted with.

Four guarantees, in order of importance:

1. **Secret-free by construction, not by hope.** A payload is never serialized
   wholesale. Each event type declares an allowlist of fields; everything else
   is dropped before it reaches the writer. On top of that sit three more
   layers: a key denylist, a value scrubber reusing the Phase-11 secret
   patterns, and a hard length cap. It is easier to prove a field cannot be
   logged than to prove every caller remembered to redact it.

2. **One writer.** SQLite plus many threads is a bug generator (Phase 9.1A was
   exactly that crash). Callers `emit()` onto a queue; a single daemon thread
   owns the only write connection. Reads use their own short-lived connections.

3. **It can never block a turn.** `emit()` is non-blocking: a full queue drops
   the event and counts it. A logging problem must never cost the user a word.

4. **It fails closed, loudly, and keeps Anna talking.** Write failures trip the
   house circuit-breaker pattern (3 strikes → open 120s → probe) and spill to a
   JSONL fallback file, so the audit trail survives even when SQLite won't.

NEVER logged, by construction: raw audio, screen text / OCR output, frames or
image data, clipboard contents, file contents, API keys, passwords.
"""

import json
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from app.agent.devlog import devlog
from app.config import DATA_DIR

DEFAULT_PATH = DATA_DIR / "events.sqlite"
FALLBACK_PATH = DATA_DIR / "events_fallback.jsonl"

QUEUE_MAX = 2000          # ~an hour of heavy use; full => drop, never block
VALUE_MAX_CHARS = 300     # a screenful of OCR can never fit through this
CIRCUIT_FAILURES = 3      # house pattern (brain, STT, Live all use it)
CIRCUIT_COOLDOWN_S = 120.0
WRITER_JOIN_S = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    type         TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT '',
    payload_json TEXT    NOT NULL DEFAULT '{}',
    salience     REAL    NOT NULL DEFAULT 0.0,
    outcome      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


# ---- layer 1: the allowlist ------------------------------------------------
# THE security boundary. A field that is not named here cannot be logged, no
# matter what a caller passes. Adding a field here is a privacy decision —
# review it as one.
EVENT_FIELDS = {
    "user_turn":         ("text", "route", "engine", "confidence"),
    "assistant_turn":    ("text", "engine"),
    "tool_call":         ("tool", "args", "success", "duration_ms", "backend"),
    "validator_verdict": ("tool", "allowed", "risk", "requires_confirmation",
                          "destructive_target", "confidence", "reason"),
    "confirmation":      ("tool", "outcome", "strong_required", "channel",
                          "reason"),
    "engine_state":      ("component", "state", "reason"),
    "circuit_state":     ("component", "state", "failures"),
    "capture":           ("kind", "scope", "used_cloud", "chars"),  # NEVER content
    "error":             ("component", "message"),
}

# ---- layer 2: the key denylist (defence in depth) ---------------------------
# Even inside an allowlisted dict (e.g. tool `args`), these keys never survive.
DENY_KEYS = {
    "password", "passwd", "pw", "secret", "token", "api_key", "apikey",
    "key", "credential", "credentials", "auth", "authorization", "cookie",
    "session", "pin", "cvv", "cvc", "otp",
}

# Tool arguments whose VALUES are the user's own words going somewhere — a
# password typed into a field is still a password. Keep the shape, drop the text.
REDACT_VALUE_TOOLS = {
    "type_text", "clipboard_write", "browser_type_into", "type_into_control",
}

# Tools whose results are content, never destinations. Their args carry hints,
# never the content itself — but be explicit about it.
CONTENT_TOOLS = {
    "look_at_screen", "screen_capture", "active_window_capture",
    "region_capture", "camera_look", "clipboard_read", "summarize_clipboard",
    "read_window_text", "browser_read_page_text",
}

_DATA_URL = re.compile(r"data:[^;,\s]+;base64,", re.IGNORECASE)


def _scrub(text: str) -> str:
    """Layer 3: strip secret-SHAPED things from a value we do intend to keep.
    Reuses the Phase-11 patterns rather than inventing a second, divergent list."""
    from app.vision.sensitive import SENSITIVE_PATTERNS

    out = str(text)
    if _DATA_URL.search(out):
        return "[redacted: embedded image data]"
    for pattern, _label in SENSITIVE_PATTERNS:
        out = re.sub(pattern, "[redacted]", out)
    if len(out) > VALUE_MAX_CHARS:      # layer 4: nothing bulk can get through
        out = out[:VALUE_MAX_CHARS] + f"…[+{len(out) - VALUE_MAX_CHARS} chars]"
    return out


def _clean_value(value):
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, dict):
        return _clean_args(value)
    if isinstance(value, (list, tuple)):
        return [_clean_value(v) for v in list(value)[:10]]
    return _scrub(value)


def _clean_args(args: dict) -> dict:
    """Sanitize a tool-argument dict: drop internal and denylisted keys, scrub
    what remains. `_resolved` (11C) is the reason for the underscore rule — it
    carries a base64 screenshot crop of the click target."""
    clean = {}
    for key, value in (args or {}).items():
        name = str(key)
        if name.startswith("_"):                       # internal (e.g. _resolved)
            continue
        if name.lower() in DENY_KEYS:
            clean[name] = "[redacted]"
            continue
        clean[name] = _clean_value(value)
    return clean


def sanitize(event_type: str, payload: dict) -> dict:
    """Apply all four layers. Unknown event type => empty payload (fail closed:
    an un-allowlisted event can leak nothing)."""
    fields = EVENT_FIELDS.get(event_type)
    if fields is None:
        devlog.warn(f"eventlog: unknown event type {event_type!r} — payload dropped")
        return {}

    out = {}
    tool = str((payload or {}).get("tool") or "")
    for name in fields:
        if name not in (payload or {}):
            continue
        value = payload[name]
        if name == "args":
            args = _clean_args(value if isinstance(value, dict) else {})
            if tool in REDACT_VALUE_TOOLS:
                # keep the shape (which keys), never the words
                args = {k: (f"[redacted {len(str(v))} chars]"
                            if isinstance(v, str) else _clean_value(v))
                        for k, v in args.items()}
            out[name] = args
        else:
            out[name] = _clean_value(value)
    return out


class EventLog:
    """Append-only, WAL, single-writer event store."""

    def __init__(self, path=None, *, fallback_path=None, queue_max: int = QUEUE_MAX,
                 start: bool = True):
        self.path = Path(path) if path else DEFAULT_PATH
        self.fallback_path = (Path(fallback_path) if fallback_path
                              else self.path.with_suffix(".fallback.jsonl"))
        self._q = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread = None
        self._failures = 0
        self._open_until = 0.0
        self.dropped = 0            # queue-full drops (never blocks a turn)
        self.written = 0
        self.spilled = 0            # rows written to the JSONL fallback
        if start:
            self.start()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="anna-eventlog")
        self._thread.start()

    def close(self, timeout: float = WRITER_JOIN_S) -> None:
        """Flush and stop. Safe to call twice."""
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._q.put_nowait(None)          # wake the writer
        except queue.Full:
            pass
        self._thread.join(timeout)
        self._thread = None

    # ------------------------------------------------------------ circuit
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _record_failure(self, error: Exception) -> None:
        self._failures += 1
        if self._failures >= CIRCUIT_FAILURES and not self.circuit_open():
            self._open_until = time.monotonic() + CIRCUIT_COOLDOWN_S
            devlog.warn(f"eventlog: circuit OPEN for {CIRCUIT_COOLDOWN_S:.0f}s "
                        f"after {self._failures} write failures "
                        f"({' '.join(str(error).split())[:80]}) — spilling to "
                        f"{self.fallback_path.name}. Voice is unaffected.")

    def _record_success(self) -> None:
        if self._failures or self._open_until:
            self._failures = 0
            self._open_until = 0.0
            devlog.log("eventlog: circuit CLOSED — writing to SQLite again.")

    # --------------------------------------------------------------- writing
    def emit(self, event_type: str, *, source: str = "", salience: float = 0.0,
             outcome: str = "", **payload) -> bool:
        """Record an event. NON-BLOCKING and never raises: a logging problem
        must never cost the user a word. Returns False if the row was dropped."""
        try:
            row = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "type": str(event_type),
                "source": str(source)[:40],
                "payload": sanitize(str(event_type), payload),
                "salience": float(salience),
                "outcome": str(outcome)[:60],
            }
        except Exception as e:                    # sanitizing must not kill a turn
            devlog.warn(f"eventlog: could not sanitize {event_type!r} ({e})")
            return False
        try:
            self._q.put_nowait(row)
            return True
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 100, 1000):    # don't spam
                devlog.warn(f"eventlog: queue full — {self.dropped} events "
                            "dropped. Voice is unaffected.")
            return False

    def _run(self) -> None:
        conn = None
        try:
            conn = self._connect()
        except Exception as e:
            self._record_failure(e)

        while True:
            try:
                row = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if row is None:
                break
            if conn is None and not self.circuit_open():
                try:
                    conn = self._connect()       # probe
                    self._record_success()
                except Exception as e:
                    self._record_failure(e)
            if conn is not None and not self.circuit_open():
                try:
                    self._write(conn, row)
                    self.written += 1
                    self._record_success()
                    continue
                except Exception as e:
                    self._record_failure(e)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
            self._spill(row)                      # circuit open / no connection

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _connect(self):
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    @staticmethod
    def _write(conn, row: dict) -> None:
        conn.execute(
            "INSERT INTO events (ts, type, source, payload_json, salience, outcome)"
            " VALUES (?,?,?,?,?,?)",
            (row["ts"], row["type"], row["source"],
             json.dumps(row["payload"], default=str),
             row["salience"], row["outcome"]))
        conn.commit()

    def _spill(self, row: dict) -> None:
        """Fallback: the audit trail survives even when SQLite won't."""
        try:
            self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.fallback_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            self.spilled += 1
        except Exception:
            pass          # last resort: drop. Never raise on the writer thread.

    # --------------------------------------------------------------- reading
    def flush(self, timeout: float = 2.0) -> None:
        """Block until the queue drains (tests, and Quit)."""
        deadline = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.02)      # let the in-flight row commit

    def recent(self, limit: int = 50, event_type: str = None) -> list:
        """Newest first. Own short-lived connection — never the writer's."""
        try:
            conn = sqlite3.connect(str(self.path))
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM events"
            args = []
            if event_type:
                sql += " WHERE type = ?"
                args.append(event_type)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            rows = [dict(r) for r in conn.execute(sql, args)]
            conn.close()
            for row in rows:
                try:
                    row["payload"] = json.loads(row.pop("payload_json"))
                except Exception:
                    row["payload"] = {}
            return rows
        except Exception as e:
            devlog.warn(f"eventlog: read failed ({e})")
            return []

    def stats(self) -> dict:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"written": self.written, "dropped": self.dropped,
                "spilled": self.spilled, "queued": self._q.qsize(),
                "circuit": "open" if self.circuit_open() else "closed",
                "db_bytes": size}
