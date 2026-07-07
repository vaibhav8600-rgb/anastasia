"""CommandPipeline guarantees: busy flag always resets, watchdog force-clears,
typed/voice separation, confirmation auto-cancel, no debug logs in chat."""

import threading
import time

from app.agent.pipeline import (
    BUSY_MESSAGE, CONFIRM_TIMEOUT_MESSAGE, GARBLE_MESSAGE, TIMEOUT_MESSAGE,
    CommandPipeline,
)
from app.agent.router import match_rule
from app.llm.intent_parser import ActionPlan
from app.llm.ollama_client import OllamaError
from app.tools import ToolResult
from app.voice.stt_whisper import SpeechConfidence
from tests.fakes import FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech, make_config

CFG = make_config()


def make_pipeline(agent=None, run_async=False, **kwargs):
    ui = FakePipelineUI()
    speech = FakeSpeech()
    agent = agent or FakeAgent(CFG, rule=lambda t: match_rule(t, CFG))
    pipeline = CommandPipeline(config=CFG, agent=agent, history=FakeHistory(),
                               ui=ui, speech=speech, run_async=run_async, **kwargs)
    return pipeline, ui, speech, agent


# ---- busy state ------------------------------------------------------------

def test_busy_state_resets_after_tool_success():
    pipeline, ui, speech, agent = make_pipeline()
    pipeline.submit("open notepad", source="typed")
    assert not pipeline.is_processing_command
    assert ui.states[-1] == "ready"
    assert agent.executed and agent.executed[0].intent == "open_app"
    assert speech.spoken  # result spoken asynchronously


def test_busy_state_resets_after_tool_error():
    agent = FakeAgent(CFG, rule=lambda t: match_rule(t, CFG),
                      execute_exc=RuntimeError("boom"))
    pipeline, ui, _, _ = make_pipeline(agent=agent)
    pipeline.submit("open notepad", source="typed")
    assert not pipeline.is_processing_command
    assert ui.states[-1] == "ready"
    assert ui.errors  # friendly error surfaced


def test_busy_state_resets_after_ollama_timeout():
    agent = FakeAgent(CFG, rule=lambda t: None,
                      llm_exc=OllamaError("The local model took too long to answer."))
    pipeline, ui, _, _ = make_pipeline(agent=agent)
    pipeline.submit("what's the meaning of life", source="typed")
    assert not pipeline.is_processing_command
    assert ui.states[-1] == "ready"
    assert TIMEOUT_MESSAGE in ui.errors
    # next command must go through immediately
    pipeline.agent = FakeAgent(CFG, rule=lambda t: match_rule(t, CFG))
    pipeline.submit("open notepad", source="typed")
    assert BUSY_MESSAGE not in ui.infos


def test_busy_watchdog_force_clears_after_45s():
    release = threading.Event()

    def blocking_rule(text):
        release.wait(3.0)
        return None

    agent = FakeAgent(CFG, rule=blocking_rule,
                      llm_plan=ActionPlan(intent="no_action", assistant_message="hi"))
    pipeline, ui, _, _ = make_pipeline(agent=agent, run_async=True,
                                       watchdog_seconds=0.05)
    pipeline.submit("open something weird", source="typed")
    deadline = time.time() + 1.0
    while not pipeline.is_processing_command and time.time() < deadline:
        time.sleep(0.005)
    assert pipeline.is_processing_command  # worker is genuinely stuck
    deadline = time.time() + 2.0
    while pipeline.is_processing_command and time.time() < deadline:
        time.sleep(0.01)
    assert not pipeline.is_processing_command  # watchdog force-cleared
    release.set()


def test_empty_stt_does_not_set_busy():
    pipeline, ui, _, agent = make_pipeline()
    pipeline.submit("", source="voice")
    pipeline.submit("   ", source="voice")
    assert not pipeline.is_processing_command
    assert "thinking" not in ui.states
    assert not agent.executed


# ---- typed vs voice separation ---------------------------------------------

def test_typed_command_does_not_start_stt():
    from tests.fakes import FakeRecorder
    recorder = FakeRecorder()
    pipeline, ui, _, agent = make_pipeline()
    pipeline.cancel_recording = recorder.cancel
    pipeline.submit("open notepad", source="typed")
    assert recorder.starts == 0
    assert "listening" not in ui.states
    assert agent.executed


def test_typed_command_cancels_active_recording():
    from tests.fakes import FakeRecorder
    recorder = FakeRecorder()
    recorder.start()
    pipeline, _, _, _ = make_pipeline()
    pipeline.cancel_recording = recorder.cancel
    pipeline.submit("open notepad", source="typed")
    assert recorder.cancels == 1
    assert not recorder.recording


def test_voice_garble_asks_clarification_instead_of_llm():
    agent = FakeAgent(CFG, rule=lambda t: match_rule(t, CFG))
    pipeline, ui, _, _ = make_pipeline(agent=agent)
    pipeline.submit("open no pass for you", source="voice",
                    confidence=SpeechConfidence(avg_logprob=-1.4,
                                                no_speech_prob=0.1))
    assert agent.llm_calls == 0
    assert GARBLE_MESSAGE in ui.annas
    assert not pipeline.is_processing_command


def test_multi_sentence_stt_executes_first_clear_command():
    pipeline, ui, _, agent = make_pipeline()
    pipeline.submit("Open Paint. Open no pass for you.", source="voice")
    assert agent.executed and agent.executed[0].arguments["app_name"] == "paint"
    assert ui.users == ["Open Paint"]  # cleaned command shown, not the garble


# ---- confirmation -----------------------------------------------------------

def _terminal_agent():
    plan = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                      arguments={"command": "git status"}, risk_level="high",
                      requires_confirmation=True,
                      confirmation_message="Run `git status`?")
    return FakeAgent(CFG, rule=lambda t: plan)


def test_confirmation_timeout_auto_cancels_and_resets_state():
    pipeline, ui, _, agent = make_pipeline(agent=_terminal_agent(),
                                           confirm_timeout_seconds=0.05)
    pipeline.submit("run git status", source="typed")
    assert pipeline.pending is not None
    assert ui.states[-1] == "waiting_confirmation"
    deadline = time.time() + 2.0
    while pipeline.pending is not None and time.time() < deadline:
        time.sleep(0.01)
    assert pipeline.pending is None
    assert CONFIRM_TIMEOUT_MESSAGE in ui.infos
    assert ui.states[-1] == "ready"
    assert not agent.executed  # never ran without approval


def test_confirmation_approve_executes_and_resets():
    pipeline, ui, _, agent = make_pipeline(agent=_terminal_agent())
    pipeline.submit("run git status", source="typed")
    assert pipeline.pending is not None
    pipeline.approve_pending()
    assert agent.executed
    assert pipeline.pending is None
    assert not pipeline.is_processing_command
    assert ui.states[-1] == "ready"


def test_confirmation_cancel_resets():
    pipeline, ui, _, agent = make_pipeline(agent=_terminal_agent())
    pipeline.submit("run git status", source="typed")
    pipeline.cancel_pending()
    assert not agent.executed
    assert pipeline.pending is None
    assert ui.states[-1] == "ready"


# ---- clean chat -------------------------------------------------------------

def test_debug_logs_not_shown_in_main_chat():
    pipeline, ui, _, _ = make_pipeline()
    pipeline.submit("open notepad", source="typed")
    for message in ui.all_messages:
        assert "intent=" not in message
        assert "risk=" not in message
        assert "args=" not in message
