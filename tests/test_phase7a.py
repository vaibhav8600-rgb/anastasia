"""Phase 7A: confidence routing, fuzzy recovery, and window rules."""

import sys
import time
import types

import pytest

from app.agent.pipeline import GARBLE_MESSAGE, CommandPipeline
from app.agent.router import match_rule
from app.llm.intent_parser import ActionPlan
from app.voice.stt_whisper import SpeechConfidence
from tests.fakes import FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech, make_config


def make_pipeline(config=None, agent=None, **kwargs):
    config = config or make_config()
    agent = agent or FakeAgent(config, rule=lambda text: match_rule(text, config))
    ui, speech = FakePipelineUI(), FakeSpeech()
    pipeline = CommandPipeline(config, agent, FakeHistory(), ui, speech,
                               run_async=False, **kwargs)
    return pipeline, ui, agent


def normal_confidence():
    return SpeechConfidence(avg_logprob=-0.25, no_speech_prob=0.05,
                            compression_ratio=1.1)


def test_fluent_unmatched_sentence_routes_to_llm():
    config = make_config()
    agent = FakeAgent(config, rule=lambda _text: None,
                      llm_plan=ActionPlan(intent="no_action",
                                          assistant_message="Got it."))
    pipeline, ui, agent = make_pipeline(config, agent)
    pipeline.submit("Open YouTube and search AI videos", source="voice",
                    confidence=normal_confidence())
    assert agent.llm_calls == 1
    assert GARBLE_MESSAGE not in ui.annas


def test_low_confidence_audio_routes_to_garble():
    config = make_config()
    agent = FakeAgent(config, rule=lambda _text: None)
    pipeline, ui, agent = make_pipeline(config, agent)
    pipeline.submit("Ryno mi hw ar an", source="voice",
                    confidence=SpeechConfidence(avg_logprob=-1.2,
                                                no_speech_prob=0.1))
    assert GARBLE_MESSAGE in ui.annas
    assert agent.llm_calls == 0


def test_silence_high_no_speech_prob_not_marked_busy():
    pipeline, ui, agent = make_pipeline()
    pipeline.submit("Thank you", source="voice",
                    confidence=SpeechConfidence(avg_logprob=-0.2,
                                                no_speech_prob=0.9))
    assert not pipeline.is_processing_command
    assert "thinking" not in ui.states
    assert ui.users == [] and agent.llm_calls == 0


def test_fuzzy_open_pink_offers_paint_confirmation():
    pipeline, ui, _agent = make_pipeline()
    pipeline.submit("Open Pink", source="voice", confidence=normal_confidence())
    assert pipeline.pending is not None
    assert pipeline.pending.kind == "fuzzy"
    assert pipeline.pending.plan.arguments == {"app_name": "paint"}
    assert ui.confirmation_meta[-1] == ("fuzzy", "Did you mean Paint?")
    pipeline.cancel_pending()


def test_fuzzy_open_crome_executes_chrome_directly():
    pipeline, _ui, agent = make_pipeline()
    pipeline.submit("open crome", source="voice", confidence=normal_confidence())
    assert pipeline.pending is None
    assert agent.executed[-1].arguments == {"app_name": "chrome"}


def test_fuzzy_confirmation_yes_executes():
    pipeline, _ui, agent = make_pipeline()
    pipeline.submit("Open Pink", source="voice", confidence=normal_confidence())
    pipeline.submit("haan", source="voice", confidence=normal_confidence())
    assert pipeline.pending is None
    assert agent.executed[-1].arguments == {"app_name": "paint"}


def test_fuzzy_confirmation_timeout_cancels_and_resets_state():
    pipeline, ui, agent = make_pipeline(fuzzy_timeout_seconds=0.05)
    pipeline.submit("Open Pink", source="voice", confidence=normal_confidence())
    deadline = time.time() + 2
    while pipeline.pending is not None and time.time() < deadline:
        time.sleep(0.01)
    deadline = time.time() + 1
    while (not ui.infos or ui.states[-1] != "ready") and time.time() < deadline:
        time.sleep(0.01)
    assert pipeline.pending is None
    assert ui.states[-1] == "ready"
    assert not agent.executed


def test_close_chrome_is_rule_routed_with_confirmation():
    pipeline, _ui, agent = make_pipeline()
    pipeline.submit("Close Chrome", source="voice", confidence=normal_confidence())
    assert agent.llm_calls == 0
    assert pipeline.pending.plan.intent == "window_control"
    assert pipeline.pending.plan.arguments == {"action": "close", "app": "chrome"}
    assert pipeline.pending.kind == "safety"
    pipeline.cancel_pending()


def test_open_youtube_and_search_matches_browser_rule():
    plan = match_rule("Open YouTube and search AI videos", make_config())
    assert plan.intent == "browser_open"
    assert "youtube.com/results" in plan.arguments["url"]
    assert "AI+videos".lower() in plan.arguments["url"].lower()


def test_garble_thresholds_configurable():
    config = make_config(stt={"garble": {"avg_logprob": -2.0,
                                         "no_speech_prob": 0.95,
                                         "compression_ratio": 3.5}})
    agent = FakeAgent(config, rule=lambda _text: None)
    pipeline, ui, agent = make_pipeline(config, agent)
    pipeline.submit("unmatched fluent words", source="voice",
                    confidence=SpeechConfidence(avg_logprob=-1.4,
                                                no_speech_prob=0.7,
                                                compression_ratio=2.8))
    assert agent.llm_calls == 1
    assert GARBLE_MESSAGE not in ui.annas


def test_faster_whisper_confidence_signals_are_captured(monkeypatch):
    import app.voice.stt_whisper as stt

    segments = [
        types.SimpleNamespace(text=" hello", avg_logprob=-0.4,
                              no_speech_prob=0.1, compression_ratio=1.2),
        types.SimpleNamespace(text=" world", avg_logprob=-0.8,
                              no_speech_prob=0.3, compression_ratio=1.8),
    ]

    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            return iter(segments), object()

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(stt, "_fw_model", None)
    monkeypatch.setattr(stt, "_fw_model_name", None)
    result = stt.transcribe_wav("unused.wav", make_config())
    assert result.text == "hello world"
    assert result.confidence.avg_logprob == pytest.approx(-0.6)
    assert result.confidence.no_speech_prob == pytest.approx(0.2)
    assert result.confidence.compression_ratio == pytest.approx(1.8)
