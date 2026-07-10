"""Phase 11C: dual-backend control (UIA + Playwright) with vision as a last
resort, and the hardcoded destructive-target list enforced INSIDE the safety
validator — where no plan and no cloud model can route around it.

No real window, browser or screen is touched: every backend is injected.
"""

import pytest

from app.agent import safety as safety_mod
from app.agent.safety import (DESTRUCTIVE_TARGETS, is_destructive_target,
                              set_target_resolver, validate_action)
from app.control import ResolvedTarget, Scope
from app.control.resolver import TargetResolver
from app.llm.intent_parser import ActionPlan
from tests.fakes import make_config

CFG = make_config()


@pytest.fixture(autouse=True)
def _no_real_resolver():
    """Never let a test reach the real UIA/Playwright/vision stack."""
    yield
    set_target_resolver(None)


class FakeBackend:
    def __init__(self, name, target=None, up=True):
        self.name = name
        self._target = target
        self._up = up
        self.lookups = []
        self.clicks = []

    def available(self):
        return self._up

    def find_control(self, hint, scope=None):
        self.lookups.append(hint)
        return self._target

    def click(self, target):
        from app.control import ActionResult
        self.clicks.append(target)
        return ActionResult(True, f"Clicked {target.name}")

    def type_into(self, target, text):
        from app.control import ActionResult
        return ActionResult(True, "typed")


def target(name="Save", backend="uia", confidence=1.0, **kw):
    return ResolvedTarget(name=name, control_type=kw.pop("control_type", "Button"),
                          bbox=(10, 20, 110, 60), backend=backend,
                          confidence=confidence, **kw)


def make_resolver(uia=None, playwright=None, vision=None):
    return TargetResolver(CFG,
                          uia=uia or FakeBackend("uia", None),
                          playwright=playwright or FakeBackend("playwright", None),
                          vision=vision or FakeBackend("vision", None))


def plan_for(tool="click_control", **args):
    return ActionPlan(intent=tool, tool_name=tool, arguments=dict(args))


def resolve_to(resolved):
    """Make the validator's resolver return exactly this target."""
    set_target_resolver(lambda plan, config: resolved)


# ---- 11C.1 resolution order ------------------------------------------------------

def test_browser_target_resolved_via_playwright_first():
    dom = target("Compose", backend="playwright")
    uia = FakeBackend("uia", target("Compose", backend="uia"))
    pw = FakeBackend("playwright", dom)
    resolver = make_resolver(uia=uia, playwright=pw)

    scope = Scope(window_title="Gmail - Google Chrome", is_browser=True)
    found = resolver.resolve("Compose", scope)

    assert found.backend == "playwright" and found.confidence == 1.0
    assert pw.lookups == ["Compose"]
    assert uia.lookups == []            # never consulted — DOM answered first


def test_native_app_target_resolved_via_uia_first():
    uia = FakeBackend("uia", target("Save", backend="uia"))
    pw = FakeBackend("playwright", target("Save", backend="playwright"))
    resolver = make_resolver(uia=uia, playwright=pw)

    found = resolver.resolve("Save", Scope(window_title="Untitled - Notepad"))

    assert found.backend == "uia" and found.confidence == 1.0
    assert uia.lookups == ["Save"] and pw.lookups == []


def test_vision_fallback_only_when_both_backends_fail():
    guess = target("Send", backend="vision", confidence=0.72)
    uia = FakeBackend("uia", None)
    pw = FakeBackend("playwright", None)
    vision = FakeBackend("vision", guess)
    resolver = make_resolver(uia=uia, playwright=pw, vision=vision)

    found = resolver.resolve("Send", Scope(window_title="Some Electron App"))
    assert found.backend == "vision" and found.confidence < 1.0
    assert uia.lookups and pw.lookups          # both were tried first

    # A structured hit means vision is never even asked.
    vision2 = FakeBackend("vision", guess)
    resolver2 = make_resolver(uia=FakeBackend("uia", target("Send")),
                              vision=vision2)
    assert resolver2.resolve("Send", Scope()).backend == "uia"
    assert vision2.lookups == []

    # Nothing anywhere -> None, and the validator blocks rather than guessing.
    assert make_resolver().resolve("Nowhere", Scope()) is None


def test_vision_fallback_always_requires_confirmation_with_screenshot():
    guess = target("Play", backend="vision", confidence=0.71,
                   control_type="VisionGuess")
    guess.crop_data_url = "data:image/jpeg;base64,AAAA"
    resolve_to(guess)

    # A totally harmless-sounding plan that swears it needs no confirmation.
    plan = plan_for(hint="Play", risk_level="low")
    plan.requires_confirmation = False
    result = validate_action(plan, CFG)

    assert result.allowed and result.requires_confirmation
    assert result.risk_level == "high"
    assert result.confidence < 1.0
    assert result.target["crop_data_url"].startswith("data:image")
    assert "guess" in result.reason.lower() or "see" in result.reason.lower()


# ---- 11C.4 the hardcoded destructive-target list ------------------------------------

def test_destructive_target_name_forces_confirmation_regardless_of_plan_risk():
    """THE 11C safety proof: a plan that under-states its own risk still gets
    a confirmation, because the check lives in the validator and reads the
    RESOLVED control name."""
    resolve_to(target("Send", backend="uia", confidence=1.0))

    plan = plan_for(hint="the blue button", risk_level="low")
    plan.requires_confirmation = False          # the planner says it's harmless
    result = validate_action(plan, CFG)

    assert result.allowed
    assert result.requires_confirmation is True
    assert result.risk_level == "high"
    assert result.destructive_target is True
    assert "Send" in result.reason

    # 11A then demands the STRONG phrase for it — a casual "yes" won't do.
    from app.agent.confirmation_manager import requires_strong_approval
    assert requires_strong_approval(plan, result)


