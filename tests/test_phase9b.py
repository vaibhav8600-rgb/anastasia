"""Phase 9B: streamed LLM -> TTS. Groq token streaming, progressive sentence
emission, barge-in abort, and the safety guarantee that command mode never
streams and partial JSON never executes a tool."""

import json

import pytest

import app.llm.providers as providers_module
from app.agent.router import Agent
from app.llm.providers import BrainRouter, LLMResult
from app.voice import StreamingSentencer
from tests.fakes import make_config


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def sse(*chunks):
    """Build a fake SSE byte-line stream like Groq's."""
    lines = []
    for c in chunks:
        lines.append(("data: " + json.dumps(
            {"choices": [{"delta": {"content": c}}]})).encode())
    lines.append(b"data: [DONE]")
    return lines


class FakeStreamResponse:
    def __init__(self, lines, status=200):
        self.status_code = status
        self.headers = {}
        self._lines = lines
    def iter_lines(self): return iter(self._lines)
    def close(self): pass
    def json(self): return {}


def brain_with_stream(monkeypatch, chunks, **cfg):
    cfg.setdefault("groq_api_key", "gsk_TESTKEY12345678")
    config = make_config(**cfg)
    brain = BrainRouter(config, ollama_client=None)
    captured = {}
    def fake_post(url, json=None, stream=False, timeout=None, headers=None):
        captured["stream"] = stream
        captured["payload"] = json
        return FakeStreamResponse(sse(*chunks))
    monkeypatch.setattr(providers_module.requests, "post", fake_post)
    return brain, captured


# ---- progressive sentence emission -------------------------------------------

def test_chat_stream_emits_sentences_progressively(monkeypatch):
    brain, cap = brain_with_stream(
        monkeypatch, ["I'm great", ", thanks", ". ", "How are you", "? ", "Bye"])
    sentences = []
    sentencer = StreamingSentencer()

    def on_token(delta):
        for s in sentencer.feed(delta):
            sentences.append(s)
    result = brain.stream_chat([{"role": "user", "content": "hi"}], on_token=on_token)
    tail = sentencer.flush()
    assert cap["stream"] is True and cap["payload"]["stream"] is True
    assert sentences == ["I'm great, thanks.", "How are you?"]
    assert tail == "Bye"
    assert result.first_token_ms > 0


def test_tts_queue_starts_before_full_reply_received(monkeypatch):
    """The first sentence must reach TTS before the last token arrives."""
    order = []
    brain, _ = brain_with_stream(monkeypatch,
                                 ["Hello there. ", "Second one. ", "Third."])
    sentencer = StreamingSentencer()
    seen_tokens = {"n": 0}

    def on_token(delta):
        seen_tokens["n"] += 1
        for s in sentencer.feed(delta):
            order.append(("speak", s, seen_tokens["n"]))
    brain.stream_chat([{"role": "user", "content": "hi"}], on_token=on_token)
    # first sentence spoken at token 1 of 3 (not after all tokens)
    assert order[0][:2] == ("speak", "Hello there.")
    assert order[0][2] == 1


def test_sentence_boundary_handles_abbreviations():
    s = StreamingSentencer()
    out = []
    for tok in ["Sure Dr. ", "Lee, that's ", "about 3.5 ", "hours. ", "Okay?"]:
        out += s.feed(tok)
    assert out == ["Sure Dr. Lee, that's about 3.5 hours."]
    assert s.flush() == "Okay?"


# ---- barge-in ----------------------------------------------------------------

def test_barge_in_aborts_groq_stream_and_tts_queue(monkeypatch):
    brain, _ = brain_with_stream(
        monkeypatch, ["One. ", "Two. ", "Three. ", "Four. ", "Five."])
    spoken = []
    aborted_after = {"n": 0}

    def on_token(delta):
        aborted_after["n"] += 1
        spoken.append(delta)
    # abort after the 2nd token arrives
    def should_abort():
        return aborted_after["n"] >= 2
    result = brain.stream_chat([{"role": "user", "content": "hi"}],
                               on_token=on_token, should_abort=should_abort)
    assert result.aborted is True
    assert len(spoken) == 2   # stopped early, didn't generate all five


def test_pipeline_barge_in_bumps_stream_epoch():
    from app.agent.pipeline import CommandPipeline
    from tests.fakes import FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech
    config = make_config()
    pipeline = CommandPipeline(config=config, agent=FakeAgent(config),
                               history=FakeHistory(), ui=FakePipelineUI(),
                               speech=FakeSpeech(), run_async=False)
    pipeline._stream_epoch = 5
    pipeline.abort_stream()
    assert pipeline._stream_epoch == 6


# ---- command mode stays non-streamed (SAFETY) --------------------------------

def test_command_mode_not_streamed(monkeypatch):
    """plan_llm (command mode) must use the non-streaming complete(), never
    complete_stream() — a partial JSON plan must never reach parsing."""
    agent = Agent(make_config(groq_api_key="gsk_TESTKEY12345678"), None, None)
    stream_called = {"n": 0}
    monkeypatch.setattr(agent.brain.groq, "complete_stream",
                        lambda *a, **k: stream_called.__setitem__("n", 1))

    from app.llm.intent_parser import ActionPlan
    monkeypatch.setattr(agent.brain, "complete",
                        lambda kind, messages, **k: LLMResult(
                            text='{"intent":"open_app","tool_name":"open_app",'
                                 '"arguments":{"app_name":"paint"}}', provider="groq"))
    plan = agent.plan_llm("open paint")
    assert stream_called["n"] == 0          # streaming NEVER used for commands
    assert plan.intent == "open_app"


def test_partial_command_json_never_executes_tool():
    """A half-streamed command JSON must not parse into an executable plan."""
    from app.llm.intent_parser import parse_action_plan
    partial = '{"intent": "run_terminal", "tool_name": "run_te'
    assert parse_action_plan(partial) is None   # unparseable -> no plan, no tool


# ---- telemetry ---------------------------------------------------------------

def test_tts_first_audio_measured_from_first_token(monkeypatch):
    brain, _ = brain_with_stream(monkeypatch, ["Hi. ", "There."])
    result = brain.stream_chat([{"role": "user", "content": "hi"}],
                               on_token=lambda d: None)
    assert result.first_token_ms > 0
    assert result.provider == "groq"


def test_stream_falls_back_to_local_on_cloud_failure(monkeypatch):
    config = make_config(groq_api_key="gsk_TESTKEY12345678")
    brain = BrainRouter(config, ollama_client=None)
    def failing_post(*a, **k):
        import requests
        raise requests.exceptions.ConnectionError()
    monkeypatch.setattr(providers_module.requests, "post", failing_post)
    brain.ollama.complete = lambda messages, **k: LLMResult(
        text="local reply here.", provider="ollama")
    spoken = []
    result = brain.stream_chat([{"role": "user", "content": "hi"}],
                               on_token=spoken.append)
    assert result.provider == "ollama" and result.failover
    assert spoken == ["local reply here."]   # emitted as one chunk
