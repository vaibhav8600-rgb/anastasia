"""Phase 11A: voice approval system. One confirmation at a time, expiry,
local phrase parsing gated on `has_pending()`, and a strong phrase for
destructive-tier actions. Nothing executes on ambiguity."""

import time

from app.agent.confirmation_manager import (ConfirmationManager, ConfirmState,
                                            Outcome, requires_strong_approval)
from app.agent.normalizer import normalize_command
from app.agent.pipeline import (CONFIRM_TIMEOUT_MESSAGE, STRONG_REQUIRED_MESSAGE,
                                UNCLEAR_MESSAGE, CommandPipeline)
from app.agent.router import match_rule
from app.agent.safety import validate_action
from app.llm.intent_parser import ActionPlan
from tests.fakes import (FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech,
                         make_config)

CFG = make_config()


def make_pipeline(agent=None, rule=None, **kwargs):
    ui = FakePipelineUI()
    speech = FakeSpeech()
    agent = agent or FakeAgent(CFG, rule=rule or (lambda t: match_rule(t, CFG)))
    pipeline = CommandPipeline(config=CFG, agent=agent, history=FakeHistory(),
                               ui=ui, speech=speech, run_async=False, **kwargs)
    return pipeline, ui, speech, agent


def terminal_pipeline(**kwargs):
    """A pending run_terminal — destructive tier, needs the strong phrase."""
    plan = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                      arguments={"command": "git status"})
    pipeline, ui, speech, agent = make_pipeline(rule=lambda t: plan, **kwargs)
    pipeline.submit("run git status", source="voice")
    assert pipeline.pending is not None
    assert pipeline.pending.strong_required
    return pipeline, ui, speech, agent


def pending_close_chrome(**kwargs):
    """A pending window_control — needs confirmation, but casual tier."""
    pipeline, ui, speech, agent = make_pipeline(**kwargs)
    pipeline.submit("close chrome", source="voice")
    assert pipeline.pending is not None
    assert not pipeline.pending.strong_required
    return pipeline, ui, speech, agent


# ---- approve / cancel by voice --------------------------------------------------

def test_voice_approve_executes_pending_action():
    pipeline, ui, _speech, agent = pending_close_chrome()
    pipeline.submit("approve", source="voice")
    assert agent.executed and agent.executed[-1].intent == "window_control"
    assert pipeline.pending is None
    assert pipeline.confirm.state is ConfirmState.IDLE


def test_voice_cancel_discards_pending_action():
    pipeline, ui, _speech, agent = pending_close_chrome()
    pipeline.submit("cancel", source="voice")
    assert agent.executed == []
    assert pipeline.pending is None
    assert pipeline.confirm.state is ConfirmState.IDLE
    assert any("Cancelled" in m for m in ui.infos)


# ---- the has_pending() gate -------------------------------------------------------

def test_random_yes_without_pending_confirmation_does_nothing():
    """A stray "yes" must never be parsed as approval — with nothing pending
    it falls through to normal chat/command routing."""
    manager = ConfirmationManager()
    assert not manager.has_pending()
    assert manager.handle_utterance("yes") is Outcome.NONE
    assert manager.handle_utterance("anna approve") is Outcome.NONE
    assert manager.state is ConfirmState.IDLE

    # ...and through the pipeline: nothing executed, nothing confirmed, the
    # utterance was routed like any other input.
    pipeline, _ui, _speech, agent = make_pipeline(rule=lambda t: None)
    pipeline.submit("yes", source="voice")
    assert pipeline.pending is None
    assert agent.executed == []
    assert (agent.chat_calls + agent.llm_calls) == 1   # normal routing ran


# ---- expiry -----------------------------------------------------------------------

