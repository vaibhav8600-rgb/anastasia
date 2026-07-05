"""Phase 2: clean user/developer separation — welcome message, setup card,
dependency warnings in devlog only, structured conversation model."""

from app.agent.conversation import Conversation
from app.agent.devlog import devlog
from app.agent.pipeline import CommandPipeline
from app.agent.router import match_rule
from app.main import Controller
from app.tools import ToolResult
from tests.fakes import (FakeAgent, FakeHistory, FakeMainUI, FakePipelineUI,
                         FakeSpeech, make_config)


class FakeMemory:
    def __init__(self, name="Vaibhav"):
        self.name = name

    def get(self, key, default=None):
        return self.name if key == "user_name" else default


class FakeLLM:
    def __init__(self, available=False, models=()):
        self.available = available
        self.models = list(models)
        self.warmed = 0

    def is_available(self): return self.available
    def list_models(self): return self.models
    def warm_up(self, messages=None):
        self.warmed += 1
        return 1.0


def make_controller(llm=None, memory=None):
    ui = FakeMainUI()
    controller = Controller(ui=ui, autostart=False, config=make_config(),
                            memory=memory or FakeMemory(),
                            history=FakeHistory())
    controller.speech.shutdown()
    controller.agent.llm = llm or FakeLLM()
    return controller, ui


def patch_voice_checks(monkeypatch, stt_ok=True, mic_ok=True, piper_ok=False):
    monkeypatch.setattr("app.voice.stt_whisper.backend_ready",
                        lambda config: (stt_ok, "faster-whisper ready (model: base)."
                                        if stt_ok else "faster-whisper is not installed."))
    monkeypatch.setattr("app.main.microphone_available", lambda: mic_ok)
    monkeypatch.setattr("app.voice.tts_piper.piper_available", lambda config: piper_ok)


# ---- startup (sec 18) -------------------------------------------------------

def test_startup_welcome_is_clean(monkeypatch):
    devlog.clear()
    patch_voice_checks(monkeypatch)
    controller, ui = make_controller(llm=FakeLLM(available=False))
    controller._startup_checks()

    assert ui.messages["assistant"][0] == "Hi Vaibhav, I'm Anna 💜\nI'm ready when you are."
    # exactly one setup card, containing the single critical issue
    assert len(ui.setup_cards) == 1
    assert any("Ollama is not running" in issue for issue in ui.setup_cards[0])
    # chat contains no dependency/status spam
    chat = " ".join(ui.messages["assistant"] + ui.messages["info"]
                    + ui.messages["error"]).lower()
    assert "piper" not in chat
    assert "whisper" not in chat
    assert "ollama is running" not in chat


def test_healthy_startup_shows_no_setup_card(monkeypatch):
    devlog.clear()
    patch_voice_checks(monkeypatch)
    llm = FakeLLM(available=True, models=["llama3.2:3b"])
    controller, ui = make_controller(llm=llm)
    controller._startup_checks()

    assert ui.setup_cards == []
    assert ui.setup_card_hidden >= 1
    assert llm.warmed == 1                       # model warm-up kicked off
    assert len(ui.messages["assistant"]) == 1    # just the welcome


def test_dependency_warnings_go_to_devlog_not_chat(monkeypatch):
    devlog.clear()
    devlog.echo_to_stdout = False
    try:
        patch_voice_checks(monkeypatch, piper_ok=False)
        llm = FakeLLM(available=True, models=["llama3.2:3b"])
        controller, ui = make_controller(llm=llm)
        controller._startup_checks()

        log_text = " ".join(e["message"] for e in devlog.entries())
        assert "Piper" in log_text               # warning recorded for devs
        chat = " ".join(sum(ui.messages.values(), [])).lower()
        assert "piper" not in chat               # user never sees it
    finally:
        devlog.echo_to_stdout = True


def test_recheck_clears_card_when_fixed(monkeypatch):
    devlog.clear()
    patch_voice_checks(monkeypatch)
    controller, ui = make_controller(llm=FakeLLM(available=False))
    controller.run_health_checks()
    assert len(ui.setup_cards) == 1

    controller.agent.llm = FakeLLM(available=True, models=["llama3.2:3b"])
    controller.run_health_checks()               # what the Recheck button runs
    assert len(ui.setup_cards) == 1              # no new card
    assert ui.setup_card_hidden >= 1


# ---- conversation model -----------------------------------------------------

def test_conversation_records_structured_entries():
    conversation = Conversation()
    seen = []
    conversation.subscribe(seen.append)
    conversation.add("user", "open paint")
    conversation.add("anna", "Done — I opened Paint for you.",
                     action={"intent": "open_app", "success": True, "data": None})

    snap = conversation.snapshot()
    assert [m["role"] for m in snap] == ["user", "anna"]
    assert snap[1]["action"]["intent"] == "open_app"
    assert all(m["ts"] for m in snap)
    assert len(seen) == 2

    conversation.clear()
    assert conversation.snapshot() == []


def test_pipeline_result_carries_action_payload():
    config = make_config()
    agent = FakeAgent(config, rule=lambda t: match_rule(t, config),
                      execute_result=ToolResult(True, "Screenshot captured.",
                                                data="C:/shots/anna_1.png"))
    ui = FakePipelineUI()
    pipeline = CommandPipeline(config=config, agent=agent, history=FakeHistory(),
                               ui=ui, speech=FakeSpeech(), run_async=False)
    pipeline.submit("take screenshot", source="typed")

    assert len(ui.results) == 1
    text, action = ui.results[0]
    assert text == "Screenshot captured."
    assert action == {"intent": "take_screenshot", "success": True,
                      "data": "C:/shots/anna_1.png"}


def test_controller_show_result_renders_card_and_path():
    controller, ui = make_controller()
    controller.show_result("Screenshot captured.",
                           {"intent": "take_screenshot", "success": True,
                            "data": "C:/shots/anna_1.png"})

    assert "Screenshot captured." in ui.messages["assistant"]
    assert any("C:/shots/anna_1.png" in m for m in ui.messages["info"])
    last = controller.conversation.snapshot()[-1]
    assert last["role"] == "anna"
    assert last["action"]["data"] == "C:/shots/anna_1.png"


def test_clear_history_clears_conversation():
    controller, ui = make_controller()
    controller.show_anna("hello")
    assert controller.conversation.snapshot()
    controller.clear_history()
    # only the "History cleared." info remains after the clear
    entries = controller.conversation.snapshot()
    assert len(entries) == 1 and entries[0]["role"] == "info"
