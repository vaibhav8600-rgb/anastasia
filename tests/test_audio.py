"""Half-duplex echo-loop fix (spec sec 13a): the mic drops everything while
Anna speaks, and push-to-talk barges in by cancelling TTS."""

import threading
import time

import numpy as np

from app.voice import audio_gate
from app.voice.recorder import Recorder
from app.voice.speech_output import SpeechOutput
from tests.fakes import FakeMainUI, FakeRecorder, FakeSpeech, make_config


def teardown_function(_fn):
    audio_gate.speaking.clear()


def test_stt_suppressed_while_tts_playing():
    recorder = Recorder(make_config())
    recorder._recording = True  # simulate an open stream without hardware
    frame = np.zeros((160, 1), dtype=np.int16)

    audio_gate.speaking.set()          # Anna is talking
    recorder._callback(frame, 160, None, None)
    recorder._callback(frame, 160, None, None)
    assert recorder._frames == []      # every frame dropped

    audio_gate.speaking.clear()        # Anna finished (+tail elapsed)
    recorder._callback(frame, 160, None, None)
    assert len(recorder._frames) == 1  # mic capture resumes


def test_speech_output_sets_gate_for_playback_plus_tail():
    config = make_config(tts_backend="windows")
    speech = SpeechOutput(config)
    gate_during_playback = threading.Event()

    def fake_speak(text):
        if audio_gate.speaking.is_set():
            gate_during_playback.set()

    speech._speak = fake_speak
    speech.speak_async("hello there")

    assert gate_during_playback.wait(2.0)   # gate closed before/during playback
    deadline = time.time() + 2.0            # gate reopens after the 400ms tail
    while audio_gate.speaking.is_set() and time.time() < deadline:
        time.sleep(0.02)
    assert not audio_gate.speaking.is_set()
    speech.shutdown()


def test_speech_cancel_clears_gate_immediately():
    config = make_config(tts_backend="windows")
    speech = SpeechOutput(config)
    started = threading.Event()

    def slow_speak(text):
        started.set()
        speech._cancel.wait(5.0)

    speech._speak = slow_speak
    speech.speak_async("a very long sentence")
    assert started.wait(2.0)
    assert audio_gate.speaking.is_set()
    speech.cancel()                     # barge-in path
    assert not audio_gate.speaking.is_set()
    speech.shutdown()


def test_ptt_barge_in_cancels_tts():
    from app.main import Controller
    from tests.fakes import FakeHistory

    ui = FakeMainUI()
    controller = Controller(ui=ui, autostart=False, config=make_config(),
                            memory=object(), history=FakeHistory())
    controller.speech.shutdown()        # replace the real TTS worker
    speech = FakeSpeech()
    speech._speaking = True             # Anna is mid-sentence
    controller.speech = speech
    controller.pipeline.speech = speech
    recorder = FakeRecorder()
    controller.recorder = recorder

    controller.toggle_mic()             # PTT pressed while Anna speaks

    assert speech.cancelled == 1        # TTS cut off immediately
    assert recorder.starts == 1         # and we're listening
    assert controller.ui.states[-1] == "listening"
