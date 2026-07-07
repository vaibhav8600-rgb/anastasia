"""Phase 9A: streaming STT — Deepgram provider, partial/final routing,
circuit breaker, fallback to Whisper, live-audio privacy gate, indicator."""

import json
import time

import pytest

from app.llm.providers import DataClass, PrivacyViolation, cloud_allowed
from app.voice.stt_providers import (STTResult, STTRouter, DeepgramSTT,
                                     stt_stream_allowed)
from tests.fakes import FakeHistory, make_config


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def dg_frame(text, is_final=False, speech_final=False, conf=0.95):
    return json.dumps({
        "type": "Results",
        "channel": {"alternatives": [{"transcript": text, "confidence": conf}]},
        "is_final": is_final, "speech_final": speech_final,
    })


def streaming_config(**over):
    over.setdefault("stt_mode", "streaming")
    over.setdefault("deepgram_api_key", "dg_TESTKEY1234567890")
    return make_config(**over)


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False
    def send(self, data, opcode=None): self.sent.append((data, opcode))
    def close(self): self.closed = True


def make_stream(config=None):
    config = config or streaming_config()
    provider = DeepgramSTT(config)
    partials, finals, errors = [], [], []
    # bypass the real socket: capture the DeepgramStream and give it a fake ws
    provider._connect = lambda stream: setattr(stream, "ws", FakeWS()) or stream.ws
    stream = provider.start_stream(
        on_partial=lambda t, e: partials.append((t, e)),
        on_final=finals.append,
        on_error=errors.append)
    return stream, partials, finals, errors


# ---- partial / final routing -------------------------------------------------

def test_deepgram_partial_and_final_callbacks_route_correctly():
    stream, partials, finals, errors = make_stream()
    stream._handle_message(dg_frame("hello", is_final=False))
    stream._handle_message(dg_frame("hello how", is_final=False))
    stream._handle_message(dg_frame("hello how are you", is_final=True, speech_final=True))
    assert [p[0] for p in partials] == ["hello", "hello how"]
    assert len(finals) == 1
    assert finals[0].text == "hello how are you"
    assert finals[0].provider == "deepgram" and finals[0].ok
    # empty transcripts ignored
    stream._handle_message(dg_frame("", is_final=True, speech_final=True))
    assert len(finals) == 1


def test_final_transcript_routes_same_as_whisper_path():
    """The final STTResult carries the same fields the Whisper path submits."""
    stream, _, finals, _ = make_stream()
    stream._handle_message(dg_frame("open notepad", is_final=True, speech_final=True))
    r = finals[0]
    assert r.text == "open notepad"
    assert 0.0 <= r.confidence <= 1.0
    assert r.stt_ms >= 0


def test_socket_closes_when_mic_closes():
    stream, _, _, _ = make_stream()
    ws = stream.ws
    stream.close()
    assert ws.closed and stream.closed


def test_send_audio_forwards_binary_frames():
    stream, _, _, _ = make_stream()
    stream.send_audio(b"\x00\x01" * 160)
    assert stream.ws.sent and stream.ws.sent[0][1] == 0x2  # binary opcode


# ---- privacy gate ------------------------------------------------------------

def test_live_audio_stream_class_only_allowed_to_deepgram():
    # never allowed to the BRAIN (Groq/Ollama)
    ok, reason = cloud_allowed({DataClass.LIVE_AUDIO_STREAM}, streaming_config())
    assert not ok and "never leaves" in reason
    # Deepgram gate: allowed only in streaming mode with a key
    allowed, _ = stt_stream_allowed(streaming_config())
    assert allowed
    allowed, why = stt_stream_allowed(make_config(stt_mode="local",
                                                  deepgram_api_key="dg_x"))
    assert not allowed and "streaming mode is off" in why


def test_local_mode_never_opens_deepgram_socket():
    config = make_config(stt_mode="local", deepgram_api_key="dg_TESTKEY123456")
    provider = DeepgramSTT(config)
    provider._connect = lambda s: (_ for _ in ()).throw(
        AssertionError("socket opened in local mode!"))
    with pytest.raises(PrivacyViolation):
        provider.start_stream(lambda *a: None, lambda *a: None, lambda *a: None)


def test_no_key_means_local_mode():
    router = STTRouter(make_config(stt_mode="streaming", deepgram_api_key=""))
    assert router.mode() == "local"
    assert not router.use_streaming()


# ---- circuit breaker ---------------------------------------------------------

def test_stt_circuit_opens_after_3_failures_and_probes():
    router = STTRouter(streaming_config())
    assert router.use_streaming()
    for _ in range(3):
        router.record_failure("boom")
    assert router.circuit_open() and not router.use_streaming()
    router._open_until = time.monotonic() - 1     # cooldown elapsed
    assert not router.circuit_open()
    router.record_success()
    assert router._failures == 0


# ---- controller integration --------------------------------------------------

def make_controller(**config_over):
    from app.main import Controller
    from tests.fakes import FakeMainUI
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=streaming_config(**config_over),
                            memory=_Mem(), history=FakeHistory())
    controller.speech.shutdown()
    return controller


def test_streaming_indicator_state_emitted_when_socket_open(monkeypatch):
    from app.web.bridge import JsApi, UIBridge
    from tests.test_bridge import FakeMemory, FakeWindow
    bridge = UIBridge(); window = FakeWindow(); bridge.window = window
    from app.main import Controller
    controller = Controller(ui=bridge, autostart=False, config=streaming_config(),
                            memory=FakeMemory(), history=FakeHistory())
    controller.speech.shutdown()
    bridge.controller = controller
    JsApi(bridge).ready()

    # stub the socket + recorder so no hardware/network is touched
    controller.stt_router.deepgram._connect = lambda s: setattr(s, "ws", FakeWS()) or s.ws
    monkeypatch.setattr(controller.recorder, "start", lambda on_auto_stop=None: None)
    monkeypatch.setattr(type(controller.recorder), "recording",
                        property(lambda self: controller._active_stream is not None))

    controller.toggle_mic()
    events = [e for e in window.of_type("stt_streaming")]
    assert events and events[-1]["payload"]["active"] is True
    assert controller._active_stream is not None


def test_deepgram_failure_falls_back_to_whisper_on_buffered_audio(monkeypatch):
    controller = make_controller()
    fell_back = []
    controller._finish_recording = lambda gen: fell_back.append(gen)
    controller.stt_router.deepgram._connect = lambda s: setattr(s, "ws", FakeWS()) or s.ws
    stream = controller.stt_router.deepgram.start_stream(
        controller._on_stt_partial, controller._on_stt_final, controller._on_stt_error)
    controller._active_stream = stream
    controller._stream_generation = controller._stt_generation

    controller._on_stt_error(STTResult(provider="deepgram", error="network",
                                       error_detail="socket dropped"))
    assert fell_back == [controller._stt_generation]   # Whisper on buffer
    assert controller._active_stream is None           # socket closed
    assert controller.stt_router._failures == 1        # counted for the breaker


class _Mem:
    def get(self, key, default=None):
        return "Vaibhav" if key == "user_name" else default
    def set(self, key, value): pass
