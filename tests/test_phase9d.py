"""Phase 9D: consolidated turn_latency_ms telemetry + audio-reactive orb
envelope (gated to the High animation tier)."""

import wave

import numpy as np

from app.voice.speech_output import SpeechOutput
from tests.fakes import FakeHistory, FakeMainUI, make_config


class _Mem:
    def get(self, key, default=None):
        return "Vaibhav" if key == "user_name" else default
    def set(self, key, value): pass


def make_controller(**cfg):
    from app.main import Controller
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(**cfg), memory=_Mem(),
                            history=FakeHistory())
    controller.speech.shutdown()
    return controller


# ---- turn_latency_ms ---------------------------------------------------------

def test_turn_clock_set_on_voice_final_and_consumed_by_first_audio():
    from app.web.bridge import JsApi, UIBridge
    from tests.test_bridge import FakeMemory, FakeWindow
    bridge = UIBridge(); window = FakeWindow(); bridge.window = window
    from app.main import Controller
    controller = Controller(ui=bridge, autostart=False, config=make_config(),
                            memory=FakeMemory(), history=FakeHistory())
    controller.speech.shutdown()
    bridge.controller = controller
    JsApi(bridge).ready()

    controller._start_turn_clock(310.0)
    assert controller._turn_t0 is not None
    assert controller._last_turn_stt_ms == 310.0

    controller._on_first_audio(240.0)             # first audible word
    assert controller._turn_t0 is None            # consumed
    evt = window.of_type("turn_latency")[-1]["payload"]
    assert evt["ms"] >= 0


def test_first_audio_without_turn_clock_is_harmless():
    controller = make_controller()
    controller._turn_t0 = None
    controller._on_first_audio(200.0)             # typed reply: no voice turn
    assert controller.last_tts_first_audio_ms == 200.0


# ---- audio-reactive orb ------------------------------------------------------

def test_emit_levels_gated_to_high_tier():
    hi = make_controller(animation_quality="high")
    assert hi.speech.emit_levels is True
    med = make_controller(animation_quality="medium")
    assert med.speech.emit_levels is False
    # flips live on settings change
    med.save_settings({"animation_quality": "high"})
    assert med.speech.emit_levels is True


def test_wav_envelope_normalizes_and_windows(tmp_path):
    speech = SpeechOutput(make_config(tts_backend="windows"))
    speech.shutdown()
    # 0.4s of 22050Hz: quiet half then loud half -> envelope should rise
    rate = 22050
    quiet = (np.zeros(rate // 5) * 0).astype(np.int16)
    loud = (np.ones(rate // 5) * 12000).astype(np.int16)
    data = np.concatenate([quiet, loud])
    wav = tmp_path / "e.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(data.tobytes())
    env = speech._wav_envelope(wav)
    assert env and max(env) <= 1.0 and min(env) >= 0.0
    assert env[0] < env[-1]                        # quiet -> loud
    assert abs(max(env) - 1.0) < 1e-6              # peak-normalized


def test_audio_level_callback_only_when_enabled(tmp_path):
    speech = SpeechOutput(make_config(tts_backend="windows"))
    speech.shutdown()
    levels = []
    speech.on_audio_level = levels.append
    # emit_levels off -> _play_wav_cancellable takes the simple path
    speech.emit_levels = False
    speech._safe_level(0.5)                        # direct helper still guarded
    assert levels == [0.5]                         # helper fires when callback set
    speech.on_audio_level = None
    speech._safe_level(0.9)                        # no callback -> no crash
    assert levels == [0.5]
