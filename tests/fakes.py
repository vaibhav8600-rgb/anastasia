"""Shared test doubles for pipeline/controller tests. No GUI, no network,
no real tool execution, no files outside pytest tmp dirs."""

from app.config import AppConfig
from app.llm.intent_parser import ActionPlan
from app.tools import ToolResult


class TestConfig(AppConfig):
    """AppConfig that never writes config.json."""

    def save(self, path=None) -> None:  # noqa: ARG002
        pass


def make_config(**overrides) -> TestConfig:
    overrides.setdefault("safe_folders",
                         ["C:/Users/Test/Downloads", "C:/Users/Test/Documents",
                          "C:/Users/Test/Desktop", "C:/Users/Test/Projects"])
    return TestConfig(**overrides)


class FakePipelineUI:
    """Implements the pipeline's UI interface and records everything."""

    def __init__(self):
        self.users, self.annas, self.errors, self.infos = [], [], [], []
        self.results = []                 # (text, action) tuples
        self.states = []
        self.confirmations = []
        self.confirmation_meta = []
        self.details_shown = []           # 11A: "show details" payloads
        self.hidden = 0

    def show_confirmation_details(self, payload):
        self.details_shown.append(payload)

    def show_user(self, text): self.users.append(text)
    def show_anna(self, text): self.annas.append(text)
    def show_result(self, text, action): self.results.append((text, action))
    def show_error(self, text): self.errors.append(text)
    def show_info(self, text): self.infos.append(text)
    def set_state(self, state, detail=""): self.states.append(state)
    def ask_confirmation(self, action_id, transcript, plan, safety,
                         kind="safety", message=""):
        self.confirmations.append((action_id, transcript, plan, safety))
        self.confirmation_meta.append((kind, message))
    def hide_confirmation(self): self.hidden += 1

    @property
    def all_messages(self):
        return (self.users + self.annas + self.errors + self.infos
                + [text for text, _ in self.results])


class FakeSpeech:
    def __init__(self):
        self.spoken = []
        self.cancelled = 0
        self._speaking = False

    @property
    def speaking(self):
        return self._speaking

    def speak_async(self, text): self.spoken.append(text)

    def cancel(self):
        self.cancelled += 1
        self._speaking = False

    def shutdown(self): pass


class FakeHistory:
    def __init__(self): self.rows = []
    def log(self, *args, **kwargs): self.rows.append((args, kwargs))
    def recent(self, limit=50): return []
    def clear(self): self.rows = []
    def close(self): pass


class ExplodingLLM:
    """Any use means a rule-routable command leaked to Ollama — fail hard."""

    def chat(self, *args, **kwargs):
        raise AssertionError("LLM was called for a rule-routable command!")

    def is_available(self): return False
    def list_models(self): return []
    def warm_up(self): return None


class FakeAgent:
    """Configurable stand-in for router.Agent. Never runs real tools."""

    def __init__(self, config, rule=None, llm_plan=None, llm_exc=None,
                 execute_result=None, execute_exc=None,
                 chat_plan=None, chat_handoff=False):
        self.config = config
        self.rule = rule                  # callable(text) -> ActionPlan|None
        self.llm_plan = llm_plan
        self.llm_exc = llm_exc
        self.execute_result = execute_result or ToolResult(True, "Done.")
        self.execute_exc = execute_exc
        self.llm = ExplodingLLM()
        self.llm_calls = 0
        self.chat_calls = 0
        self.chat_plan = chat_plan
        self.chat_handoff = chat_handoff
        self.executed = []

    def plan_rule(self, text):
        return self.rule(text) if self.rule else None

    def plan_llm(self, text):
        self.llm_calls += 1
        if self.llm_exc:
            raise self.llm_exc
        return self.llm_plan or ActionPlan(intent="no_action",
                                           assistant_message="Okay.")

    def plan_chat(self, text):
        self.chat_calls += 1
        if self.llm_exc:
            raise self.llm_exc
        plan = self.chat_plan or self.llm_plan or ActionPlan(
            intent="no_action", assistant_message="Chat reply.")
        return plan, self.chat_handoff

    def execute(self, plan):
        self.executed.append(plan)
        if self.execute_exc:
            raise self.execute_exc
        return self.execute_result


class FakeRecorder:
    def __init__(self):
        self.recording = False
        self.starts = 0
        self.cancels = 0
        self.observer = None
        self.rolling_seconds = 0.0

    def start(self, on_auto_stop=None, rolling_seconds=0.0):
        self.starts += 1
        self.rolling_seconds = rolling_seconds
        self.recording = True

    def stop(self):
        self.recording = False
        return None

    def cancel(self):
        self.cancels += 1
        self.recording = False

    def set_frame_observer(self, cb):
        self.observer = cb

    def buffered_pcm(self):
        return b""


class FakeMainUI:
    """Quacks like gui.MainWindow for controller-level tests."""

    def __init__(self):
        self.transcript = self
        self.confirm_panel = self
        self.messages = {"user": [], "assistant": [], "info": [], "error": []}
        self.results = []                 # (text, action) result cards
        self.states = []
        self.mic_active = None
        self.wake_switch_on = None
        self.setup_cards = []             # issue lists shown
        self.setup_card_hidden = 0

    def after(self, _ms, fn): fn()
    def add_user(self, t): self.messages["user"].append(t)
    def add_assistant(self, t, name=""): self.messages["assistant"].append(t)
    def add_result(self, t, action):
        self.messages["assistant"].append(t)
        self.results.append((t, action))
    def add_info(self, t): self.messages["info"].append(t)
    def add_error(self, t): self.messages["error"].append(t)
    def set_state(self, s, d=""): self.states.append(s)
    def set_mic_active(self, a): self.mic_active = a
    def set_wake_switch(self, on): self.wake_switch_on = on
    def show_setup_card(self, issues, on_recheck): self.setup_cards.append(list(issues))
    def hide_setup_card(self): self.setup_card_hidden += 1
    def show(self, *a, **k): pass
    def hide(self): pass
    def clear(self): pass
    def destroy(self): pass
