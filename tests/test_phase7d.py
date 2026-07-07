"""Phase 7D: chat/command split, handoff, memory, and reply preservation."""

from app.agent.pipeline import CommandPipeline
from app.agent.router import Agent, classify_input_mode
from app.llm.intent_parser import ActionPlan
from app.llm.prompt_builder import (
    CHAT_HANDOFF, build_chat_messages, estimate_tokens,
)
from tests.fakes import FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech, make_config


class Memory:
    def __init__(self, data=None):
        self.data = data or {"user_name": "Vaibhav",
                             "preferred_browser": "Chrome"}

    def get(self, key, default=None):
        return self.data.get(key, default)


def pipeline_with(agent, config=None):
    config = config or agent.config
    ui = FakePipelineUI()
    pipeline = CommandPipeline(config, agent, FakeHistory(), ui, FakeSpeech(),
                               run_async=False)
    return pipeline, ui


def test_chat_input_routes_to_chat_mode():
    config = make_config()
    agent = FakeAgent(config, rule=lambda _text: None,
                      chat_plan=ActionPlan(intent="no_action",
                                           assistant_message="I'm good!"))
    pipeline, _ui = pipeline_with(agent)
    pipeline.submit("How about you", source="typed")
    assert agent.chat_calls == 1 and agent.llm_calls == 0


def test_command_input_routes_to_command_mode():
    config = make_config()
    agent = FakeAgent(config, rule=lambda _text: None)
    pipeline, _ui = pipeline_with(agent)
    pipeline.submit("open something weird", source="typed")
    assert agent.llm_calls == 1 and agent.chat_calls == 0


def test_chat_prompt_under_250_tokens():
    system = build_chat_messages("hello", make_config(), Memory())[0]["content"]
    assert estimate_tokens(system) < 250


def test_chat_mode_reply_is_llm_text_not_canned():
    config = make_config()
    reply = "Honestly? Better now that you're here."
    agent = FakeAgent(config, rule=lambda _text: None,
                      chat_plan=ActionPlan(intent="no_action",
                                           assistant_message=reply))
    pipeline, ui = pipeline_with(agent)
    pipeline.submit("How about you", source="typed")
    assert ui.annas == [reply]


class RecordingLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return next(self.responses)


def test_handoff_marker_reruns_command_mode():
    config = make_config()
    agent = Agent(config, Memory(), FakeHistory())
    agent.llm = RecordingLLM([
        CHAT_HANDOFF,
        '{"assistant_message":"Opening Paint.","intent":"open_app",'
        '"tool_name":"open_app","arguments":{"app_name":"paint"}}',
    ])
    plan, handed_off = agent.plan_chat("Could you open Paint?")
    assert handed_off is True
    assert plan.intent == "open_app" and plan.arguments["app_name"] == "paint"
    assert len(agent.llm.calls) == 2


def test_ambiguous_input_prefers_command_mode():
    assert classify_input_mode("weather tomorrow") == "command"
    assert classify_input_mode("maybe the project later") == "command"


def test_chat_model_config_respected():
    config = make_config(chat_model="llama3.2:1b")
    agent = Agent(config, Memory(), FakeHistory())
    agent.llm = RecordingLLM(["Hey — I'm right here."])
    plan, handed_off = agent.plan_chat("How are you?")
    assert not handed_off and plan.assistant_message.startswith("Hey")
    assert agent.llm.calls[0][1]["model"] == "llama3.2:1b"
    assert agent.llm.calls[0][1]["json_format"] is False
    assert agent.llm.calls[0][1]["num_predict"] == 100


def test_memory_lines_included_in_chat_prompt():
    memory = Memory({
        "user_name": "Vaibhav",
        "preferred_browser": "Chrome",
        "work_apps": ["VS Code", "Notepad"],
        "password": "never include this",
    })
    system = build_chat_messages("Do you know me?", make_config(), memory)[0]["content"]
    assert "Vaibhav" in system and "Chrome" in system and "VS Code" in system
    assert "never include this" not in system
