"""Gemini Live session manager (Phase 10A) — native speech-to-speech.

Verified 2026-07 against ai.google.dev/gemini-api/docs/live-api:
  * Model (Gemini API tier): gemini-3.1-flash-live-preview  (the older
    gemini-live-2.5-flash-native-audio is the Vertex AI variant).
  * SDK: google-genai >= 2.10 — `client.aio.live.connect(model, config)` is an
    async context manager; `session.receive()` yields server messages.
  * Audio IN: raw 16-bit PCM, 16 kHz, little-endian, Blob
    mime_type="audio/pcm;rate=16000".  Audio OUT: raw 16-bit PCM, 24 kHz.
  * Audio-only sessions are capped at 15 minutes; the server sends
    SessionResumptionUpdate.new_handle checkpoints (valid ~2 h) and a
    GoAway(time_left) before terminating; reconnect with
    SessionResumptionConfig(handle=...) to continue seamlessly.
  * Barge-in: server_content.interrupted -> stop local playback, clear queue.
  * Transcripts: input_audio_transcription / output_audio_transcription
    configs; transcripts are DISPLAY/HISTORY ONLY — never an execution path.

Threading: the SDK is asyncio-native; Anna is thread-based. The session runs
its own asyncio loop in a daemon thread; send_audio()/close() are thread-safe
bridges. Tool calls (10B) surface via on_tool_call and are answered with
send_tool_response() — the model NEVER executes anything itself.

The session tracks audio seconds in/out for cost transparency (10D) and
guarantees teardown: close() is idempotent and the loop always exits with the
socket closed — a lingering session bills by the minute.
"""

import asyncio
import threading
import time

from app.agent.devlog import devlog
from app.llm.providers import mask_key

INPUT_RATE = 16000    # bytes/sec = rate * 2 (16-bit mono)
OUTPUT_RATE = 24000


class GeminiLiveUnavailable(Exception):
    pass


def gemini_key(config) -> str:
    import os
    return (os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or getattr(config, "gemini_api_key", "") or "")


def gemini_live_available(config) -> tuple[bool, str]:
    if not gemini_key(config):
        return False, "Add a Gemini API key in Settings."
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False, "Install the SDK: pip install google-genai."
    return True, f"Gemini Live ready ({config.gemini_live_model})."


