"""Phase 10C: three-tier engine selector + automatic fallback. Gemini Live
is never the silent default; failures fall back to the pipeline within the
same turn; rules short-circuit locally in every engine; the chip is honest."""

import time

from app.agent.engine import EngineSelector
from app.agent.live_tools import LiveToolBridge
from app.agent.pipeline import CommandPipeline
from app.agent.safety import validate_action
from app.llm.intent_parser import ActionPlan
from app.main import Controller
from app.tools import ToolResult
from app.voice.live_engine import LiveEngine
from app.voice.stt_providers import stt_stream_allowed
from tests.fakes import (FakeAgent, FakeHistory, FakeMainUI, FakePipelineUI,
                         FakeRecorder, FakeSpeech, make_config)

LIVE_KEY = "AIzaTESTKEY123456789"


def live_config(**over):
    over.setdefault("engine_mode", "gemini_live")
    over.setdefault("live_audio_consent", True)
    over.setdefault("gemini_api_key", LIVE_KEY)
    return make_config(**over)


class _Mem:
    def get(self, key, default=None):
        return "Vaibhav" if key == "user_name" else default

    def set(self, key, value):
        pass


def make_controller(**cfg):
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(**cfg), memory=_Mem(),
                            history=FakeHistory())
    controller.speech.shutdown()
    controller.engine_selector._online_check = lambda: True
    return controller


# ---- fakes for the live engine ---------------------------------------------

class FakeLiveSession:
    """Stands in for GeminiLiveSession: records everything, never connects."""

    instances = []

    def __init__(self, config, **callbacks):
        self.config = config
        self.cb = callbacks
        self.closed = []
        self.sent_audio = []
        self.tool_responses = []
        FakeLiveSession.instances.append(self)

    def start(self):
        pass

    def close(self, reason="user"):
        self.closed.append(reason)

    def send_audio(self, pcm):
        self.sent_audio.append(pcm)

    def send_text(self, text):
        pass

    def send_tool_response(self, call_id, name, result):
        self.tool_responses.append((call_id, name, result))

    def stats(self):
        return {"audio_in_s": 0.0, "audio_out_s": 0.0}


class FakePlayer:
    def __init__(self):
        self.chunks = []
        self.stops = 0

    def play_chunk(self, pcm):
        self.chunks.append(pcm)

    def stop(self):
        self.stops += 1


def make_live_engine(config=None, agent=None, on_failure=None):
    config = config or live_config()
    agent = agent or FakeAgent(config)
    selector = EngineSelector(config, online_check=lambda: True)
    pipeline = CommandPipeline(config=config, agent=agent,
                               history=FakeHistory(), ui=FakePipelineUI(),
                               speech=FakeSpeech(), run_async=False)
    shown = {"user": [], "anna": [], "results": []}
    engine = LiveEngine(config, agent, FakeHistory(), pipeline, selector,
                        memory=None,
                        show_user=shown["user"].append,
                        show_anna=shown["anna"].append,
                        show_result=lambda t, a: shown["results"].append((t, a)),
                        on_failure=on_failure or (lambda d: None),
                        session_factory=FakeLiveSession, player=FakePlayer())
    recorder = FakeRecorder()
    assert engine.begin(recorder)
    return engine, agent, shown, recorder, selector


# ---- 10C.1: never the silent default -----------------------------------------

def test_default_engine_is_pipeline_until_live_opted_in():
    # Fresh config: pipeline, no questions asked.
    sel = EngineSelector(make_config(), online_check=lambda: True)
    assert sel.choose() == ("pipeline", "")
    # Selecting gemini_live + adding a key is STILL not enough — the
    # billing/continuous-audio consent is a separate explicit step.
    sel = EngineSelector(live_config(live_audio_consent=False),
                         online_check=lambda: True)
    engine, reason = sel.choose()
    assert engine == "pipeline" and "opt-in" in reason
    # Everything opted in -> premium tier.
    sel = EngineSelector(live_config(), online_check=lambda: True)
    assert sel.choose() == ("gemini_live", "")
    # No key -> honest reason, pipeline.
    sel = EngineSelector(live_config(gemini_api_key=""),
                         online_check=lambda: True)
    engine, reason = sel.choose()
    assert engine == "pipeline" and "key" in reason.lower()


# ---- 10C.2: circuit breaker ----------------------------------------------------

def test_live_circuit_opens_after_3_failures_and_probes():
    sel = EngineSelector(live_config(), online_check=lambda: True)
    sel.record_failure("one")
    sel.record_failure("two")
    assert sel.choose()[0] == "gemini_live"      # still closed after 2
    sel.record_failure("three")
    engine, reason = sel.choose()
    assert engine == "pipeline" and "circuit" in reason.lower()
    # Cooldown elapses -> the next attempt is the probe.
    sel._open_until = time.monotonic() - 1
    assert sel.choose()[0] == "gemini_live"
    # Failed probe -> straight back open (failure count never reset).
    sel.record_failure("probe failed")
    assert sel.choose()[0] == "pipeline"
    # Successful probe closes it for real.
    sel.record_success()
    assert sel.choose() == ("gemini_live", "")
    assert sel.circuit_state() == "closed"


