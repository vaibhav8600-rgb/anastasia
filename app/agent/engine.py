"""Three-tier conversation engine selector (Phase 10C).

    gemini_live  (premium)   native speech-to-speech, needs key + internet
                             + the explicit billing/privacy opt-in
    pipeline     (reliable)  Phase-9 stack: STT -> brain -> TTS
    local        (floor)     Whisper -> Ollama -> Piper, nothing leaves the PC

The selector never runs a turn — it only decides which stack should, and
tracks Gemini Live health with the same circuit breaker the brain (Phase 8)
and streaming STT (Phase 9) use: 3 consecutive failures -> OPEN 120s ->
the next attempt is the probe -> success closes it.

The default is ALWAYS pipeline: Live streams continuous mic audio to Google
and bills per minute, so it can never be the silent default — it needs
engine_mode="gemini_live" AND live_audio_consent, set knowingly.
"""

import time

from app.agent.devlog import devlog

CIRCUIT_FAILURES = 3
CIRCUIT_COOLDOWN_S = 120.0
ONLINE_CACHE_S = 10.0
ONLINE_PROBE = ("8.8.8.8", 53)   # DNS handshake — cheap, no HTTP, no payload


def _default_online_check() -> bool:
    import socket
    try:
        with socket.create_connection(ONLINE_PROBE, timeout=1.5):
            return True
    except OSError:
        return False


def local_floor(config) -> bool:
    """True when the user picked the fully-offline engine."""
    return getattr(config, "engine_mode", "pipeline") == "local"


class EngineSelector:
    """Decides gemini_live / pipeline / local per turn. Thread-safe enough
    for its use (reads + monotonic counters, same pattern as BrainRouter)."""

    def __init__(self, config, online_check=None):
        self.config = config
        self._online_check = online_check or _default_online_check
        self._online_cached = (0.0, True)
        self._failures = 0
        self._open_until = 0.0
        self.last_reason = ""     # why Live wasn't chosen (chip/devlog)

    # ------------------------------------------------------------ circuit
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def circuit_state(self) -> str:
        if not self.circuit_open():
            return "closed"
        return f"open ({self._open_until - time.monotonic():.0f}s left)"

    def record_failure(self, detail: str = "") -> None:
        self._failures += 1
        devlog.warn(f"Gemini Live failure #{self._failures}: {detail[:150]}")
        if self._failures >= CIRCUIT_FAILURES and not self.circuit_open():
            self._open_until = time.monotonic() + CIRCUIT_COOLDOWN_S
            devlog.warn(f"Live circuit OPEN for {CIRCUIT_COOLDOWN_S:.0f}s — "
                        "the pipeline handles everything meanwhile.")

    def record_success(self) -> None:
        if self._failures or self._open_until:
            self._failures = 0
            self._open_until = 0.0
            devlog.log("Live circuit CLOSED — Gemini Live healthy again.")

    # ------------------------------------------------------------- online
    def online(self) -> bool:
        ts, value = self._online_cached
        now = time.monotonic()
        if now - ts < ONLINE_CACHE_S:
            return value
        value = bool(self._online_check())
        self._online_cached = (now, value)
        return value

    # ------------------------------------------------------------- choice
    def choose(self) -> tuple:
        """(engine, reason). reason is non-empty only when the user asked
        for gemini_live and we fell back — it's honest chip/info text."""
        mode = getattr(self.config, "engine_mode", "pipeline")
        if mode == "local":
            return "local", self._remember("")
        if mode != "gemini_live":
            return "pipeline", self._remember("")

        if not getattr(self.config, "live_audio_consent", False):
            return "pipeline", self._remember(
                "Live needs the privacy + billing opt-in in Settings")
        from app.voice.gemini_live import gemini_live_available
        ok, why = gemini_live_available(self.config)
        if not ok:
            return "pipeline", self._remember(why)
        if self.circuit_open():
            return "pipeline", self._remember(
                f"Live circuit {self.circuit_state()}")
        if not self.online():
            return "pipeline", self._remember("offline")
        return "gemini_live", self._remember("")

    def _remember(self, reason: str) -> str:
        self.last_reason = reason
        return reason
