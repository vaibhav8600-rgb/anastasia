"""Phase 11E: email / messaging / browser / general-app integrations.

Email/message SENDS are always preview + strong confirmation and never go out
without a clear recipient; payments and form-submits stay blocked; "Send" and
"Submit" are hardcoded destructive targets; UIA control is app-agnostic.
"""

import pytest

from app.agent import safety as safety_mod
from app.agent.confirmation_manager import requires_strong_approval
from app.agent.router import match_rule
from app.agent.safety import is_payment_action, set_target_resolver, validate_action
from app.control import ResolvedTarget, Scope
from app.llm.intent_parser import ActionPlan
from app.tools.email_tools import parse_recipients, recipient_status
from tests.fakes import make_config

CFG = make_config()


@pytest.fixture(autouse=True)
def _no_real_resolver():
    yield
    set_target_resolver(None)


def resolve_to(name, confidence=1.0, **kw):
    set_target_resolver(lambda plan, config: ResolvedTarget(
        name=name, control_type=kw.pop("control_type", "Button"),
        bbox=(0, 0, 10, 10), backend=kw.pop("backend", "uia"),
        confidence=confidence, **kw))


def plan_for(tool, **args):
    return ActionPlan(intent=tool, tool_name=tool, arguments=dict(args))


# ---- 11E.1 email --------------------------------------------------------------------

def test_email_send_always_requires_preview_and_confirmation():
    plan = plan_for("send_email", to="rahul@x.com", subject="Hi",
                    body="running late", risk_level="low")
    plan.requires_confirmation = False              # the model claims harmless
    result = validate_action(plan, CFG)
    assert result.allowed and result.requires_confirmation
    assert result.risk_level == "high"
    assert result.target["to"] == ["rahul@x.com"]   # preview carries recipient
    assert result.target["subject"] == "Hi"
    assert requires_strong_approval(plan, result)   # 11A: needs "Anna approve"


def test_email_missing_recipient_blocks_send():
    for to in ("", None, "just a name with no address"):
        plan = plan_for("send_email", to=to, body="hi")
        result = validate_action(plan, CFG)
        assert not result.allowed, to
        assert result.risk_level == "blocked"
        assert "recipient" in result.reason.lower()


def test_email_multiple_recipients_requires_explicit_confirmation():
    plan = plan_for("send_email", to="a@x.com, b@y.com, c@z.com", body="hi")
    result = validate_action(plan, CFG)
    assert result.allowed and result.requires_confirmation
    assert result.target["to"] == ["a@x.com", "b@y.com", "c@z.com"]
    assert result.target["recipient_status"] == "multiple"
    assert "multiple" in result.reason.lower()      # the card names them all


def test_recipient_parsing_and_status():
    assert parse_recipients("a@x.com, b@y.com") == ["a@x.com", "b@y.com"]
    assert parse_recipients("Rahul <rahul@x.com>") == ["rahul@x.com"]
    assert parse_recipients("just a name") == []
    assert recipient_status("")[1] == "missing"
    assert recipient_status("Bob")[1] == "ambiguous"       # name, no address
    assert recipient_status("a@x.com")[1] == "ok"
    assert recipient_status(["a@x.com", "b@y.com"])[1] == "multiple"


def test_compose_email_opens_a_draft_but_does_not_send(monkeypatch):
    import app.tools.email_tools as et
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))
    from app.tools import ToolContext
    ctx = ToolContext(config=make_config(email_provider="gmail"))
    result = et.compose_email({"to": "rahul@x.com", "subject": "Late",
                               "body": "on my way"}, ctx)
    assert result.success
    assert "mail.google.com" in opened["url"]
    assert "rahul%40x.com" in opened["url"] or "rahul@x.com" in opened["url"]
    assert result.data["preview"]["subject"] == "Late"
    # composing never sends; missing recipient can't even draft
    assert not et.compose_email({"subject": "x"}, ctx).success


