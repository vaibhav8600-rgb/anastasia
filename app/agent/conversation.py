"""User-facing conversation model — the single source of truth for what the
chat shows (spec sections 4/5).

Every entry is structured, never a raw log line. Action results carry a
payload (intent, success, data such as a screenshot path) so richer
frontends can render result cards with View/Copy/Save buttons; simpler
frontends just render the text. The web UI (Phase 3) re-hydrates itself
from snapshot().

Developer/diagnostic output NEVER goes through here — that's devlog.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatMessage:
    role: str                     # "user" | "anna" | "info" | "error"
    text: str
    ts: str = ""                  # "10:42 AM"
    action: Optional[dict] = None # {"intent","success","data"} for result cards

    def __post_init__(self):
        if not self.ts:
            self.ts = time.strftime("%I:%M %p").lstrip("0")

    def to_dict(self) -> dict:
        return {"role": self.role, "text": self.text, "ts": self.ts,
                "action": self.action}


class Conversation:
    """Thread-safe ordered chat log with change subscribers."""

    def __init__(self, maxlen: int = 400):
        self._entries: list = []
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self, callback) -> None:
        """callback(message: ChatMessage) — called on the posting thread."""
        with self._lock:
            self._subscribers.append(callback)

    def add(self, role: str, text: str, action: Optional[dict] = None) -> ChatMessage:
        message = ChatMessage(role=role, text=text, action=action)
        with self._lock:
            self._entries.append(message)
            if len(self._entries) > self._maxlen:
                self._entries = self._entries[-self._maxlen:]
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(message)
            except Exception:
                pass
        return message

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def snapshot(self) -> list:
        """Serializable copy for full_state re-hydration (web UI, Phase 3)."""
        with self._lock:
            return [m.to_dict() for m in self._entries]
