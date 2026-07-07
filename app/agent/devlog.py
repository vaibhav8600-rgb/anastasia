"""Developer log channel — technical detail never shown in the main chat.

Everything diagnostic (raw transcripts, intent JSON, timing breakdowns,
exceptions, dependency warnings) goes here. The GUI's Developer Tools
panel subscribes to render entries; stdout echo helps when run from a
terminal.
"""

import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field


@dataclass
class CommandTrace:
    """Per-command timing breakdown (section 11 of the spec)."""

    source: str = "typed"           # typed | voice | wake_word
    raw: str = ""                   # full raw transcript
    normalized: str = ""            # cleaned command actually routed
    route: str = ""                 # rule | llm | garble | none
    intent: str = ""
    args: dict = field(default_factory=dict)
    llm_used: bool = False
    stt_ms: float = 0.0
    routing_ms: float = 0.0
    llm_ms: float = 0.0
    safety_ms: float = 0.0
    tool_ms: float = 0.0
    tts_ms: float = 0.0             # time to queue speech (TTS itself is async)
    total_ms: float = 0.0

    def format(self) -> str:
        return (
            f"Command: {self.normalized}\n"
            f"Source: {self.source}\n"
            f"Raw transcript: {self.raw}\n"
            f"Route: {self.route}\n"
            f"Intent: {self.intent}\n"
            f"Args: {self.args}\n"
            f"LLM used: {str(self.llm_used).lower()}\n"
            f"stt_ms: {self.stt_ms:.0f}ms | Routing: {self.routing_ms:.0f}ms | "
            f"LLM: {self.llm_ms:.0f}ms | "
            f"Safety: {self.safety_ms:.0f}ms | Tool: {self.tool_ms:.0f}ms | "
            f"TTS queued: {self.tts_ms:.0f}ms | Total: {self.total_ms:.0f}ms"
        )


class DevLog:
    """Thread-safe ring buffer of developer log entries with subscribers."""

    def __init__(self, maxlen: int = 800):
        self._entries = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._subscribers = []
        self.echo_to_stdout = True

    def subscribe(self, callback) -> None:
        """callback(entry: dict) — called on the logging thread."""
        with self._lock:
            self._subscribers.append(callback)

    def entries(self, limit: int = 200) -> list:
        with self._lock:
            return list(self._entries)[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------------
    def _emit(self, category: str, message: str) -> None:
        entry = {"ts": time.strftime("%H:%M:%S"), "category": category,
                 "message": message}
        with self._lock:
            self._entries.append(entry)
            subscribers = list(self._subscribers)
        if self.echo_to_stdout:
            try:
                print(f"[{entry['ts']}] [{category}] {message}")
            except Exception:
                pass  # closed stdout (pythonw) must never break logging
        for cb in subscribers:
            try:
                cb(entry)
            except Exception:
                pass

    def log(self, message: str) -> None:
        self._emit("info", message)

    def warn(self, message: str) -> None:
        self._emit("warn", message)

    def error(self, message: str) -> None:
        self._emit("error", message)

    def exception(self, exc: BaseException, context: str = "") -> None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        prefix = f"{context}: " if context else ""
        self._emit("error", f"{prefix}{type(exc).__name__}: {exc}\n{detail}")

    def timing(self, trace: CommandTrace) -> None:
        self._emit("timing", trace.format())


devlog = DevLog()