def test_confirmation_expires_after_timeout_and_resets_state():
    pipeline, ui, _speech, agent = pending_close_chrome(
        confirm_timeout_seconds=0.1)
    # Poll the user-visible message, not `pending`: expiry clears pending
    # first and speaks last, so polling on the flag races that gap.
    deadline = time.time() + 5
    while CONFIRM_TIMEOUT_MESSAGE not in ui.infos and time.time() < deadline:
        time.sleep(0.02)
    assert pipeline.pending is None
    assert pipeline.confirm.state is ConfirmState.IDLE
    assert agent.executed == []                      # expiry never executes
    assert CONFIRM_TIMEOUT_MESSAGE in ui.infos
    assert ui.states[-1] == "ready"


# ---- strong phrase for destructive tier --------------------------------------------

def test_high_risk_action_requires_strong_approval_phrase():
    pipeline, _ui, _speech, agent = terminal_pipeline()
    pipeline.submit("anna approve", source="voice")
    assert agent.executed and agent.executed[-1].tool_name == "run_terminal"
    assert pipeline.pending is None


def test_weak_phrase_insufficient_for_high_risk_action():
    pipeline, ui, speech, agent = terminal_pipeline()
    pipeline.submit("yes", source="voice")           # casual — refused
    assert agent.executed == []
    assert pipeline.pending is not None              # still armed, not lost
    assert STRONG_REQUIRED_MESSAGE in ui.infos
    assert any("Anna approve" in s for s in speech.spoken)
    # the strong phrase then unlocks the very same pending action
    pipeline.submit("i approve", source="voice")
    assert agent.executed and agent.executed[-1].tool_name == "run_terminal"


def test_strong_phrase_survives_wake_word_normalization():
    """The normalizer strips a leading wake word, so normalize("anna approve")
    is "approve". Parsing the NORMALIZED text would silently downgrade the
    strong phrase to a casual one — confirmations must read the RAW text."""
    assert normalize_command("anna approve", CFG).cleaned == "approve"
    pipeline, _ui, _speech, agent = terminal_pipeline()
    pipeline.submit("anna approve", source="voice")
    assert agent.executed, "strong phrase was downgraded by normalization"


def test_requires_strong_approval_ignores_understated_plan_risk():
    """A plan that claims it's harmless cannot dodge the strong phrase — the
    decision reads the validator's result and a hardcoded tool list."""
    plan = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                      arguments={"command": "git status"},
                      risk_level="low", requires_confirmation=False)
    safety = validate_action(plan, CFG)
    assert requires_strong_approval(plan, safety)


# ---- utility phrases ----------------------------------------------------------------

def test_repeat_confirmation_on_request():
    pipeline, ui, speech, agent = pending_close_chrome()
    spoken_before = len(speech.spoken)
    pipeline.submit("what are you asking?", source="voice")
    assert pipeline.pending is not None              # still pending
    assert agent.executed == []
    assert len(speech.spoken) > spoken_before        # repeated aloud
    assert any("approve" in s for s in speech.spoken[spoken_before:])


def test_show_details_expands_card():
    pipeline, ui, _speech, agent = pending_close_chrome()
    pipeline.submit("show details", source="voice")
    assert pipeline.pending is not None
    assert agent.executed == []
    assert ui.details_shown, "the card was never expanded"
    payload = ui.details_shown[-1]
    assert payload["tool"] == "window_control"
    assert payload["arguments"] == {"action": "close", "app": "chrome"}


def test_unclear_utterance_reasks_and_never_executes():
    pipeline, ui, _speech, agent = pending_close_chrome()
    pipeline.submit("maybe later or something", source="voice")
    assert agent.executed == []
    assert pipeline.pending is not None
    assert UNCLEAR_MESSAGE in ui.infos


# ---- races and queueing ---------------------------------------------------------------