def test_offline_skips_live_entirely():
    sel = EngineSelector(live_config(), online_check=lambda: False)
    engine, reason = sel.choose()
    assert engine == "pipeline" and "offline" in reason
    # Controller level: an offline mic tap starts a normal pipeline
    # recording — no session, no LiveEngine, no cloud attempt.
    controller = make_controller(engine_mode="gemini_live",
                                 live_audio_consent=True,
                                 gemini_api_key=LIVE_KEY)
    controller.engine_selector._online_check = lambda: False
    began = []
    controller._begin_live_conversation = lambda s="voice": began.append(s)
    controller.recorder = FakeRecorder()
    controller.toggle_mic()
    assert began == []                           # Live never attempted
    assert controller.recorder.recording         # the pipeline turn runs
    # WiFi back -> Live is re-eligible immediately (cache expiry aside).
    controller.engine_selector._online_check = lambda: True
    controller.engine_selector._online_cached = (0.0, False)   # drop cache
    assert controller.engine_selector.choose()[0] == "gemini_live"


# ---- 10C.2: same-turn fallback -------------------------------------------------

def test_live_failure_falls_back_to_pipeline_same_turn():
    controller = make_controller(engine_mode="gemini_live",
                                 live_audio_consent=True,
                                 gemini_api_key=LIVE_KEY)
    finished = []
    controller._finish_recording = finished.append
    controller.recorder = FakeRecorder()
    controller.recorder.recording = True         # mic buffer holds the turn

    class _Live:
        ended = []
        def end(self, reason):
            _Live.ended.append(reason)
        def stats(self):
            return {}

    controller._live = _Live()
    controller._on_live_failure("socket dropped mid-turn")
    deadline = time.time() + 3
    while not finished and time.time() < deadline:
        time.sleep(0.02)
    assert finished, "the user's turn was lost — no pipeline recovery"
    assert _Live.ended and "failure" in _Live.ended[0]
    assert controller._live is None
    assert any("pipeline" in m for m in controller.ui.messages["info"])


def test_live_engine_error_records_failure_and_notifies():
    failures = []
    engine, agent, shown, recorder, selector = make_live_engine(
        on_failure=failures.append)
    session = engine.session
    session.cb["on_error"](" quota exceeded ")
    assert failures == [" quota exceeded "]
    assert selector._failures == 1
    assert not engine.active
    # teardown un-tees the mic and closes the socket
    engine.end("failure: quota")
    assert recorder.observer is None
    assert session.closed


# ---- 10C.3: rule short-circuit --------------------------------------------------

def test_rule_command_short_circuits_before_any_engine():
    plan = ActionPlan(intent="open_app", tool_name="open_app",
                      arguments={"app_name": "paint"})
    config = live_config()
    agent = FakeAgent(config,
                      rule=lambda t: plan if "open paint" in t else None,
                      execute_result=ToolResult(True, "Opening Paint."))
    engine, agent, shown, recorder, selector = make_live_engine(
        config=config, agent=agent)
    engine._on_input_transcript("open paint")
    assert agent.executed == [plan]              # instant, local, no cloud
    assert shown["results"] and "Opening Paint." in shown["results"][0][0]
    # The model heard the audio too and echoes a tool call — deduped, not
    # executed twice.
    msg = engine._skip_check("open_app", {"app_name": "paint"})
    assert msg is not None
    assert engine._skip_check("run_terminal", {}) is None   # others unaffected

    # The dedup rides the 10B bridge: respond success, execute nothing.
    responses = []
    bridge = LiveToolBridge(config, agent, FakeHistory(), run_async=False,
                            respond=lambda c, n, r: responses.append(r),
                            skip_check=engine._skip_check)
    bridge.handle_tool_call("open_app", {"app_name": "paint"}, "fc1")
    assert len(agent.executed) == 1              # STILL exactly once
    assert responses and responses[0]["success"] is True


def test_rule_short_circuit_respects_settings_and_confirmation():
    plan = ActionPlan(intent="open_app", tool_name="open_app",
                      arguments={"app_name": "paint"})
    # User disabled it ("always use the conversation engine") -> no local run.
    config = live_config(engine_rules_first=False)
    agent = FakeAgent(config, rule=lambda t: plan)
    engine, agent, *_ = make_live_engine(config=config, agent=agent)
    engine._on_input_transcript("open paint")
    assert agent.executed == []
    # Confirmation-needing rules never short-circuit (one card, one flow —
    # the Live tool-call path owns them).
    confirm_plan = ActionPlan(intent="window_control", tool_name="window_control",
                              arguments={"action": "close", "app": "chrome"})
    config = live_config()
    agent = FakeAgent(config, rule=lambda t: confirm_plan)
    engine, agent, *_ = make_live_engine(config=config, agent=agent)
    engine._on_input_transcript("close chrome for me")
    assert agent.executed == []


