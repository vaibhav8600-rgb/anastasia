"""Phase 7C: natural TTS delivery, setup diagnostics, and warm responses."""

import os
import time

from app.agent.responses import WarmActionResponses
from app.llm.intent_parser import ActionPlan
from app.voice import _clean_for_speech, split_sentences
from app.voice.speech_output import SpeechOutput
from tests.fakes import make_config


def test_tts_text_sanitized_no_emoji_markdown_urls():
    text = "**Hi** 💜 — see [this page](https://example.com) or https://openai.com."
    cleaned = _clean_for_speech(text)
    assert "💜" not in cleaned
    assert "**" not in cleaned and "https://" not in cleaned
    assert "this page" in cleaned and "link" in cleaned


def test_sentence_queue_splits_and_orders():
    speech = SpeechOutput(make_config(tts_backend="windows"))
    spoken = []
    speech._speak = spoken.append
    speech.speak_async("First sentence. Second one! Third?")
    deadline = time.time() + 2
    while len(spoken) < 3 and time.time() < deadline:
        time.sleep(0.01)
    speech.shutdown()
    assert spoken == ["First sentence.", "Second one!", "Third?"]


def test_piper_failure_falls_back_to_sapi_without_crash(monkeypatch):
    speech = SpeechOutput(make_config(tts_backend="piper"))
    spoken = []
    monkeypatch.setattr("app.voice.tts_piper.piper_available", lambda _cfg: True)
    monkeypatch.setattr(speech, "_speak_piper",
                        lambda _text: (_ for _ in ()).throw(RuntimeError("bad voice")))
    monkeypatch.setattr(speech, "_speak_windows", spoken.append)
    speech._speak("Hello")
    speech.shutdown()
    assert spoken == ["Hello"]


def test_kokoro_backend_unavailable_shows_setup_not_crash(monkeypatch):
    from app.voice import tts_kokoro
    monkeypatch.setattr(tts_kokoro.importlib.util, "find_spec", lambda _name: None)
    ok, message = tts_kokoro.kokoro_setup_status(
        make_config(tts_backend="kokoro"))
    assert not ok
    assert "pip install kokoro-onnx" in message


def test_piper_setup_requires_matching_json(tmp_path):
    from app.voice.tts_piper import piper_setup_status
    exe = tmp_path / "piper.exe"
    voice = tmp_path / "en_US-hfc_female-medium.onnx"
    exe.write_bytes(b"placeholder")
    voice.write_bytes(b"placeholder")
    ok, message = piper_setup_status(
        make_config(piper_exe=str(exe), piper_voice=str(voice)))
    assert not ok and ".onnx.json" in message
    voice.with_suffix(".onnx.json").write_text("{}", encoding="utf-8")
    assert piper_setup_status(
        make_config(piper_exe=str(exe), piper_voice=str(voice)))[0]


def test_piper_setup_auto_detects_exe_and_reports_missing_voice(monkeypatch, tmp_path):
    from app.voice.tts_piper import piper_setup_status
    exe = tmp_path / "piper.exe"
    exe.write_bytes(b"placeholder")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    ok, message = piper_setup_status(make_config(piper_exe="", piper_voice=""))

    assert not ok
    assert "piper.exe was not found" not in message
    assert "voice" in message.lower()


def test_love_frequency_capped_in_rotation():
    responses = WarmActionResponses()
    plan = ActionPlan(intent="open_app", arguments={"app_name": "paint"})
    samples = [responses.next(plan) for _ in range(24)]
    love_positions = [index for index, text in enumerate(samples) if "love" in text]
    assert len(love_positions) <= 4
    assert all(right - left > 1 for left, right in zip(love_positions, love_positions[1:]))
