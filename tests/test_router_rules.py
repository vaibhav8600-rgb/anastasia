"""Fast-router guarantees: simple commands NEVER call the LLM (spec sec 6/24),
paint maps to mspaint, and normalization handles STT quirks."""

import pytest

from app.agent.normalizer import looks_garbled, normalize_command
from app.agent.router import Agent, match_rule
from app.tools.open_app import resolve_app
from tests.fakes import make_config

CFG = make_config()


def agent_with_exploding_llm() -> Agent:
    agent = Agent(CFG, memory=None, history=None)
    from tests.fakes import ExplodingLLM
    agent.llm = ExplodingLLM()
    return agent


# ---- rule routes must never reach Ollama ---------------------------------

@pytest.mark.parametrize("command,intent,args", [
    ("open paint", "open_app", {"app_name": "paint"}),
    ("Open Paint.", "open_app", {"app_name": "paint"}),
    ("open ms paint", "open_app", {"app_name": "mspaint"}),
    ("open notepad", "open_app", {"app_name": "notepad"}),
    ("take screenshot", "take_screenshot", {}),
    ("capture screenshot", "take_screenshot", {}),
    ("copy", "press_hotkey", {"keys": "ctrl+c"}),
    ("paste", "press_hotkey", {"keys": "ctrl+v"}),
    ("open downloads", "open_folder", {"folder": "downloads"}),
    ("open my downloads", "open_folder", {"folder": "downloads"}),
])
def test_rule_routes_never_call_llm(command, intent, args):
    agent = agent_with_exploding_llm()
    plan = agent.plan(command)  # ExplodingLLM fails the test if consulted
    assert plan.intent == intent
    if args:
        for key, value in args.items():
            assert plan.arguments[key] == value


def test_open_paint_rule_route_never_calls_llm():
    plan = agent_with_exploding_llm().plan("Anna, open Paint.")
    assert plan.intent == "open_app" and plan.arguments["app_name"] == "paint"


def test_open_notepad_rule_route_never_calls_llm():
    plan = agent_with_exploding_llm().plan("open notepad")
    assert plan.intent == "open_app" and plan.arguments["app_name"] == "notepad"


def test_take_screenshot_rule_route_never_calls_llm():
    plan = agent_with_exploding_llm().plan("take a screenshot")
    assert plan.intent == "take_screenshot"


def test_copy_paste_rule_route_never_calls_llm():
    agent = agent_with_exploding_llm()
    assert agent.plan("copy").arguments["keys"] == "ctrl+c"
    assert agent.plan("paste").arguments["keys"] == "ctrl+v"


def test_open_downloads_rule_route_never_calls_llm():
    plan = agent_with_exploding_llm().plan("open downloads")
    assert plan.intent == "open_folder" and plan.arguments["folder"] == "downloads"


def test_project_folder_synonym():
    plan = match_rule("open my project folder", CFG)
    assert plan is not None and plan.intent == "open_folder"
    assert plan.arguments["folder"] == "projects"


def test_search_youtube_rule():
    plan = match_rule("search youtube for lofi beats", CFG)
    assert plan is not None and plan.intent == "browser_open"
    assert "youtube.com/results" in plan.arguments["url"]


def test_open_website_rule():
    plan = match_rule("open website example.com", CFG)
    assert plan is not None and plan.intent == "browser_open"
    assert plan.arguments["url"] == "example.com"


# ---- aliases --------------------------------------------------------------

def test_paint_alias_maps_to_mspaint():
    assert "mspaint" in resolve_app("paint", CFG).lower()
    assert "mspaint" in resolve_app("ms paint", CFG).lower()
    assert "mspaint" in resolve_app("Paint", CFG).lower()


# ---- normalization --------------------------------------------------------

def test_normalize_strips_wake_words():
    assert normalize_command("Anna, open Paint.", CFG).cleaned == "open Paint"
    assert normalize_command("Hey Anna open MS Paint", CFG).cleaned == "open MS Paint"


def test_normalize_strips_trailing_filler():
    assert normalize_command("open downloads please", CFG).cleaned == "open downloads"
    assert normalize_command("open notepad for me, thanks", CFG).cleaned == "open notepad"


def test_normalize_keeps_search_for_intact():
    n = normalize_command("search downloads for invoice", CFG)
    assert n.cleaned == "search downloads for invoice"


def test_normalize_splits_multi_sentence_stt():
    n = normalize_command("Open Paint. Open no pass for you.", CFG)
    assert n.sentences[0] == "Open Paint"
    assert match_rule(n.sentences[0], CFG).arguments["app_name"] == "paint"


def test_whisper_artifacts_preserved_until_audio_confidence_gate():
    for phrase in ("Thank you.", "thanks for watching", "you"):
        assert not normalize_command(phrase, CFG).empty, phrase
    assert normalize_command("", CFG).empty


def test_garble_detection():
    assert looks_garbled("x")
    assert not looks_garbled("open no pass for you")
    assert not looks_garbled("what's the weather like")


def test_type_text_preserves_case_after_normalize():
    n = normalize_command("Anna, type Hello World", CFG)
    plan = match_rule(n.cleaned, CFG)
    assert plan.intent == "type_text" and plan.arguments["text"] == "Hello World"
