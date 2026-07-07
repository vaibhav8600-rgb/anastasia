"""Phase 8A: hybrid brain — Groq provider, failover chain, circuit breaker,
key hygiene. All network calls mocked; no real API usage."""

import json
import time

import pytest

import app.llm.providers as providers_module
from app.agent.pipeline import CommandPipeline
from app.agent.router import Agent
from app.llm.providers import (BrainRouter, BrainUnavailable, GroqProvider,
                               LLMResult, mask_key)
from tests.fakes import (ExplodingLLM, FakeAgent, FakeHistory, FakePipelineUI,
                         FakeSpeech, make_config)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


class FakeResponse:
    def __init__(self, status=200, content="hello from groq",
                 headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body if body is not None else {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12},
        }
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def make_brain(monkeypatch, groq_response=None, groq_exc=None,
               ollama_text="local answer", ollama_exc=None, **config_overrides):
    config_overrides.setdefault("groq_api_key", "gsk_TESTKEY12345678")
    config = make_config(**config_overrides)
    brain = BrainRouter(config, ollama_client=None)
    captured = {"groq_calls": 0}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["groq_calls"] += 1
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        if groq_exc:
            raise groq_exc
        return groq_response or FakeResponse()

    monkeypatch.setattr(providers_module.requests, "post", fake_post)

    def fake_ollama(messages, **kwargs):
        captured["ollama_kwargs"] = kwargs
        if ollama_exc:
            raise ollama_exc
        return LLMResult(text=ollama_text, provider="ollama", latency_ms=50)

    brain.ollama.complete = fake_ollama
    return brain, captured, config


# ---- payload / auth ---------------------------------------------------------

def test_groq_payload_shape_and_auth_header(monkeypatch):
    brain, cap, config = make_brain(monkeypatch)
    result = brain.complete("command", [{"role": "user", "content": "open x"}])
    assert result.ok and result.provider == "groq"
    assert cap["url"].startswith("https://api.groq.com/openai/v1/chat")
    assert cap["headers"]["Authorization"] == "Bearer gsk_TESTKEY12345678"
    p = cap["payload"]
    assert p["model"] == "llama-3.3-70b-versatile"
    assert p["max_tokens"] == 300 and p["temperature"] == 0.1
    assert p["messages"][0]["content"] == "open x"
    assert cap["timeout"][1] == 8.0
    assert result.completion_tokens == 12


def test_json_mode_uses_response_format(monkeypatch):
    brain, cap, _ = make_brain(monkeypatch)
    brain.complete("command", [{"role": "user", "content": "x"}])
    assert cap["payload"]["response_format"] == {"type": "json_object"}
    brain.complete("chat", [{"role": "user", "content": "x"}])
    assert "response_format" not in cap["payload"]
    assert cap["payload"]["max_tokens"] == 150
    assert cap["payload"]["temperature"] == 0.7


# ---- routing ---------------------------------------------------------------

def test_rule_commands_never_touch_any_provider(monkeypatch):
    agent = Agent(make_config(groq_api_key="gsk_TESTKEY12345678"),
                  memory=None, history=None)
    agent.llm = ExplodingLLM()

    def explode(*a, **k):
        raise AssertionError("provider called for a rule command!")
    monkeypatch.setattr(agent.brain, "complete", explode)

    plan = agent.plan("open paint")
    assert plan.intent == "open_app" and plan.arguments["app_name"] == "paint"


def test_groq_timeout_fails_over_to_ollama(monkeypatch):
    import requests as real_requests
    brain, cap, _ = make_brain(monkeypatch,
                               groq_exc=real_requests.exceptions.ConnectTimeout())
    result = brain.complete("chat", [{"role": "user", "content": "hi"}])
    assert result.provider == "ollama" and result.text == "local answer"
    assert result.failover is True
    assert cap["ollama_kwargs"]["timeout_s"] == 15.0   # capped as fallback
    assert brain.history[-2]["provider"] == "groq"
    assert brain.history[-2]["error"] == "timeout"


def test_groq_429_fails_over_without_crash(monkeypatch):
    brain, cap, _ = make_brain(
        monkeypatch, groq_response=FakeResponse(status=429,
                                                headers={"retry-after": "7"},
                                                body={}))
    result = brain.complete("chat", [{"role": "user", "content": "hi"}])
    assert result.provider == "ollama" and result.failover
    groq_attempt = brain.history[-2]
    assert groq_attempt["error"] == "rate_limit"


def test_circuit_opens_after_3_failures_and_probes_after_cooldown(monkeypatch):
    import requests as real_requests
    brain, cap, _ = make_brain(monkeypatch,
                               groq_exc=real_requests.exceptions.ConnectionError())
    for _ in range(3):
        brain.complete("chat", [{"role": "user", "content": "hi"}])
    assert brain.circuit_open()
    calls_before = cap["groq_calls"]
    result = brain.complete("chat", [{"role": "user", "content": "hi"}])
    assert cap["groq_calls"] == calls_before     # no cloud attempt while open
    assert result.provider == "ollama" and not result.failover

    # cooldown elapses -> next request is the probe; make Groq healthy again
    brain._open_until = time.monotonic() - 1
    def healthy_post(url, json=None, timeout=None, headers=None):
        cap["groq_calls"] += 1
        return FakeResponse(content="probe ok")
    monkeypatch.setattr(providers_module.requests, "post", healthy_post)
    result = brain.complete("chat", [{"role": "user", "content": "hi"}])
    assert result.provider == "groq" and result.text == "probe ok"
    assert not brain.circuit_open() and brain._failures == 0


