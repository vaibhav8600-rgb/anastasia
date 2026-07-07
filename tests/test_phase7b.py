"""Phase 7B: Whisper accuracy settings, prompt biasing, and STT telemetry."""

import sys
import types

import numpy as np

from app.agent.devlog import CommandTrace
from app.voice.recorder import Recorder, normalize_audio_for_stt
from app.voice.stt_whisper import build_stt_prompt
from tests.fakes import make_config


def run_fake_transcription(monkeypatch, config=None):
    import app.voice.stt_whisper as stt

    captured = {}
    segment = types.SimpleNamespace(text=" open paint", avg_logprob=-0.2,
                                    no_speech_prob=0.05,
                                    compression_ratio=1.1)

    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return iter([segment]), object()

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(stt, "_fw_model", None)
    monkeypatch.setattr(stt, "_fw_model_name", None)
    result = stt.transcribe_wav("unused.wav", config or make_config())
    return captured, result


def test_transcribe_called_with_beam_size_and_vad(monkeypatch):
    captured, result = run_fake_transcription(monkeypatch)
    kwargs = captured["kwargs"]
    assert kwargs["language"] == "en"
    assert kwargs["beam_size"] == 5
    assert kwargs["vad_filter"] is True
    assert kwargs["vad_parameters"] == {"min_silence_duration_ms": 400}
    assert result.text == "open paint"


def test_condition_on_previous_text_disabled(monkeypatch):
    captured, _result = run_fake_transcription(monkeypatch)
    assert captured["kwargs"]["condition_on_previous_text"] is False


def test_initial_prompt_built_from_config_aliases():
    config = make_config(
        app_aliases={"paint": "mspaint.exe", "my studio": "studio.exe"},
        safe_folders=["C:/Users/Test/Recordings"],
    )
    prompt = build_stt_prompt(config)
    assert "Anna" in prompt
    assert "paint" in prompt and "my studio" in prompt
    assert "Recordings" in prompt
    assert "open" in prompt and "close" in prompt


def test_initial_prompt_capped_length():
    aliases = {f"custom application number {index}": f"app{index}.exe"
               for index in range(100)}
    prompt = build_stt_prompt(make_config(app_aliases=aliases))
    assert len(prompt) <= 200
    assert prompt.endswith(".")


def test_stt_ms_present_in_timing_log(monkeypatch):
    _captured, result = run_fake_transcription(monkeypatch)
    assert result.stt_ms >= 0
    trace = CommandTrace(source="voice", normalized="open paint",
                         stt_ms=123.4)
    assert "stt_ms: 123ms" in trace.format()


def test_audio_normalized_to_16khz_mono():
    stereo = np.zeros((48000, 2), dtype=np.int16)
    stereo[:, 0] = 1000
    normalized = normalize_audio_for_stt(stereo, source_rate=48000)
    assert normalized.dtype == np.int16
    assert normalized.ndim == 1
    assert len(normalized) == 16000
    assert 490 <= int(normalized.max()) <= 510


def test_microphone_device_logged_once_per_session(monkeypatch):
    from app.agent.devlog import devlog

    class FakeStream:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake_sd = types.SimpleNamespace(
        InputStream=FakeStream,
        query_devices=lambda **_kwargs: {
            "name": "Test microphone", "default_samplerate": 48000,
        },
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(devlog, "echo_to_stdout", False)
    devlog.clear()
    recorder = Recorder(make_config())
    recorder.start()
    recorder.stop()
    recorder.start()
    recorder.stop()
    messages = [entry["message"] for entry in devlog.entries()
                if entry["message"].startswith("Microphone input:")]
    assert len(messages) == 1
    assert "Test microphone" in messages[0] and "16000Hz mono" in messages[0]


def test_low_microphone_gain_hint_shown_once(monkeypatch):
    from app.main import Controller
    from app.voice.stt_whisper import TranscriptionResult
    from tests.fakes import FakeHistory, FakeMainUI

    class LowRecorder:
        def __init__(self):
            self.rates = []

        def stop(self):
            return np.full((16000, 1), 100, dtype=np.int16)

        def save_wav(self, _data, _path, sample_rate=None):
            self.rates.append(sample_rate)

    ui = FakeMainUI()
    controller = Controller(ui=ui, autostart=False, config=make_config(),
                            memory=object(), history=FakeHistory())
    controller.speech.shutdown()
    recorder = LowRecorder()
    controller.recorder = recorder
    monkeypatch.setattr("app.voice.stt_whisper.transcribe_wav",
                        lambda *_args: TranscriptionResult(text="hello"))
    controller._transcribe_recording()
    controller._transcribe_recording()
    hints = [message for message in ui.messages["info"]
             if "mic level seems very low" in message]
    assert len(hints) == 1
    assert recorder.rates == [16000, 16000]
