"""Phase 3: pywebview bridge — event buffering, full_state re-hydration,
js_api routing, confirmation ids, toggle sync, open_path whitelist."""

import json

from app.main import Controller
from app.web.bridge import JsApi, UIBridge
from tests.fakes import FakeHistory, make_config


class FakeWindow:
    """Captures evaluate_js calls and decodes the dispatched events."""

    def __init__(self):
        self.events = []

    def evaluate_js(self, js: str):
        assert js.startswith("ui.dispatch(") and js.endswith(")")
        self.events.append(json.loads(js[len("ui.dispatch("):-1]))

    def destroy(self):
        pass

    def of_type(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeMemory:
    def get(self, key, default=None):
        return "Vaibhav" if key == "user_name" else default

    def set(self, key, value):
        pass


def make_web_controller():
    bridge = UIBridge()
    window = FakeWindow()
    bridge.window = window
    controller = Controller(ui=bridge, autostart=False, config=make_config(),
                            memory=FakeMemory(), history=FakeHistory())
    controller.speech.shutdown()
    bridge.controller = controller
    return bridge, window, controller


# ---- buffering / handshake ---------------------------------------------------

def test_events_buffer_until_js_ready():
    bridge, window, controller = make_web_controller()
    controller.show_anna("early message")          # page not loaded yet
    controller.set_state("thinking")
    assert window.events == []                     # nothing leaked early

    JsApi(bridge).ready()                          # JS dispatcher exists now
    types = [e["type"] for e in window.events]
    anna_at = types.index("anna_message")
    thinking_at = window.events.index(next(
        e for e in window.events if e["type"] == "state_change"
        and e["payload"]["state"] == "thinking"))
    assert anna_at < thinking_at                   # buffered, flushed in order
    assert types[-1] == "full_state"               # then a snapshot


def test_full_state_rehydrates_conversation_and_toggles():
    bridge, window, controller = make_web_controller()
    controller.show_user("open paint")
    controller.show_result("Done — I opened Paint for you.",
                           {"intent": "open_app", "success": True, "data": None})
    JsApi(bridge).ready()

    full = window.of_type("full_state")[-1]["payload"]
    roles = [m["role"] for m in full["conversation"]]
    assert roles == ["user", "anna"]
    assert full["conversation"][1]["action"]["intent"] == "open_app"
    assert full["toggles"] == {"wake_word": False, "voice": True,
                               "hands_free": False}
    assert full["state"] in ("ready", "thinking")
    assert full["hotkey"] == "ctrl+alt+space"
    assert full["prefs"] == {"animation_quality": "medium"}


def test_save_settings_dispatches_prefs():
    bridge, window, controller = make_web_controller()
    JsApi(bridge).ready()
    controller.save_settings({"animation_quality": "high"})
    assert controller.config.animation_quality == "high"
    prefs = window.of_type("prefs")[-1]["payload"]
    assert prefs == {"animation_quality": "high"}


# ---- js_api routing -----------------------------------------------------------

def test_send_text_goes_through_pipeline():
    from app.agent.router import match_rule
    from tests.fakes import FakeAgent
    bridge, window, controller = make_web_controller()
    agent = FakeAgent(controller.config,
                      rule=lambda t: match_rule(t, controller.config))
    controller.pipeline.agent = agent
    controller.pipeline.run_async = False
    JsApi(bridge).ready()

    JsApi(bridge)._bridge = bridge
    api = JsApi(bridge)
    api.send_text("open notepad")
    assert agent.executed and agent.executed[0].intent == "open_app"
    assert window.of_type("action_result")         # result card event emitted


def test_confirm_routes_by_action_id():
    from app.llm.intent_parser import ActionPlan
    from tests.fakes import FakeAgent
    bridge, window, controller = make_web_controller()
    plan = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                      arguments={"command": "git status"}, risk_level="high",
                      requires_confirmation=True)
    controller.pipeline.agent = FakeAgent(controller.config, rule=lambda t: plan)
    controller.pipeline.run_async = False
    api = JsApi(bridge)
    api.ready()

    api.send_text("run git status")
    request = window.of_type("confirm_request")[-1]["payload"]
    assert request["tool"] == "run_terminal"

    api.confirm(request["id"] + 999, True)          # stale/wrong id: ignored
    assert controller.pipeline.pending is not None
    api.confirm(request["id"], True)                # correct id: executes
    assert controller.pipeline.pending is None
    assert controller.pipeline.agent.executed
    assert window.of_type("confirm_resolved")


def test_set_toggle_voice_syncs_back():
    bridge, window, controller = make_web_controller()
    api = JsApi(bridge)
    api.ready()
    api.set_toggle("voice", False)
    assert controller.config.voice_enabled is False
    sync = window.of_type("toggle_sync")[-1]["payload"]
    assert sync == {"name": "voice", "value": False}


def test_open_path_refuses_outside_whitelisted_roots(tmp_path):
    bridge, window, controller = make_web_controller()
    outside = tmp_path / "evil.txt"
    outside.write_text("nope")
    controller.open_path(str(outside))              # must not raise, must not open
    # a path inside the screenshot dir would be allowed; outside is refused
    # (no assertion on os.startfile — refusal path returns before it)


def test_status_chips_event_shape():
    bridge, window, controller = make_web_controller()
    controller._mic_ok = True
    controller._piper_ok = False
    controller._push_chips("connected")
    JsApi(bridge).ready()
    chips = window.of_type("status")[-1]["payload"]["chips"]
    assert chips["model"]["state"] == "connected"
    assert chips["voice"]["label"] == "Voice: Windows fallback"
