"""Phase 9.1B: streaming STT honesty — missing dependency is surfaced (amber
chip, one warning), never a silent per-turn fallback, and the turn_latency
breakdown reports the REAL measured stt_ms, never an estimate."""

from app.agent.devlog import devlog
from app.voice.stt_providers import STTRouter
from tests.fakes import FakeHistory, make_config


def streaming_config(**over):
    over.setdefault("stt_mode", "streaming")
    over.setdefault("deepgram_api_key", "dg_TESTKEY1234567890")
    return make_config(**over)


# ---- router-level honesty ----------------------------------------------------

def test_missing_websocket_dep_reported_not_silent(monkeypatch):
    router = STTRouter(streaming_config())
    monkeypatch.setattr(STTRouter, "websocket_available", staticmethod(lambda: False))
    state, reason = router.streaming_status()
    assert state == "unavailable"
    assert "websocket-client" in reason
    assert not router.use_streaming()   # won't even attempt -> no per-turn warning


def test_missing_key_reported():
    router = STTRouter(make_config(stt_mode="streaming", deepgram_api_key=""))
    state, reason = router.streaming_status()
    assert state == "unavailable" and "Deepgram API key" in reason


def test_streaming_active_when_dep_and_key_present(monkeypatch):
    router = STTRouter(streaming_config())
    monkeypatch.setattr(STTRouter, "websocket_available", staticmethod(lambda: True))
    state, _ = router.streaming_status()
    assert state == "streaming" and router.use_streaming()


def test_local_mode_no_stt_chip():
    router = STTRouter(make_config(stt_mode="local"))
    state, _ = router.streaming_status()
    assert state == "local"   # controller shows NO stt chip in this case


# ---- controller-level chip + warn-once + capability line ---------------------

def make_web_controller(monkeypatch, ws_available, **cfg):
    from app.main import Controller
    from app.web.bridge import JsApi, UIBridge
    from tests.test_bridge import FakeMemory, FakeWindow
    monkeypatch.setattr(STTRouter, "websocket_available",
                        staticmethod(lambda: ws_available))
    # keep health checks offline/fast
    monkeypatch.setattr("app.voice.stt_whisper.backend_ready",
                        lambda c: (True, "faster-whisper ready."))
    monkeypatch.setattr("app.main.microphone_available", lambda: True)
    monkeypatch.setattr("app.voice.tts_piper.piper_available", lambda c: False)
    bridge = UIBridge(); window = FakeWindow(); bridge.window = window
    controller = Controller(ui=bridge, autostart=False,
                            config=streaming_config(**cfg),
                            memory=FakeMemory(), history=FakeHistory())
    controller.speech.shutdown()
    # no real Ollama/Groq round-trips in these health-check tests
    controller.agent.llm.is_available = lambda: False
    controller.agent.brain.mode = lambda: "hybrid"
    bridge.controller = controller
    JsApi(bridge).ready()
    return controller, window


def test_missing_websocket_dep_sets_amber_chip_not_silent(monkeypatch):
    devlog.clear(); devlog.echo_to_stdout = False
    try:
        controller, window = make_web_controller(monkeypatch, ws_available=False)
        controller.run_health_checks()
        chips = window.of_type("status")[-1]["payload"]["chips"]
        assert chips["stt"] == {"label": "STT: Local (streaming unavailable)",
                                "state": "warn"}
    finally:
        devlog.echo_to_stdout = True


def test_streaming_unavailable_warns_once_not_per_turn(monkeypatch):
    devlog.clear(); devlog.echo_to_stdout = False
    try:
        controller, _ = make_web_controller(monkeypatch, ws_available=False)
        controller.run_health_checks()
        controller.run_health_checks()   # a recheck / second turn
        controller.run_health_checks()
        warns = [e for e in devlog.entries(200)
                 if "Streaming STT not active" in e["message"]]
        assert len(warns) == 1           # once per session, never per turn
    finally:
        devlog.echo_to_stdout = True


def test_capability_summary_logged_at_startup(monkeypatch):
    devlog.clear(); devlog.echo_to_stdout = False
    try:
        controller, _ = make_web_controller(monkeypatch, ws_available=True)
        controller.run_health_checks()
        caps = [e for e in devlog.entries(200) if e["message"].startswith("Capabilities —")]
        assert caps
        assert "STT: streaming (deepgram)" in caps[-1]["message"]
    finally:
        devlog.echo_to_stdout = True


def test_capability_summary_says_local_when_unavailable(monkeypatch):
    devlog.clear(); devlog.echo_to_stdout = False
    try:
        controller, _ = make_web_controller(monkeypatch, ws_available=False)
        controller.run_health_checks()
        caps = [e for e in devlog.entries(200) if e["message"].startswith("Capabilities —")]
        assert "STT: local whisper" in caps[-1]["message"]
    finally:
        devlog.echo_to_stdout = True


# ---- telemetry honesty -------------------------------------------------------

def test_turn_latency_breakdown_uses_real_stt_ms(monkeypatch):
    devlog.clear(); devlog.echo_to_stdout = False
    try:
        controller, window = make_web_controller(monkeypatch, ws_available=True)
        # a local Whisper turn with a REAL 3200ms stt
        controller._start_turn_clock(3200.0, provider="local")
        controller._on_first_audio(250.0)
        line = [e for e in devlog.entries(50) if "turn_latency_ms" in e["message"]][-1]
        assert "stt(local)~3200" in line["message"]
        assert "~300" not in line["message"]   # no aspirational estimate

        # a real Deepgram turn
        controller._start_turn_clock(310.0, provider="deepgram")
        controller._on_first_audio(240.0)
        line = [e for e in devlog.entries(50) if "turn_latency_ms" in e["message"]][-1]
        assert "stt(deepgram)~310" in line["message"]
    finally:
        devlog.echo_to_stdout = True