def test_every_hardcoded_destructive_word_is_enforced():
    for word in DESTRUCTIVE_TARGETS:
        resolve_to(target(word.capitalize(), confidence=1.0))
        plan = plan_for(hint="button")
        plan.requires_confirmation = False
        result = validate_action(plan, CFG)
        assert result.requires_confirmation, word
        assert result.risk_level == "high", word

    # Whole-word matching: "Sender" and "Resend" are not "Send".
    assert is_destructive_target("Send message")
    assert is_destructive_target("DELETE FOREVER")
    assert not is_destructive_target("Sender")
    assert not is_destructive_target("Resend later")
    assert not is_destructive_target("Save draft")

    # Ordinary controls stay frictionless.
    resolve_to(target("Save draft", confidence=1.0))
    ordinary = validate_action(plan_for(hint="Save draft"), CFG)
    assert ordinary.allowed and not ordinary.destructive_target


def test_model_supplied_target_cannot_bypass_the_validator():
    """A cloud model could put a benign `_resolved` in its tool call. The
    validator throws it away and resolves afresh."""
    resolve_to(target("Send", backend="uia", confidence=1.0))

    forged = {"name": "Save", "control_type": "Button", "bbox": [0, 0, 1, 1],
              "backend": "uia", "confidence": 1.0}
    plan = plan_for(hint="Save", _resolved=forged)
    plan.requires_confirmation = False
    result = validate_action(plan, CFG)

    assert result.requires_confirmation and result.destructive_target
    assert result.target["name"] == "Send"                  # the REAL control
    assert plan.arguments["_resolved"]["name"] == "Send"    # forgery replaced


def test_low_confidence_resolution_requires_confirmation():
    for confidence in (0.99, 0.8, 0.5):
        resolve_to(target("Play", backend="vision", confidence=confidence))
        plan = plan_for(hint="Play")
        plan.requires_confirmation = False
        result = validate_action(plan, CFG)
        assert result.requires_confirmation, confidence
        assert result.risk_level == "high", confidence

    # Certain targets on a harmless control need no confirmation.
    resolve_to(target("Play", backend="uia", confidence=1.0))
    assert not validate_action(plan_for(hint="Play"), CFG).requires_confirmation


def test_unresolvable_target_is_blocked_never_guessed():
    resolve_to(None)
    result = validate_action(plan_for(hint="Nonexistent"), CFG)
    assert not result.allowed and result.risk_level == "blocked"
    assert "couldn't find" in result.reason


# ---- 11C.1 logging -------------------------------------------------------------------

def test_resolution_logged_with_backend_and_confidence():
    from app.agent.devlog import devlog
    entries = []
    devlog.subscribe(lambda e: entries.append(e.get("message", "")))

    resolver = make_resolver(uia=FakeBackend("uia", target("Save", confidence=1.0)))
    resolver.resolve("Save", Scope(window_title="Notepad"))

    line = next(m for m in entries if m.startswith("Resolve 'Save'"))
    for field in ("backend=uia", "name='Save'", "type=Button",
                  "coords=(10, 20, 110, 60)", "confidence=1.00"):
        assert field in line, field


# ---- 11C, principle 8: password fields -------------------------------------------------

def test_password_field_detected_via_uia_skipped_or_flagged():
    # Typing into a UIA-confirmed password field needs an explicit OK.
    resolve_to(target("Password", control_type="Edit", confidence=1.0,
                      is_password=True))
    plan = plan_for("type_into_control", hint="password", text="hunter2")
    plan.requires_confirmation = False
    result = validate_action(plan, CFG)
    assert result.requires_confirmation and result.risk_level == "high"
    assert "password" in result.reason.lower()

    # An ordinary text box is frictionless.
    resolve_to(target("Search", control_type="Edit", confidence=1.0))
    plan = plan_for("type_into_control", hint="search", text="cats")
    assert not validate_action(plan, CFG).requires_confirmation

    # read_window_text never reads a password field's contents.
    from app.control.uia import UIABackend

    class FakeControl:
        def __init__(self, name, is_password=False):
            self.Name = name
            self.IsPassword = is_password
            self.ControlTypeName = "EditControl"

    backend = UIABackend(CFG)
    backend._root_window = lambda scope: object()
    backend._walk = lambda root: [FakeControl("Username"),
                                  FakeControl("hunter2", is_password=True),
                                  FakeControl("Sign in")]
    text = backend.read_window_text(Scope())
    assert "Username" in text and "Sign in" in text
    assert "hunter2" not in text            # never read, never logged


# ---- the confirmation card carries the target ---------------------------------------------

def test_confirmation_card_shows_target_and_hides_internal_args():
    from app.agent.confirmation_manager import ConfirmationManager

    guess = target("Send", backend="vision", confidence=0.7)
    guess.crop_data_url = "data:image/jpeg;base64,ZZZ"
    resolve_to(guess)
    plan = plan_for(hint="send button")
    result = validate_action(plan, CFG)

    manager = ConfirmationManager(timeout_s=0)
    manager.request(plan, result, "click send")
    payload = manager.payload()

    assert payload["details"]["target"]["name"] == "Send"
    assert payload["details"]["target"]["crop_data_url"].startswith("data:image")
    assert "_resolved" not in payload["arguments"]     # no base64 in the args blob
    assert payload["arguments"] == {"hint": "send button"}
    assert payload["strong_required"] is True
