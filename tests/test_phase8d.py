"""Phase 8D: latency polish + conversational continuity.
In-process Piper warm-path, first-audio telemetry, 10-turn hybrid context,
hands-free follow-up gating, and the required cross-cutting safety proof."""

import time

from app.llm.prompt_builder import build_chat_messages
from tests.fakes import (FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech,
                         make_config)


# ---- 8D.2 conversational continuity ------------------------------------------

def test_chat_messages_include_history_turns():
    turns = [{"role": "user", "text": "my name is Sam"},
             {"role": "assistant", "text": "Nice to meet you, Sam!"}]
    messages = build_chat_messages("what's my name?", make_config(), None,
                                   history_turns=turns)
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "my name is Sam"
    assert messages[-1]["content"] == "what's my name?"


def test_chat_messages_drop_blank_and_bad_turns():
    turns = [{"role": "info", "text": "ignored"},
             {"role": "user", "text": ""},
             {"role": "assistant", "text": "kept"}]
    messages = build_chat_messages("hi", make_config(), None, history_turns=turns)
    contents = [m["content"] for m in messages]
    assert "ignored" not in contents and "" not in contents
    assert "kept" in contents


def test_hybrid_uses_10_turns_local_uses_4(monkeypatch):
    from app.agent.router import Agent
    seen = {}

    def make_agent(mode, circuit_open=False):
        agent = Agent(make_config(groq_api_key="gsk_x" * 4), None, None)
        monkeypatch.setattr(agent.brain, "mode", lambda: mode)
        monkeypatch.setattr(agent.brain, "circuit_open", lambda: circuit_open)

        def fake_complete(kind, messages, model=None, payload_classes=None):
            from app.llm.providers import LLMResult
            return LLMResult(text="hi there", provider="groq")
        monkeypatch.setattr(agent.brain, "complete", fake_complete)
        agent.recent_chat_turns = lambda n: seen.__setitem__("n", n) or []
        return agent

    make_agent("hybrid").plan_chat("hello")
    assert seen["n"] == 10
    make_agent("local_only").plan_chat("hello")
    assert seen["n"] == 4


def test_recent_chat_turns_maps_conversation(monkeypatch):
    from app.main import Controller
    from tests.fakes import FakeMainUI
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(), memory=_Mem(), history=FakeHistory())
    controller.speech.shutdown()
    controller.conversation.add("user", "hey")
    controller.conversation.add("anna", "hi love")
    controller.conversation.add("anna", "opened Paint", action={"intent": "open_app"})
    controller.conversation.add("user", "how are you")   # current line
    turns = controller._recent_chat_turns(10)
    # action results excluded; current user line excluded
    assert turns == [{"role": "user", "text": "hey"},
                     {"role": "assistant", "text": "hi love"}]


# ---- 8D.2 hands-free follow-up gating ----------------------------------------

def test_hands_free_off_does_not_arm():
    from app.main import Controller
    from tests.fakes import FakeMainUI
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(hands_free_followup=False),
                            memory=_Mem(), history=FakeHistory())
    controller.speech.shutdown()
    controller.arm_followup()
    assert controller._followup_armed is False


def test_hands_free_on_arms_then_fires_once():
    from app.main import Controller
    from tests.fakes import FakeMainUI
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(hands_free_followup=True),
                            memory=_Mem(), history=FakeHistory())
    controller.speech.shutdown()
    fired = []
    controller._start_followup_listen = lambda: fired.append(True)
    controller.arm_followup()
    assert controller._followup_armed is True
    controller._on_speaking_changed(False)   # speech finished
    assert controller._followup_armed is False and fired == [True]
    controller._on_speaking_changed(False)   # no re-fire
    assert fired == [True]


# ---- 8D.1 first-audio telemetry ----------------------------------------------

def test_first_audio_callback_fires_once_per_utterance():
    speech = FakeSpeech()   # placeholder; use the real class below

    from app.voice.speech_output import SpeechOutput
    got = []
    s = SpeechOutput(make_config(tts_backend="windows"))
    s.shutdown()
    s.on_first_audio = got.append
    s._utterance_start = time.perf_counter() - 0.05
    s._first_audio_pending = True
    s._mark_first_audio()
    s._mark_first_audio()   # already marked -> no second callback
    assert len(got) == 1 and got[0] >= 40


# ---- required cross-cutting safety proof (spec "Final instructions") ---------

def test_groq_plan_still_passes_local_safety_validator():
    """A mocked Groq plan flows through the full pipeline and is still
    checked by the LOCAL safety validator (terminal -> confirmation)."""
    from app.agent.pipeline import CommandPipeline
    from app.llm.intent_parser import ActionPlan

    config = make_config()
    groq_plan = ActionPlan(
        assistant_message="Running that now.", intent="run_terminal",
        tool_name="run_terminal", arguments={"command": "git status"},
        risk_level="low", requires_confirmation=False)  # cloud UNDERSTATES risk
    agent = FakeAgent(config, rule=lambda t: None, llm_plan=groq_plan)
    ui = FakePipelineUI()
    pipeline = CommandPipeline(config=config, agent=agent, history=FakeHistory(),
                               ui=ui, speech=FakeSpeech(), run_async=False)
    pipeline.submit("run git status in my project", source="typed")

    # local safety escalated the cloud plan to a confirmation despite the
    # cloud saying requires_confirmation=false — the gate is intact.
    assert pipeline.pending is not None
    assert ui.confirmations, "terminal plan from cloud must still be gated locally"
    assert not agent.executed   # nothing ran without approval


class _Mem:
    def get(self, key, default=None):
        return "Vaibhav" if key == "user_name" else default

    def set(self, key, value):
        pass
