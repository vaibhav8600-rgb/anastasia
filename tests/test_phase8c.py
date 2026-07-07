"""Phase 8C: privacy tiers — hard cloud gate in the provider, clipboard
opt-in routing, private_ memory keys never in prompts."""

import pytest

import app.llm.providers as providers_module
from app.llm.providers import (BrainRouter, DataClass, GroqProvider,
                               LLMResult, PrivacyViolation, cloud_allowed)
from tests.fakes import make_config


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    # any real HTTP attempt in this file is a test failure
    def no_network(*a, **k):
        raise AssertionError("real network call attempted!")
    monkeypatch.setattr(providers_module.requests, "post", no_network)


def make_brain(**overrides):
    overrides.setdefault("groq_api_key", "gsk_TESTKEY12345678")
    config = make_config(**overrides)
    brain = BrainRouter(config, ollama_client=None)
    calls = {"ollama": 0}

    def fake_ollama(messages, **kwargs):
        calls["ollama"] += 1
        return LLMResult(text="local summary", provider="ollama")
    brain.ollama.complete = fake_ollama
    return brain, calls, config


# ---- hard gate in the provider ------------------------------------------------

def test_file_content_to_groq_raises_privacy_violation():
    provider = GroqProvider(make_config(groq_api_key="gsk_TESTKEY12345678"))
    with pytest.raises(PrivacyViolation):
        provider.complete([{"role": "user", "content": "x"}],
                          json_mode=False, max_tokens=10, temperature=0.1,
                          timeout_s=5,
                          payload_classes={DataClass.TRANSCRIPT,
                                           DataClass.FILE_CONTENT})


def test_screenshot_to_groq_raises_privacy_violation():
    provider = GroqProvider(make_config(groq_api_key="gsk_TESTKEY12345678"))
    for never in (DataClass.SCREENSHOT, DataClass.AUDIO):
        with pytest.raises(PrivacyViolation):
            provider.complete([{"role": "user", "content": "x"}],
                              json_mode=False, max_tokens=10, temperature=0.1,
                              timeout_s=5, payload_classes={never})


def test_clipboard_without_optin_raises_in_provider():
    provider = GroqProvider(make_config(groq_api_key="gsk_TESTKEY12345678",
                                        allow_clipboard_to_cloud=False))
    with pytest.raises(PrivacyViolation):
        provider.complete([{"role": "user", "content": "x"}],
                          json_mode=False, max_tokens=10, temperature=0.1,
                          timeout_s=5, payload_classes={DataClass.CLIPBOARD})


# ---- router-level privacy routing ---------------------------------------------

def test_clipboard_default_routes_to_ollama_in_hybrid():
    brain, calls, _ = make_brain(allow_clipboard_to_cloud=False)
    assert brain.mode() == "hybrid"
    result = brain.complete("chat", [{"role": "user", "content": "clip"}],
                            payload_classes={DataClass.CLIPBOARD})
    assert result.provider == "ollama" and calls["ollama"] == 1
    assert brain.history[-1]["data_classes"] == ["clipboard"]


def test_clipboard_optin_allows_groq(monkeypatch):
    brain, calls, _ = make_brain(allow_clipboard_to_cloud=True)
    seen = {}

    def fake_groq(messages, **kwargs):
        seen["classes"] = kwargs.get("payload_classes")
        return LLMResult(text="cloud summary", provider="groq", latency_ms=200)
    monkeypatch.setattr(brain.groq, "complete", fake_groq)

    result = brain.complete("chat", [{"role": "user", "content": "clip"}],
                            payload_classes={DataClass.CLIPBOARD})
    assert result.provider == "groq" and calls["ollama"] == 0
    assert DataClass.CLIPBOARD in seen["classes"]


def test_never_cloud_routes_local_even_in_hybrid():
    brain, calls, _ = make_brain(allow_clipboard_to_cloud=True)
    result = brain.complete("chat", [{"role": "user", "content": "x"}],
                            payload_classes={DataClass.FILE_CONTENT})
    assert result.provider == "ollama"       # router never even tries Groq


def test_summarize_clipboard_uses_brain_with_clipboard_class(monkeypatch):
    from app.tools import ToolContext
    from app.tools.clipboard_tools import summarize_clipboard

    class FakeClip:
        @staticmethod
        def paste():
            return "some copied article text"
    monkeypatch.setattr("app.tools.clipboard_tools._clip", lambda: FakeClip)

    brain, calls, _ = make_brain(allow_clipboard_to_cloud=False)
    ctx = ToolContext(config=brain.config, memory=None, llm=None, brain=brain)
    result = summarize_clipboard({}, ctx)
    assert result.success and result.message == "local summary"
    assert calls["ollama"] == 1              # stayed local by default


# ---- private memory keys -------------------------------------------------------

def test_private_memory_keys_never_in_prompts(monkeypatch):
    import app.llm.prompt_builder as pb

    class LeakyMemory:
        data = {"user_name": "Vaibhav", "private_diary": "TOP SECRET",
                "private_password_hint": "hunter2"}

        def get(self, key, default=None):
            return self.data.get(key, default)

    # even if someone whitelists a private_ key, the guard must drop it
    monkeypatch.setattr(pb, "_SAFE_MEMORY_KEYS",
                        list(getattr(pb, "_SAFE_MEMORY_KEYS", []))
                        + ["private_diary", "private_password_hint"])
    messages = pb.build_chat_messages("hi", make_config(), LeakyMemory())
    blob = " ".join(m["content"] for m in messages)
    assert "TOP SECRET" not in blob
    assert "hunter2" not in blob
    assert "Vaibhav" in blob                  # safe keys still flow


def test_cloud_allowed_helper():
    config = make_config(allow_clipboard_to_cloud=False)
    ok, _ = cloud_allowed({DataClass.TRANSCRIPT, DataClass.CHAT_CONTEXT}, config)
    assert ok
    ok, reason = cloud_allowed({DataClass.CLIPBOARD}, config)
    assert not ok and "clipboard" in reason
    ok, reason = cloud_allowed({DataClass.SCREENSHOT}, config)
    assert not ok and "never leaves" in reason
