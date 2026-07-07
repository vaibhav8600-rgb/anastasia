"""Phase 5: voice settings — rate/volume mapping, settings roundtrip,
test hooks, choice validation."""

from app.voice.speech_output import piper_length_scale, sapi_rate
from tests.fakes import FakeHistory, make_config


def make_web_controller():
    from app.main import Controller
    from app.web.bridge import JsApi, UIBridge
    from tests.test_bridge import FakeMemory, FakeWindow
    bridge = UIBridge()
    window = FakeWindow()
    bridge.window = window
    controller = Controller(ui=bridge, autostart=False, config=make_config(),
                            memory=FakeMemory(), history=FakeHistory())
    controller.speech.shutdown()
    bridge.controller = controller
    JsApi(bridge).ready()
    return bridge, window, controller


# ---- rate/volume mapping -----------------------------------------------------

def test_sapi_rate_mapping():
    assert sapi_rate(1.0) == 0
    assert sapi_rate(0.5) == -5
    assert sapi_rate(2.0) == 10
    assert sapi_rate(3.0) == 10      # clamped
    assert sapi_rate(0.1) == -9


def test_piper_length_scale_inverts_rate():
    assert piper_length_scale(1.0) == 1.0
    assert piper_length_scale(2.0) == 0.5
    assert piper_length_scale(0.5) == 2.0
    assert piper_length_scale(0) == 1.0   # guarded against zero


# ---- settings roundtrip ------------------------------------------------------

def test_voice_settings_roundtrip():
    bridge, window, controller = make_web_controller()
    controller.save_settings({
        "chat_model": "llama3.2:1b",
        "tts_backend": "windows", "tts_rate": 1.4, "tts_volume": 70,
        "piper_exe": "C:/tools/piper/piper.exe",
        "piper_voice": "C:/tools/piper/amy.onnx",
        "piper_length_scale": 1.08,
        "kokoro_model": "C:/tools/kokoro/kokoro-v1.0.onnx",
        "kokoro_voices": "C:/tools/kokoro/voices-v1.0.bin",
        "kokoro_voice": "af_bella",
        "faster_whisper_model": "small", "stt_language": "en",
        "silence_seconds": 2.0, "max_record_seconds": 12,
    })
    c = controller.config
    assert (c.tts_backend, c.tts_rate, c.tts_volume) == ("windows", 1.4, 70)
    assert c.chat_model == "llama3.2:1b"
    assert c.piper_exe.endswith("piper.exe")
    assert c.piper_length_scale == 1.08 and c.kokoro_voice == "af_bella"
    assert (c.faster_whisper_model, c.stt_language) == ("small", "en")
    assert (c.silence_seconds, c.max_record_seconds) == (2.0, 12)

    controller.open_settings()
    payload = window.of_type("settings")[-1]["payload"]
    assert payload["tts_rate"] == 1.4
    assert payload["chat_model"] == "llama3.2:1b"
    assert payload["stt_language"] == "en"
    assert payload["piper_voice"].endswith("amy.onnx")
    assert payload["kokoro_model"].endswith("kokoro-v1.0.onnx")


def test_invalid_choices_are_rejected():
    bridge, window, controller = make_web_controller()
    before = controller.config.tts_backend
    controller.save_settings({"tts_backend": "espeak; rm -rf /",
                              "faster_whisper_model": "gigantic",
                              "stt_language": "klingon"})
    assert controller.config.tts_backend == before
    assert controller.config.faster_whisper_model == "base"
    assert controller.config.stt_language == "en"


def test_english_whisper_models_are_valid_choices():
    _bridge, _window, controller = make_web_controller()
    controller.save_settings({"faster_whisper_model": "small.en"})
    assert controller.config.faster_whisper_model == "small.en"
    controller.save_settings({"faster_whisper_model": "base.en"})
    assert controller.config.faster_whisper_model == "base.en"


def test_piper_selected_but_unconfigured_warns():
    bridge, window, controller = make_web_controller()
    controller.save_settings({"tts_backend": "piper"})
    warnings = [e for e in window.of_type("anna_message")
                if "Piper" in e["payload"].get("text", "")]
    assert warnings, "expected a friendly Piper-not-configured warning"


# ---- test hooks ----------------------------------------------------------------

def test_test_voice_queues_sample_speech():
    bridge, window, controller = make_web_controller()
    spoken = []
    controller.speech.speak_async = spoken.append
    controller.test_voice()
    assert spoken and "how I sound" in spoken[0]
    result = window.of_type("test_result")[-1]["payload"]
    assert result["kind"] == "voice" and result["ok"] is True


def test_test_model_reports_unreachable():
    import time
    bridge, window, controller = make_web_controller()

    class DeadLLM:
        def is_available(self): return False

    controller.agent.llm = DeadLLM()
    controller.test_model()
    deadline = time.time() + 2.0
    while not window.of_type("test_result") and time.time() < deadline:
        time.sleep(0.01)
    result = window.of_type("test_result")[-1]["payload"]
    assert result["kind"] == "model" and result["ok"] is False