# ---- confirmations through the shared card ---------------------------------------

def test_live_tool_confirmation_flows_through_pipeline_card():
    config = make_config()
    ui = FakePipelineUI()
    pipeline = CommandPipeline(config=config, agent=FakeAgent(config),
                               history=FakeHistory(), ui=ui,
                               speech=FakeSpeech(), run_async=False)
    plan = ActionPlan(intent="run_terminal", tool_name="run_terminal",
                      arguments={"command": "echo hi"})
    safety = validate_action(plan, config)
    outcomes = []
    assert pipeline.request_external_confirmation(plan, safety,
                                                  "Gemini Live: run_terminal",
                                                  outcomes.append)
    assert ui.confirmations                       # the card appeared
    pipeline.approve_pending()
    assert outcomes == [True]
    assert pipeline.pending is None
    # Approve must reset the UI, or it strands in the yellow waiting state
    # after the tool runs (user-reported: "close paint" left it stuck).
    assert ui.states[-1] == "ready"
    # Deny path fires exactly once with False; no local TTS double-voice.
    assert pipeline.request_external_confirmation(plan, safety, "x",
                                                  outcomes.append)
    pipeline.cancel_pending()
    assert outcomes == [True, False]
    assert ui.states[-1] == "ready"
    # Busy path: a second request while one is pending is refused (deny).
    assert pipeline.request_external_confirmation(plan, safety, "a",
                                                  outcomes.append)
    assert not pipeline.request_external_confirmation(plan, safety, "b",
                                                      outcomes.append)
    pipeline.cancel_pending()


def test_live_confirmation_leaves_ui_in_listening_not_waiting():
    """End-to-end at the controller: approving a Live tool confirmation must
    return the UI to the Live listening state, never leave it yellow."""
    controller = make_controller(engine_mode="gemini_live",
                                 live_audio_consent=True,
                                 gemini_api_key=LIVE_KEY)
    controller._live = object()                  # a session is up
    plan = ActionPlan(intent="window_control", tool_name="window_control",
                      arguments={"action": "close", "app": "mspaint"})
    safety = validate_action(plan, controller.config)
    outcomes = []
    assert controller.pipeline.request_external_confirmation(
        plan, safety, "Gemini Live: window_control", outcomes.append)
    assert "waiting_confirmation" in controller.ui.states
    controller.pipeline.approve_pending()
    assert outcomes == [True]
    # "ready" is translated to the Live listening state while a session runs.
    assert controller.ui.states[-1] == "listening"
    assert controller.last_state == "listening"
    assert "waiting_confirmation" not in controller.ui.states[-1:]


# ---- 10C.4: the chip is honest ------------------------------------------------------

def test_engine_chip_reflects_state_and_fallback():
    # Default install: pipeline.
    controller = make_controller()
    controller._push_chips("ok")
    assert controller.chips["engine"] == {"label": "Engine: Pipeline",
                                          "state": "ok"}
    # Local floor.
    controller = make_controller(engine_mode="local")
    controller._push_chips("ok")
    assert controller.chips["engine"] == {"label": "Engine: Local",
                                          "state": "local"}
    # Live selected and healthy; pulsing 'live' only while a session runs.
    controller = make_controller(engine_mode="gemini_live",
                                 live_audio_consent=True,
                                 gemini_api_key=LIVE_KEY)
    controller._push_chips("ok")
    assert controller.chips["engine"] == {"label": "Engine: Gemini Live",
                                          "state": "ok"}
    controller._live = object()
    controller._push_chips("ok")
    assert controller.chips["engine"]["state"] == "live"
    # Live selected but unreachable -> honest amber fallback label.
    controller._live = None
    controller.engine_selector._online_check = lambda: False
    controller.engine_selector._online_cached = (0.0, False)
    controller._push_chips("ok")
    assert controller.chips["engine"] == {
        "label": "Engine: Pipeline (Live offline)", "state": "warn"}


# ---- local floor forces on-device paths ----------------------------------------------

def test_local_engine_forces_ondevice_stt_and_brain():
    config = make_config(engine_mode="local", stt_mode="streaming",
                         deepgram_api_key="dg_test", brain_mode="hybrid",
                         groq_api_key="gsk_test")
    allowed, reason = stt_stream_allowed(config)
    assert not allowed and "local engine" in reason
    from app.llm.providers import BrainRouter
    router = BrainRouter(config, ollama_client=None)
    assert router.mode() == "local_only"