def test_ui_click_and_voice_approval_both_valid_first_wins():
    # voice first -> the later UI click is a harmless no-op
    pipeline, _ui, _speech, agent = pending_close_chrome()
    pipeline.submit("approve", source="voice")
    pipeline.approve_pending()                       # stale click
    assert len(agent.executed) == 1

    # UI click first -> a later voice "approve" finds nothing pending and is
    # routed as ordinary input, never re-executing the action
    pipeline, _ui, _speech, agent = pending_close_chrome()
    pipeline.approve_pending()
    assert len(agent.executed) == 1
    assert pipeline.confirm.handle_utterance("approve") is Outcome.NONE
    assert len(agent.executed) == 1


def test_second_risky_action_while_pending_is_deferred_not_overwritten():
    pipeline, _ui, _speech, agent = pending_close_chrome()
    first = pipeline.pending
    other = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                       arguments={"command": "git status"})
    safety = validate_action(other, CFG)

    result = pipeline.confirm.request(other, safety, "run git status")
    assert not result.accepted and result.deferred
    assert pipeline.pending is first                 # untouched, not replaced
    assert pipeline.pending.plan.intent == "window_control"

    # the Gemini Live path (10C) refuses for the same reason
    assert not pipeline.request_external_confirmation(
        other, safety, "Gemini Live: run_terminal", lambda ok: None)
    assert pipeline.pending is first
    assert agent.executed == []


# ---- hands-free "listening_for_confirmation" (opt-in) --------------------------------

def _controller(**cfg):
    from app.main import Controller
    from tests.fakes import FakeMainUI

    class _Mem:
        def get(self, key, default=None):
            return "Vaibhav" if key == "user_name" else default
        def set(self, key, value): pass

    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(**cfg), memory=_Mem(),
                            history=FakeHistory())
    controller.speech.shutdown()
    return controller


def test_confirmation_voice_listen_is_opt_in_and_never_grabs_mic_by_default():
    """Default OFF: a pending card must not open the microphone on its own —
    the 9C rule (never grab the mic while awaiting approval) still holds."""
    controller = _controller()
    opened = []
    controller.toggle_mic = lambda src="voice": opened.append(src)
    controller._hands_free_active = True
    controller.pipeline.pending = ("id", None, None, "run git status")
    controller._on_speaking_changed(False)
    time.sleep(0.7)                                  # past the echo-tail delay
    assert opened == []


def test_confirmation_voice_listen_reopens_mic_when_enabled():
    controller = _controller(confirmation_voice_listen=True)
    opened = []
    controller.toggle_mic = lambda src="voice": opened.append(src)
    controller._hands_free_active = True
    controller.pipeline.pending = ("id", None, None, "run git status")
    controller._on_speaking_changed(False)
    deadline = time.time() + 3
    while not opened and time.time() < deadline:
        time.sleep(0.02)
    assert opened == ["voice"]                       # listening_for_confirmation

    # ...and it stands down the moment the confirmation is resolved.
    controller2 = _controller(confirmation_voice_listen=True)
    opened2 = []
    controller2.toggle_mic = lambda src="voice": opened2.append(src)
    controller2._hands_free_active = True
    controller2.pipeline.pending = ("id", None, None, "x")
    controller2._on_speaking_changed(False)
    controller2.pipeline.pending = None              # approved/cancelled meanwhile
    time.sleep(0.7)
    assert opened2 == []


# ---- state machine ----------------------------------------------------------------------

def test_state_machine_transitions():
    manager = ConfirmationManager(timeout_s=0)        # no timer
    plan = ActionPlan(intent="open_app", tool_name="open_app",
                      arguments={"app_name": "paint"})
    safety = validate_action(plan, CFG)
    assert manager.state is ConfirmState.IDLE

    result = manager.request(plan, safety, "open paint")
    assert result.accepted and manager.state is ConfirmState.PENDING
    manager.begin_listening()
    assert manager.state is ConfirmState.LISTENING
    assert manager.has_pending()

    assert manager.handle_utterance("go ahead") is Outcome.APPROVED
    taken = manager.take(result.action_id)
    assert taken is not None and manager.state is ConfirmState.IDLE
    assert manager.take(result.action_id) is None     # idempotent