class GeminiLiveSession:
    """One live conversation. Owns connect/stream/resume/teardown."""

    def __init__(self, config, *, on_audio_out, on_input_transcript=None,
                 on_output_transcript=None, on_interrupted=None,
                 on_tool_call=None, on_closed=None, on_error=None,
                 system_instruction: str = None, tools: list = None):
        self.config = config
        self.on_audio_out = on_audio_out              # callback(pcm24k_bytes)
        self.on_input_transcript = on_input_transcript
        self.on_output_transcript = on_output_transcript
        self.on_interrupted = on_interrupted
        self.on_tool_call = on_tool_call              # callback(name, args, call_id) [10B]
        self.on_closed = on_closed                    # callback(reason)
        self.on_error = on_error                      # callback(detail)
        self.system_instruction = system_instruction
        self.tools = tools or []

        self.active = False
        self.resumption_handle = None
        self.audio_in_seconds = 0.0                   # cost transparency (10D)
        self.audio_out_seconds = 0.0
        self._loop = None
        self._thread = None
        self._in_queue = None                          # asyncio.Queue (audio in)
        self._stop = threading.Event()
        self._closed_reported = False
        self._last_activity = time.monotonic()
        self._awaiting_reply = False                   # stall watchdog scope
        self._started_evt = threading.Event()

    # ------------------------------------------------------------- public
    def start(self) -> None:
        ok, reason = gemini_live_available(self.config)
        if not ok:
            raise GeminiLiveUnavailable(reason)
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="anna-gemini-live")
        self._thread.start()
        if not self._started_evt.wait(10.0) or not self.active:
            self.close("connect failed")
            raise GeminiLiveUnavailable("Gemini Live session did not connect.")

    def send_audio(self, pcm16k_bytes: bytes) -> None:
        """Thread-safe: tee'd recorder frames go into the session."""
        if not self.active or self._loop is None or not pcm16k_bytes:
            return
        self.audio_in_seconds += len(pcm16k_bytes) / (INPUT_RATE * 2)
        try:
            self._loop.call_soon_threadsafe(self._in_queue.put_nowait,
                                            ("audio", pcm16k_bytes))
        except RuntimeError:
            pass  # loop shut down mid-call

    def send_text(self, text: str) -> None:
        """Inject a typed turn into the live conversation."""
        if self.active and self._loop is not None and text:
            self._awaiting_reply = True
            self._touch()
            try:
                self._loop.call_soon_threadsafe(self._in_queue.put_nowait,
                                                ("text", text))
            except RuntimeError:
                pass

    def send_tool_response(self, call_id: str, name: str, result: dict) -> None:
        """10B: answer a tool call after LOCAL validation/execution."""
        if self.active and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(
                    self._in_queue.put_nowait,
                    ("tool_response", (call_id, name, result)))
            except RuntimeError:
                pass

    def close(self, reason: str = "user") -> None:
        """Idempotent, guaranteed teardown — no lingering billed session."""
        self._stop.set()
        self.active = False
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._in_queue.put_nowait,
                                                ("stop", None))
            except RuntimeError:
                pass
        self._report_closed(reason)

    def stats(self) -> dict:
        return {"audio_in_s": round(self.audio_in_seconds, 1),
                "audio_out_s": round(self.audio_out_seconds, 1),
                "resumable": bool(self.resumption_handle)}

    # ---------------------------------------------------------- internals
    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _report_closed(self, reason: str) -> None:
        if not self._closed_reported:
            self._closed_reported = True
            if self.on_closed:
                try:
                    self.on_closed(reason)
                except Exception:
                    pass

    def _report_error(self, detail: str) -> None:
        if self.on_error:
            try:
                self.on_error(str(detail)[:200])
            except Exception:
                pass

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:
            self._report_error(f"live loop: {e}")
        finally:
            self.active = False
            try:
                self._loop.close()
            except Exception:
                pass
            self._report_closed("loop exited")

    def _build_config(self):
        """LiveConnectConfig for this connection (fresh handle each time)."""
        from google.genai import types
        speech = types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name=self.config.gemini_live_voice)))
        kwargs = dict(
            response_modalities=["AUDIO"],
            speech_config=speech,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            session_resumption=types.SessionResumptionConfig(
                handle=self.resumption_handle),
        )
        if self.system_instruction:
            kwargs["system_instruction"] = self.system_instruction
        if self.tools:
            kwargs["tools"] = self.tools
        return types.LiveConnectConfig(**kwargs)

    def _connect(self, config_obj):
        """Return the SDK's async connect context manager. Isolated so tests
        can monkeypatch this with a fake session."""
        from google import genai
        client = genai.Client(api_key=gemini_key(self.config))
        return client.aio.live.connect(model=self.config.gemini_live_model,
                                       config=config_obj)

    async def _run(self) -> None:
        self._in_queue = asyncio.Queue()
        first = True
        while not self._stop.is_set():
            try:
                async with self._connect(self._build_config()) as session:
                    self.active = True
                    self._touch()
                    if first:
                        self._started_evt.set()
                        first = False
                    else:
                        devlog.log("Gemini Live: session resumed seamlessly.")
                    await self._pump(session)
            except Exception as e:
                if first:
                    self._started_evt.set()   # unblock start() with active=False
                    self._report_error(f"connect: {e}")
                    return
                self._report_error(f"session: {e}")
                return
            if self._stop.is_set():
                self.active = False
                return
            # normal server-side termination (15-min cap / GoAway): resume if
            # we have a checkpoint handle, else stop honestly.
            if not self.resumption_handle:
                self.active = False
                self._report_error("session ended without a resumption handle")
                return
            devlog.log("Gemini Live: reconnecting with resumption handle...")

    async def _pump(self, session) -> None:
        """Race recv/send/watchdog/stop — whichever finishes first unwinds the
        rest, so a blocked SDK receive() can never trap a close() or a stall
        teardown (that's what would leave a lingering billed session)."""
        tasks = {asyncio.create_task(self._recv_loop(session)),
                 asyncio.create_task(self._send_loop(session)),
                 asyncio.create_task(self._watchdog()),
                 asyncio.create_task(self._wait_stop())}
        done, pending = await asyncio.wait(tasks,
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()

    async def _wait_stop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.05)

    async def _send_loop(self, session) -> None:
        from google.genai import types
        while not self._stop.is_set():
            kind, payload = await self._in_queue.get()
            if kind == "stop":
                return
            if kind == "audio":
                await session.send_realtime_input(
                    audio=types.Blob(data=payload,
                                     mime_type=f"audio/pcm;rate={INPUT_RATE}"))
            elif kind == "text":
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": payload}]},
                    turn_complete=True)
            elif kind == "tool_response":
                call_id, name, result = payload
                await session.send_tool_response(function_responses=[
                    types.FunctionResponse(id=call_id, name=name,
                                           response=result)])

    async def _watchdog(self) -> None:
        """Mid-turn stall -> teardown so the engine selector can fall back."""
        timeout = float(getattr(self.config, "live_stall_timeout_s", 20.0))
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            if self._awaiting_reply and \
                    time.monotonic() - self._last_activity > timeout:
                self._report_error(f"stalled: no server activity for "
                                   f"{timeout:.0f}s mid-turn")
                self._stop.set()
                try:
                    self._in_queue.put_nowait(("stop", None))
                except Exception:
                    pass
                return

    async def _recv_loop(self, session) -> None:
        async for message in session.receive():
            if self._stop.is_set():
                return
            self._handle_server_message(message)

    # Parsing is defensive getattr-walking so tests can drive it with plain
    # namespaces and SDK message-shape churn degrades gracefully.
    def _handle_server_message(self, message) -> None:
        self._touch()
        update = getattr(message, "session_resumption_update", None)
        if update is not None and getattr(update, "resumable", False):
            handle = getattr(update, "new_handle", None)
            if handle:
                self.resumption_handle = handle

        go_away = getattr(message, "go_away", None)
        if go_away is not None:
            devlog.log(f"Gemini Live: GoAway (time_left="
                       f"{getattr(go_away, 'time_left', '?')}) — will resume.")

        tool_call = getattr(message, "tool_call", None)
        if tool_call is not None and self.on_tool_call:
            for fc in getattr(tool_call, "function_calls", None) or []:
                try:
                    self.on_tool_call(getattr(fc, "name", ""),
                                      dict(getattr(fc, "args", None) or {}),
                                      getattr(fc, "id", ""))
                except Exception as e:
                    self._report_error(f"tool dispatch: {e}")

        content = getattr(message, "server_content", None)
        if content is None:
            return
        if getattr(content, "interrupted", False):
            self._awaiting_reply = False
            if self.on_interrupted:
                try:
                    self.on_interrupted()
                except Exception:
                    pass
        tr_in = getattr(content, "input_transcription", None)
        if tr_in is not None and getattr(tr_in, "text", None):
            self._awaiting_reply = True
            if self.on_input_transcript:
                self.on_input_transcript(tr_in.text)
        tr_out = getattr(content, "output_transcription", None)
        if tr_out is not None and getattr(tr_out, "text", None):
            if self.on_output_transcript:
                self.on_output_transcript(tr_out.text)
        turn = getattr(content, "model_turn", None)
        for part in (getattr(turn, "parts", None) or []):
            blob = getattr(part, "inline_data", None)
            data = getattr(blob, "data", None) if blob else None
            if data:
                self._awaiting_reply = False
                self.audio_out_seconds += len(data) / (OUTPUT_RATE * 2)
                self.on_audio_out(data)
        if getattr(content, "turn_complete", False):
            self._awaiting_reply = False


class LivePcmPlayer:
    """Streams Gemini's 24 kHz PCM chunks to the speakers via sounddevice,
    holding the half-duplex gate (configurable) so the mic doesn't hear Anna.
    stop() clears everything instantly (barge-in / interrupted flag)."""

    def __init__(self, config):
        self.config = config
        self._stream = None
        self._lock = threading.Lock()
        self._pending = 0

    def play_chunk(self, pcm_bytes: bytes) -> None:
        import numpy as np
        from app.voice import audio_gate
        with self._lock:
            if self._stream is None:
                import sounddevice as sd
                self._stream = sd.OutputStream(samplerate=OUTPUT_RATE,
                                               channels=1, dtype="int16")
                self._stream.start()
            if getattr(self.config, "live_half_duplex", True):
                audio_gate.speaking.set()
            self._pending += 1
            try:
                self._stream.write(np.frombuffer(pcm_bytes, dtype=np.int16))
            finally:
                self._pending -= 1
                if self._pending == 0 and getattr(self.config,
                                                  "live_half_duplex", True):
                    audio_gate.speaking.clear()

    def stop(self) -> None:
        """Barge-in: abort playback immediately and drop queued audio."""
        from app.voice import audio_gate
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.abort()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
        audio_gate.speaking.clear()
