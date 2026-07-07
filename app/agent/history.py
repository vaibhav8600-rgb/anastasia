"""SQLite command/interaction history. Raw audio is never logged.

Thread-safety (9.1A): every call opens its own short-lived connection, writes,
and closes. A personal assistant's write volume makes the open/close cost
irrelevant, and it makes cross-thread use correct by construction — no shared
handle can be closed out from under another thread (the confirmation-approval
crash). WAL mode keeps concurrent readers safe. log() can never raise.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.agent.devlog import devlog
from app.config import HISTORY_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    transcript TEXT,
    intent TEXT,
    tool TEXT,
    arguments TEXT,
    risk_level TEXT,
    allowed INTEGER,
    requires_confirmation INTEGER,
    executed INTEGER,
    result TEXT,
    error TEXT
)
"""


class History:
    def __init__(self, db_path: Path = HISTORY_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        return conn

    def log(self, transcript: str = "", plan=None, safety=None,
            executed: bool = False, result: str = "", error: str = "") -> None:
        """Best-effort write. NEVER raises — a logging failure must not break
        command execution or the error handler (9.1A.2)."""
        try:
            row = (
                datetime.now().isoformat(timespec="seconds"),
                transcript,
                getattr(plan, "intent", ""),
                getattr(plan, "tool_name", ""),
                json.dumps(getattr(plan, "arguments", {}) or {}),
                getattr(safety, "risk_level", ""),
                int(bool(getattr(safety, "allowed", False))),
                int(bool(getattr(safety, "requires_confirmation", False))),
                int(bool(executed)),
                str(result)[:2000],
                str(error)[:2000],
            )
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO interactions (ts, transcript, intent, tool, arguments,"
                    " risk_level, allowed, requires_confirmation, executed, result, error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
                conn.commit()
        except Exception as e:
            devlog.warn(f"history write failed: {' '.join(str(e).split())[:150]}")

    def recent(self, limit: int = 50) -> list:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT ts, transcript, intent, tool, arguments, risk_level,"
                    " allowed, requires_confirmation, executed, result, error"
                    " FROM interactions ORDER BY id DESC LIMIT ?", (limit,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as e:
            devlog.warn(f"history read failed: {' '.join(str(e).split())[:150]}")
            return []

    def clear(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM interactions")
                conn.commit()
        except Exception as e:
            devlog.warn(f"history clear failed: {' '.join(str(e).split())[:150]}")

    def close(self) -> None:
        # Per-call connections are already closed; nothing to hold open.
        return