def test_local_only_mode_never_calls_groq(monkeypatch):
    brain, cap, _ = make_brain(monkeypatch, brain_mode="local_only")
    result = brain.complete("command", [{"role": "user", "content": "x"}])
    assert cap["groq_calls"] == 0
    assert result.provider == "ollama" and not result.failover


def test_no_key_behaves_as_pure_local(monkeypatch):
    brain, cap, _ = make_brain(monkeypatch, groq_api_key="")
    assert brain.mode() == "local_only"
    result = brain.complete("chat", [{"role": "user", "content": "x"}])
    assert cap["groq_calls"] == 0 and result.provider == "ollama"


# ---- key hygiene ------------------------------------------------------------

def test_api_key_never_in_logs_or_frontend_events(monkeypatch):
    from app.agent.devlog import devlog
    from app.web.bridge import JsApi, UIBridge
    from app.main import Controller
    from tests.test_bridge import FakeMemory, FakeWindow

    secret = "gsk_SUPERSECRETVALUE999"
    devlog.clear()
    devlog.echo_to_stdout = False
    try:
        bridge = UIBridge()
        window = FakeWindow()
        bridge.window = window
        controller = Controller(ui=bridge, autostart=False,
                                config=make_config(groq_api_key=secret),
                                memory=FakeMemory(), history=FakeHistory())
        controller.speech.shutdown()
        bridge.controller = controller
        JsApi(bridge).ready()

        controller.open_settings()
        controller.save_settings({"groq_api_key": "gsk_ANOTHERSECRET111",
                                  "cloud_timeout_s": 9.0})
        controller._on_brain_state()

        everything = json.dumps(window.events) + json.dumps(devlog.entries(500))
        assert secret not in everything
        assert "gsk_ANOTHERSECRET111" not in everything
        payload = window.of_type("settings")[-1]["payload"]
        assert payload["groq_key_masked"] == "gsk_...E999"  # masked display only
        assert "groq_api_key" not in payload
        # the masked placeholder is never stored as the key
        controller.save_settings({"groq_api_key": "gsk_...Y999"})
        assert controller.config.groq_api_key == "gsk_ANOTHERSECRET111"
    finally:
        devlog.echo_to_stdout = True


def test_mask_key():
    assert mask_key("gsk_ABCDEFGHIJKL1234") == "gsk_...1234"
    assert mask_key("") == ""
    assert mask_key("short") == "•••"


# ---- honest total failure ----------------------------------------------------

def test_both_providers_fail_shows_honest_message_and_resets_state(monkeypatch):
    import requests as real_requests
    from app.llm.ollama_client import OllamaError
    brain, cap, _ = make_brain(monkeypatch,
                               groq_exc=real_requests.exceptions.ConnectTimeout(),
                               ollama_exc=OllamaError("local timed out"))
    with pytest.raises(BrainUnavailable):
        brain.complete("chat", [{"role": "user", "content": "hi"}])

    # and through the pipeline: honest message + clean state reset
    config = make_config()
    agent = FakeAgent(config, rule=lambda t: None,
                      llm_exc=BrainUnavailable(BrainRouter.HONEST_FAILURE))
    ui = FakePipelineUI()
    pipeline = CommandPipeline(config=config, agent=agent, history=FakeHistory(),
                               ui=ui, speech=FakeSpeech(), run_async=False)
    pipeline.submit("please summarize the news for me", source="typed")
    assert any("cloud brain is unreachable" in e for e in ui.errors)
    assert not pipeline.is_processing_command
    assert ui.states[-1] == "ready"


def test_brain_chip_states(monkeypatch):
    from app.main import Controller
    from app.web.bridge import JsApi, UIBridge
    from tests.test_bridge import FakeMemory, FakeWindow

    bridge = UIBridge(); window = FakeWindow(); bridge.window = window
    controller = Controller(ui=bridge, autostart=False,
                            config=make_config(groq_api_key="gsk_TESTKEY12345678"),
                            memory=FakeMemory(), history=FakeHistory())
    controller.speech.shutdown()
    bridge.controller = controller
    JsApi(bridge).ready()

    controller._push_chips("connected")
    chips = window.of_type("status")[-1]["payload"]["chips"]
    assert chips["brain"]["label"].startswith("Brain: Groq")
    assert chips["brain"]["state"] == "ok"

    controller.agent.brain._open_until = time.monotonic() + 60   # circuit open
    controller._push_chips("connected")
    chips = window.of_type("status")[-1]["payload"]["chips"]
    assert chips["brain"] == {"label": "Brain: Local (cloud offline)",
                              "state": "warn"}

    controller.config.brain_mode = "local_only"
    controller._push_chips("connected")
    chips = window.of_type("status")[-1]["payload"]["chips"]
    assert chips["brain"]["state"] == "local"
