"""Tests for JSON extraction/validation and rule-based intent matching."""

from app.agent.router import clean_command, match_rule
from app.config import AppConfig
from app.llm.intent_parser import extract_json, parse_action_plan

CFG = AppConfig(safe_folders=["C:/Users/Test/Downloads", "C:/Users/Test/Projects"])


# ---- JSON parsing -------------------------------------------------------

def test_parse_plain_json():
    plan = parse_action_plan(
        '{"assistant_message": "Opening Chrome.", "intent": "open_app",'
        ' "tool_name": "open_app", "arguments": {"app_name": "chrome"},'
        ' "risk_level": "low", "requires_confirmation": false,'
        ' "confirmation_message": ""}')
    assert plan is not None
    assert plan.intent == "open_app"
    assert plan.arguments["app_name"] == "chrome"
    assert not plan.requires_confirmation


def test_parse_json_in_code_fence():
    text = 'Sure!\n```json\n{"intent": "take_screenshot", "arguments": {}}\n```\nDone.'
    plan = parse_action_plan(text)
    assert plan is not None and plan.intent == "take_screenshot"
    assert plan.tool_name == "take_screenshot"  # defaults to intent


def test_parse_json_with_thinking_block():
    text = '<think>The user wants chrome...</think>{"intent": "open_app", "arguments": {"app_name": "chrome"}}'
    plan = parse_action_plan(text)
    assert plan is not None and plan.intent == "open_app"


def test_parse_json_with_surrounding_prose():
    text = 'Here you go: {"intent": "no_action", "assistant_message": "Hi!"} hope that helps'
    plan = parse_action_plan(text)
    assert plan is not None and plan.assistant_message == "Hi!"


def test_garbage_returns_none():
    assert parse_action_plan("I would love to open chrome for you!") is None
    assert parse_action_plan("") is None
    assert extract_json("{broken json") is None


def test_risk_level_normalized():
    plan = parse_action_plan('{"intent": "open_app", "risk_level": "EXTREME"}')
    assert plan.risk_level == "low"  # unknown values fall back safely


# ---- wake-name stripping --------------------------------------------------

def test_clean_command_strips_names():
    assert clean_command("Anna, open chrome", CFG) == "open chrome"
    assert clean_command("hey anastasia open chrome", CFG) == "open chrome"
    assert clean_command("open chrome", CFG) == "open chrome"


# ---- rule-based matching ---------------------------------------------------

def test_rule_open_chrome():
    plan = match_rule("Anna, open Chrome", CFG)
    assert plan is not None and plan.intent == "open_app"
    assert plan.arguments["app_name"] == "chrome"


def test_rule_open_vs_code_variants():
    for phrase in ("open vs code", "open vscode", "launch visual studio code"):
        plan = match_rule(phrase, CFG)
        assert plan is not None and plan.intent == "open_app", phrase
        assert plan.arguments["app_name"] == "vscode", phrase


def test_rule_take_screenshot():
    plan = match_rule("take a screenshot", CFG)
    assert plan is not None and plan.intent == "take_screenshot"


def test_rule_open_downloads_folder():
    plan = match_rule("open downloads", CFG)
    assert plan is not None and plan.intent == "open_folder"
    assert plan.arguments["folder"] == "downloads"


def test_rule_type_text_preserves_case():
    plan = match_rule("type Hello World", CFG)
    assert plan is not None and plan.intent == "type_text"
    assert plan.arguments["text"] == "Hello World"


def test_rule_search_files():
    plan = match_rule("search my downloads for invoice", CFG)
    assert plan is not None and plan.intent == "search_files"
    assert plan.arguments == {"folder": "downloads", "query": "invoice"}


def test_rule_summarize_clipboard():
    plan = match_rule("summarize my clipboard", CFG)
    assert plan is not None and plan.intent == "summarize_clipboard"


def test_rule_copy_paste():
    assert match_rule("copy this", CFG).arguments["keys"] == "ctrl+c"
    assert match_rule("paste", CFG).arguments["keys"] == "ctrl+v"


def test_rule_open_youtube():
    plan = match_rule("open youtube", CFG)
    assert plan is not None and plan.intent == "browser_open"
    assert "youtube" in plan.arguments["url"]


def test_rule_google_search():
    plan = match_rule("search google for python virtual environment", CFG)
    assert plan is not None and plan.intent == "browser_open"
    assert plan.arguments["query"] == "python virtual environment"


def test_run_npm_falls_through_to_llm():
    # "run npm dev" is not a rule — must go to the LLM (-> run_terminal + confirm)
    assert match_rule("run npm dev", CFG) is None


def test_chitchat_falls_through_to_llm():
    assert match_rule("how are you feeling today", CFG) is None