def test_email_router_opens_draft():
    plan = match_rule("email rahul@x.com saying I'll be late", CFG)
    assert plan is not None and plan.tool_name == "compose_email"
    assert plan.arguments["to"] == "rahul@x.com"
    assert "late" in plan.arguments["body"].lower()


# ---- 11E.2 messaging ------------------------------------------------------------------

def test_messaging_send_on_destructive_target_list():
    # send_message is gated on its own...
    plan = plan_for("send_message", to="Rahul", body="hi")
    plan.requires_confirmation = False
    result = validate_action(plan, CFG)
    assert result.allowed and result.requires_confirmation
    assert result.risk_level == "high" and result.destructive_target
    # ...and clicking a messaging app's "Send" button is destructive too (11C)
    resolve_to("Send", backend="uia")
    click = validate_action(plan_for("click_control", hint="send"), CFG)
    assert click.requires_confirmation and click.destructive_target

    # a message with no recipient is refused
    assert not validate_action(plan_for("send_message", body="hi"), CFG).allowed


def test_messaging_uia_fallback_to_vision_confirmation_on_electron_gap():
    """Electron messaging apps expose a sparse UIA tree: the resolver falls to
    a vision guess, which is low-confidence and always confirmed with a crop."""
    guess = ResolvedTarget(name="Send", control_type="VisionGuess",
                           bbox=(0, 0, 10, 10), backend="vision", confidence=0.7)
    guess.crop_data_url = "data:image/jpeg;base64,AAAA"
    set_target_resolver(lambda plan, config: guess)
    result = validate_action(plan_for("click_control", hint="send"), CFG)
    assert result.requires_confirmation and result.confidence < 1.0
    assert result.target["backend"] == "vision"
    assert result.target["crop_data_url"].startswith("data:image")


# ---- 11E.3 browser: reading ok, paying blocked ----------------------------------------

def test_banking_payment_action_blocked_not_just_confirmed():
    assert is_payment_action("Pay $500 now")
    assert is_payment_action("Confirm payment")
    assert is_payment_action("Transfer funds")
    assert is_payment_action("Place order")
    assert not is_payment_action("Pay attention")     # not a money action
    assert not is_payment_action("Send")

    # Clicking an actual payment control is BLOCKED, not merely confirmed.
    resolve_to("Pay $500 now", backend="uia", confidence=1.0)
    result = validate_action(plan_for("click_control", hint="pay button"), CFG)
    assert not result.allowed and result.risk_level == "blocked"
    assert "payment" in result.reason.lower() or "money" in result.reason.lower()

    # A blocked payment tool stays blocked outright.
    assert not validate_action(plan_for("make_payment", amount="500"), CFG).allowed

    # But READING a bank page is fine.
    reading = validate_action(plan_for("browser_read_page_text"), CFG)
    assert reading.allowed


def test_form_submit_still_hardcoded_high_risk():
    resolve_to("Submit", backend="playwright", confidence=1.0)
    result = validate_action(plan_for("browser_find_and_click", hint="submit"), CFG)
    assert result.allowed and result.requires_confirmation
    assert result.risk_level == "high" and result.destructive_target


# ---- 11E.4 general app control is app-agnostic ----------------------------------------

def test_general_app_uia_control_works_app_agnostic():
    """The 11C UIA path isn't email-specific: an ordinary control in ANY app
    resolves and runs with no extra friction; a destructive one still gates."""
    for app_name, control in (("Calculator", "Equals"), ("Notepad", "Bold"),
                              ("Spotify", "Play")):
        resolve_to(control, backend="uia", confidence=1.0)
        result = validate_action(plan_for("click_control", hint=control,
                                          app=app_name), CFG)
        assert result.allowed and not result.requires_confirmation, control
        assert result.target["name"] == control

    # typing into an ordinary field is frictionless; a password field gates
    resolve_to("Search", control_type="Edit", backend="uia", confidence=1.0)
    typed = validate_action(plan_for("type_into_control", hint="search",
                                     text="hello"), CFG)
    assert typed.allowed and not typed.requires_confirmation
