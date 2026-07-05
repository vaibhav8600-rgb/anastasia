"""SQLite command/interaction history. Raw audio is never logged."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

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
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def log(self, transcript: str = "", plan=None, safety=None,
            executed: bool = False, result: str = "", error: str = "") -> None:
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
        with self._lock:
            self._conn.execute(
                "INSERT INTO interactions (ts, transcript, intent, tool, arguments,"
                " risk_level, allowed, requires_confirmation, executed, result, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
            self._conn.commit()

    def recent(self, limit: int = 50) -> list:
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, transcript, intent, tool, arguments, risk_level,"
                " allowed, requires_confirmation, executed, result, error"
                " FROM interactions ORDER BY id DESC LIMIT ?", (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM interactions")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
