"""Ollama root-cause fixes (spec sec 10a): request settings, think-stripping,
prompt token budget, model default and config migration."""

import json

import app.llm.ollama_client as ollama_module
from app.config import AppConfig
from app.llm.intent_parser import parse_action_plan, strip_thinking
from app.llm.ollama_client import OllamaClient
from app.llm.prompt_builder import build_intent_messages
from tests.fakes import make_config


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, content="{}"):
        self._content = content

    def json(self):
        return {"message": {"content": self._content},
                "eval_count": 12, "eval_duration": int(1e9)}

    def raise_for_status(self):
        pass


def capture_post(monkeypatch, content="{}"):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return FakeResponse(content)

    monkeypatch.setattr(ollama_module.requests, "post", fake_post)
    return captured


def test_ollama_payload_contains_required_settings(monkeypatch):
    config = make_config()
    captured = capture_post(monkeypatch)
    OllamaClient(config).chat([{"role": "user", "content": "open notepad"}])

    payload = captured["payload"]
    assert payload["think"] is False
    assert payload["keep_alive"] == "30m"
    assert payload["format"] == "json"
    assert payload["stream"] is False
    assert payload["options"]["num_predict"] == 220
    assert payload["options"]["temperature"] == 0.1
    assert payload["options"]["num_ctx"] == 2048
    assert payload["options"]["num_gpu"] == 0  # iGPU offload corrupts output
    # read timeout honours the 20s config default
    assert captured["timeout"][1] == 20


def test_ollama_num_gpu_auto_omits_option(monkeypatch):
    config = make_config(ollama_num_gpu=-1)
    captured = capture_post(monkeypatch)
    OllamaClient(config).chat([{"role": "user", "content": "hi"}])
    assert "num_gpu" not in captured["payload"]["options"]


def test_warmup_request_is_tiny_and_keeps_model_loaded(monkeypatch):
    config = make_config()
    captured = capture_post(monkeypatch, content="Hi!")
    client = OllamaClient(config)
    messages = build_intent_messages("hello", config, None)
    ms = client.warm_up(messages)
    assert ms is not None and client.warmed_up
    payload = captured["payload"]
    assert payload["options"]["num_predict"] == 1
    assert payload["keep_alive"] == "30m"
    assert payload["think"] is False
    # warm-up primes the prompt cache with the REAL system prompt
    assert payload["messages"][0]["content"] == messages[0]["content"]


def test_think_block_stripped_before_json_parse(monkeypatch):
    config = make_config(ollama_model="qwen3:4b")
    raw = '<think>hmm, the user wants paint</think>{"intent": "open_app", ' \
          '"arguments": {"app_name": "paint"}}'
    capture_post(monkeypatch, content=raw)
    content = OllamaClient(config).chat([{"role": "user", "content": "open paint"}])
    assert "<think>" not in content
    plan = parse_action_plan(content)
    assert plan is not None and plan.intent == "open_app"
    # the parser is defensive on its own too
    assert "<think>" not in strip_thinking(raw)


def test_latency_and_tokens_per_second_recorded(monkeypatch):
    config = make_config()
    capture_post(monkeypatch)
    client = OllamaClient(config)
    client.chat([{"role": "user", "content": "hi"}])
    assert client.last_latency_ms > 0
    assert client.average_latency_ms > 0
    assert client.last_tokens_per_s == 12.0  # 12 tokens / 1s


def test_system_prompt_under_token_budget():
    config = make_config()

    class NoMemory:
        data = {}
        def get(self, key, default=None): return default

    messages = build_intent_messages("open paint", config, NoMemory())
    system = messages[0]["content"]
    # ~4 chars per token: budget of 800 tokens => 3200 chars
    assert len(system) < 3200, f"system prompt too long: {len(system)} chars"


def test_default_model_and_timeout():
    config = AppConfig()
    assert config.ollama_model == "llama3.2:3b"
    assert config.ollama_timeout == 20
    assert config.ollama_keep_alive == "30m"


def test_config_migration_updates_old_defaults(tmp_path):
    old = {"ollama_model": "qwen3:4b", "ollama_timeout": 120,
           "silence_seconds": 1.6, "max_record_seconds": 30,
           "app_aliases": {"chrome": "chrome"}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    config = AppConfig.load(path)
    assert config.ollama_model == "llama3.2:3b"
    assert config.ollama_timeout == 20
    assert config.silence_seconds == 1.2
    assert config.max_record_seconds == 8
    assert config.app_aliases["chrome"] == "chrome"       # user value kept
    assert config.app_aliases["paint"] == "mspaint.exe"   # new alias merged


def test_config_migration_keeps_user_choices(tmp_path):
    old = {"ollama_model": "mistral:7b", "ollama_timeout": 45}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    config = AppConfig.load(path)
    assert config.ollama_model == "mistral:7b"
    assert config.ollama_timeout == 45
