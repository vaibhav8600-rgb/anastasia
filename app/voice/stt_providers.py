"""Streaming/batch STT provider abstraction + router (Phase 9A).

DeepgramSTT — live WebSocket streaming (nova-2); final transcript ~200-400ms
              after the speaker stops. Sends live mic audio to Deepgram ONLY
              while the mic is open, and only in stt_mode="streaming".
WhisperSTT  — the existing faster-whisper batch path; the offline/private
              fallback, exactly as Ollama is the fallback for the brain.
STTRouter   — provider selection, circuit breaker (3 fails -> open 120s ->
              probe), and the live-audio privacy gate.

Mirrors the Phase 8 BrainRouter patterns deliberately (same circuit breaker,
same key hygiene: key from env DEEPGRAM_API_KEY or config, never logged).
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field

from app.agent.devlog import devlog
from app.llm.providers import DataClass, PrivacyViolation, mask_key

CIRCUIT_FAILURES = 3
CIRCUIT_COOLDOWN_S = 120.0
DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model={model}&language=en&smart_format=true&interim_results=true"
    "&endpointing=300&encoding=linear16&sample_rate=16000&channels=1"
)


@dataclass
class STTResult:
    text: str = ""
    confidence: float = 1.0
    provider: str = ""
    stt_ms: float = 0.0
    error: str = ""            # "" | auth | network | timeout | bad_response
    error_detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def deepgram_key(config) -> str:
    return os.environ.get("DEEPGRAM_API_KEY") or \
        getattr(config, "deepgram_api_key", "") or ""


def stt_stream_allowed(config) -> tuple[bool, str]:
    """Live-audio privacy gate: streaming to Deepgram is only permitted in
    streaming mode with a key. Anything else must stay on local Whisper."""
    if getattr(config, "stt_mode", "local") != "streaming":
        return False, "streaming mode is off — audio stays on-device"
    if not deepgram_key(config):
        return False, "no Deepgram key configured"
    return True, ""


class DeepgramStream:
    """One live streaming session. Feed PCM frames via send_audio(); the
    provider emits on_partial/on_final/on_error. close() ends it."""

    def __init__(self, provider, on_partial, on_final, on_error):
        self.provider = provider
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self.ws = None
        self.closed = False
        self._started = time.perf_counter()
        self._final_sent = False

    # -- Deepgram message handling (tests drive these directly) ----------
    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if msg.get("type") not in (None, "Results"):
            return
        alts = (msg.get("channel", {}) or {}).get("alternatives", [])
        if not alts:
            return
        text = (alts[0].get("transcript") or "").strip()
        confidence = float(alts[0].get("confidence", 1.0) or 1.0)
        if not text:
            return
        if msg.get("is_final") and msg.get("speech_final"):
            if not self._final_sent:
                self._final_sent = True
                ms = (time.perf_counter() - self._started) * 1000
                self.on_final(STTResult(text=text, confidence=confidence,
                                        provider="deepgram", stt_ms=ms))
        elif msg.get("is_final"):
            # an endpointed segment that isn't the utterance end — still final
            # text we can act on if speech_final never arrives before close
            self.on_partial(text, True)
        else:
            self.on_partial(text, False)

    def _handle_error(self, detail: str) -> None:
        if not self.closed:
            self.on_error(STTResult(provider="deepgram", error="network",
                                    error_detail=str(detail)[:150]))

    # -- audio in --------------------------------------------------------
    def send_audio(self, pcm_bytes: bytes) -> None:
        if self.closed or self.ws is None:
            return
        try:
            self.ws.send(pcm_bytes, opcode=0x2)  # binary frame
        except Exception as e:
            self._handle_error(e)

    def finish(self) -> None:
        """Signal end-of-speech; Deepgram flushes a final result."""
        if self.ws is not None and not self.closed:
            try:
                self.ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

    def close(self) -> None:
        self.closed = True
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


class DeepgramSTT:
    name = "deepgram"

    def __init__(self, config):
        self.config = config

    def configured(self) -> bool:
        return bool(deepgram_key(self.config))

    def health_check(self) -> bool:
        # Cheap: a key is present. A real socket open is the true probe, done
        # lazily on first stream (failures feed the circuit breaker).
        return self.configured()

    def start_stream(self, on_partial, on_final, on_error) -> DeepgramStream:
        # Hard privacy gate — live audio only leaves in streaming mode.
        allowed, reason = stt_stream_allowed(self.config)
        if not allowed:
            raise PrivacyViolation(f"live audio not permitted: {reason}")
        stream = DeepgramStream(self, on_partial, on_final, on_error)
        stream.ws = self._connect(stream)
        return stream

    def _connect(self, stream: DeepgramStream):
        """Open the WebSocket. Isolated so tests can monkeypatch it."""
        import websocket  # websocket-client
        url = DEEPGRAM_URL.format(model=self.config.deepgram_model)
        ws = websocket.WebSocketApp(
            url,
            header={"Authorization": f"Token {deepgram_key(self.config)}"},
            on_message=lambda _ws, m: stream._handle_message(m),
            on_error=lambda _ws, e: stream._handle_error(e),
        )
        thread = threading.Thread(target=ws.run_forever, daemon=True,
                                  name="anna-deepgram")
        thread.start()
        # give the socket a moment to connect
        for _ in range(50):
            if getattr(ws, "sock", None) and ws.sock and ws.sock.connected:
                break
            time.sleep(0.01)
        return ws


class WhisperSTT:
    name = "whisper"

    def __init__(self, config):
        self.config = config

    def health_check(self) -> bool:
        from app.voice.stt_whisper import backend_ready
        return backend_ready(self.config)[0]

    def transcribe_batch(self, wav_path: str) -> STTResult:
        from app.voice.stt_whisper import transcribe_wav
        started = time.perf_counter()
        result = transcribe_wav(wav_path, self.config)
        return STTResult(text=result.text, confidence=result.confidence,
                         provider="whisper",
                         stt_ms=getattr(result, "stt_ms", None)
                         or (time.perf_counter() - started) * 1000)


class STTRouter:
    """STT provider selection + circuit breaker + live-audio privacy gate."""

    def __init__(self, config):
        self.config = config
        self.deepgram = DeepgramSTT(config)
        self.whisper = WhisperSTT(config)
        self.on_state_change = None       # callback() — chip/indicator refresh
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def mode(self) -> str:
        """Effective mode: streaming only with a key AND the toggle on."""
        allowed, _ = stt_stream_allowed(self.config)
        return "streaming" if allowed else "local"

    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def circuit_state(self) -> str:
        return "closed" if not self.circuit_open() \
            else f"open ({self._open_until - time.monotonic():.0f}s left)"

    def use_streaming(self) -> bool:
        return self.mode() == "streaming" and not self.circuit_open()

    def _notify(self) -> None:
        if self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                pass

    def record_failure(self, detail: str = "") -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= CIRCUIT_FAILURES and not self.circuit_open():
                self._open_until = time.monotonic() + CIRCUIT_COOLDOWN_S
                devlog.warn(f"STT circuit OPEN for {CIRCUIT_COOLDOWN_S:.0f}s "
                            f"after {self._failures} Deepgram failures "
                            "— using local Whisper.")
                self._notify()
        if detail:
            devlog.warn(f"Deepgram failure ({str(detail)[:120]}); "
                        "falling back to local Whisper for this turn.")

    def record_success(self) -> None:
        with self._lock:
            if self._failures or self._open_until:
                self._failures = 0
                self._open_until = 0.0
                devlog.log("STT circuit CLOSED — Deepgram healthy again.")
                self._notify()

    def masked_key(self) -> str:
        return mask_key(deepgram_key(self.config))

    def info(self) -> dict:
        return {"mode": self.mode(), "circuit": self.circuit_state(),
                "model": getattr(self.config, "deepgram_model", "")}
