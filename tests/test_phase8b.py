"""Phase 8B: Piper binary validation (reject pip entry points) + TTS
circuit breaker (bench after 2 failures, single warning, retry restores)."""

import wave

import app.voice.tts_piper as tts_piper
from app.agent.devlog import devlog
from app.voice.speech_output import SpeechOutput, _short_error
from app.voice.tts_piper import (PIP_PIPER_MESSAGE, looks_like_pip_piper,
                                 piper_setup_status, validate_piper_config)
from tests.fakes import FakeHistory, make_config


def make_speech(tmp_path, monkeypatch, backend="auto"):
    """SpeechOutput with Piper 'configured' and both backends stubbed."""
    config = make_config(tts_backend=backend,
                         piper_exe="C:/tools/piper/piper.exe",
                         piper_voice="C:/tools/piper/amy.onnx")
    monkeypatch.setattr("app.voice.tts_piper.piper_available", lambda c: True)
    monkeypatch.setattr("app.voice.speech_output.TTS_ERROR_LOG",
                        tmp_path / "tts_errors.log")
    speech = SpeechOutput(config)
    speech.shutdown()                      # kill the worker; we call _speak()
    calls = {"piper": 0, "windows": 0}

    def bad_piper(text):
        calls["piper"] += 1
        raise RuntimeError("Traceback (most recent call last): " + "x" * 900)

    speech._speak_piper = bad_piper
    speech._speak_windows = lambda text: calls.__setitem__(
        "windows", calls["windows"] + 1)
    return speech, calls


# ---- 8B.1 validation ---------------------------------------------------------

def test_piper_venv_path_rejected_with_specific_error():
    for bad in (r"F:\Projects\anastasia\.venv\Scripts\piper.exe",
                "C:/x/venv/Scripts/piper.exe",
                "C:/Python313/Scripts/piper.exe"):
        assert looks_like_pip_piper(bad), bad
        config = make_config(piper_exe=bad, piper_voice="C:/v/amy.onnx")
        ok, message = piper_setup_status(config)
        assert not ok and message == PIP_PIPER_MESSAGE
    # the real binary location is fine
    assert not looks_like_pip_piper("C:/tools/piper/piper.exe")


def test_piper_probe_requires_nonempty_wav(monkeypatch, tmp_path):
    def fake_synth(text, config, wav_path, timeout=60):
        with wave.open(str(wav_path), "wb") as wf:   # header, zero frames
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
    monkeypatch.setattr(tts_piper, "synthesize_piper", fake_synth)
    ok, message = validate_piper_config(make_config(), play=False)
    assert not ok and "empty" in message.lower()


# ---- 8B.2 circuit breaker -----------------------------------------------------

def test_tts_circuit_opens_after_2_failures_single_warning(tmp_path, monkeypatch):
    devlog.clear()
    devlog.echo_to_stdout = False
    try:
        speech, calls = make_speech(tmp_path, monkeypatch)
        for _ in range(4):
            speech._speak("hello there")
        assert speech.piper_unhealthy
        assert calls["piper"] == 2          # never attempted after the bench
        assert calls["windows"] == 4        # every sentence still spoken
        disabled = [e for e in devlog.entries(100)
                    if "disabled for this session" in e["message"]]
        assert len(disabled) == 1           # ONE warning, no spam
    finally:
        devlog.echo_to_stdout = True


def test_tts_retry_button_reprobes_and_restores(tmp_path, monkeypatch):
    from app.main import Controller
    from app.web.bridge import JsApi, UIBridge
    from tests.test_bridge import FakeMemory, FakeWindow

    bridge = UIBridge(); window = FakeWindow(); bridge.window = window
    controller = Controller(
        ui=bridge, autostart=False, memory=FakeMemory(), history=FakeHistory(),
        config=make_config(tts_backend="piper",
                           piper_exe="C:/tools/piper/piper.exe",
                           piper_voice="C:/tools/piper/amy.onnx"))
    controller.speech.shutdown()
    bridge.controller = controller
    JsApi(bridge).ready()
    controller._piper_ok = True
    controller.speech.piper_unhealthy = True

    controller._push_chips("connected")
    chips = window.of_type("status")[-1]["payload"]["chips"]
    assert chips["voice"] == {"label": "Voice: Windows fallback (Piper error)",
                              "state": "warn"}

    monkeypatch.setattr("app.voice.tts_piper.validate_piper_config",
                        lambda config, play=False: (True, "Piper validated"))
    import time
    controller.validate_piper()
    deadline = time.time() + 2
    while controller.speech.piper_unhealthy and time.time() < deadline:
        time.sleep(0.02)
    assert not controller.speech.piper_unhealthy
    chips = window.of_type("status")[-1]["payload"]["chips"]
    assert chips["voice"]["label"].startswith("Voice: Piper")
    assert chips["voice"]["state"] == "ok"


def test_tts_error_logs_truncated(tmp_path, monkeypatch):
    devlog.clear()
    devlog.echo_to_stdout = False
    try:
        speech, calls = make_speech(tmp_path, monkeypatch)
        speech._speak("hi")
        for entry in devlog.entries(100):
            assert len(entry["message"]) < 320
            assert "\n" not in entry["message"].replace(
                "— using the Windows voice. Fix the paths in Settings "
                "and press Validate Piper to restore it.", "")
        speech._speak("hi again")           # second failure -> file traceback
        log_file = tmp_path / "tts_errors.log"
        assert log_file.exists()
        assert "Traceback" in log_file.read_text(encoding="utf-8")
        assert _short_error(RuntimeError("a" * 999)) == "a" * 200
    finally:
        devlog.echo_to_stdout = True
