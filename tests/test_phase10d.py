"""Phase 10D: continuous-audio privacy hard gate, cost transparency, idle
auto-close, warm voice + persona. No network; the SDK stays mocked."""

import json
import time

import pytest

from app.llm.providers import (NEVER_CLOUD, DataClass, PrivacyViolation,
                               cloud_allowed, live_audio_allowed)
from app.voice.gemini_live import GeminiLiveSession, affective_dialog_supported
from app.voice.live_cost import add_month_spend, month_spend, session_cost_usd
from app.voice.live_engine import LiveEngine
from tests.fakes import FakeAgent, FakeHistory, FakeRecorder, make_config
from tests.test_phase10c import FakeLiveSession, FakePlayer
from app.agent.engine import EngineSelector

LIVE_KEY = "AIzaTESTKEY123456789"


@pytest.fixture(autouse=True)
def _spend_file_in_tmp(tmp_path, monkeypatch):
    """Never let a test session write the real DATA_DIR spend file."""
    import app.voice.live_cost as live_cost
    monkeypatch.setattr(live_cost, "SPEND_PATH", tmp_path / "live_spend.json")


def live_config(**over):
    over.setdefault("engine_mode", "gemini_live")
    over.setdefault("live_audio_consent", True)
    over.setdefault("gemini_api_key", LIVE_KEY)
    return make_config(**over)


# ---- 10D.1: the hard privacy gate ---------------------------------------------

def test_live_audio_dataclass_is_never_cloud_for_the_brain():
    assert DataClass.LIVE_AUDIO_BIDIRECTIONAL in NEVER_CLOUD
    allowed, reason = cloud_allowed({DataClass.LIVE_AUDIO_BIDIRECTIONAL},
                                    make_config())
    assert not allowed and "never leaves" in reason


def test_live_session_start_hard_gates_on_engine_and_consent():
    # Consent off -> PrivacyViolation, regardless of key/SDK. This is the
    # choke point: NOTHING can stream mic audio around it.
    with pytest.raises(PrivacyViolation):
        GeminiLiveSession(live_config(live_audio_consent=False),
                          on_audio_out=lambda b: None).start()
    # Engine not selected -> PrivacyViolation too (a stray session can't
    # start just because a key + old consent exist).
    with pytest.raises(PrivacyViolation):
        GeminiLiveSession(live_config(engine_mode="pipeline"),
                          on_audio_out=lambda b: None).start()
    ok, why = live_audio_allowed(make_config())
    assert not ok and "engine" in why


# ---- 10D.2: cost transparency ---------------------------------------------------

def test_session_cost_uses_editable_config_prices():
    config = make_config()   # defaults: $0.005 in / $0.018 out per minute
    assert session_cost_usd(60, 60, config) == pytest.approx(0.023)
    config = make_config(live_price_in_per_min=0.01,
                         live_price_out_per_min=0.03)
    assert session_cost_usd(30, 120, config) == pytest.approx(0.005 + 0.06)


def test_month_spend_accumulates_and_resets_next_month(tmp_path):
    path = tmp_path / "live_spend.json"
    assert month_spend(path) == 0.0
    assert add_month_spend(0.05, path) == pytest.approx(0.05)
    assert add_month_spend(0.02, path) == pytest.approx(0.07)
    assert month_spend(path) == pytest.approx(0.07)
    # A file from a previous month resets instead of accumulating forever.
    path.write_text(json.dumps({"month": "1999-01", "usd": 99.0}),
                    encoding="utf-8")
    assert month_spend(path) == 0.0


def make_live_engine(config, **kw):
    agent = FakeAgent(config)
    selector = EngineSelector(config, online_check=lambda: True)
    engine = LiveEngine(config, agent, FakeHistory(), None, selector,
                        session_factory=FakeLiveSession, player=FakePlayer(),
                        **kw)
    assert engine.begin(FakeRecorder())
    return engine


def test_idle_live_session_auto_closes():
    idled = []
    config = live_config(live_idle_close_s=0.3)
    engine = make_live_engine(config, on_idle=lambda: idled.append(1))
    deadline = time.time() + 5
    while not idled and time.time() < deadline:
        time.sleep(0.05)
    assert idled, "idle session was never auto-closed"
    engine.end("idle auto-close")
    assert engine.session.closed


def test_running_cost_reported_while_session_active():
    costs = []
    config = live_config(live_idle_close_s=0.0)   # no idle close in this test

    class TalkativeSession(FakeLiveSession):
        def stats(self):
            return {"audio_in_s": 30.0, "audio_out_s": 60.0}

    agent = FakeAgent(config)
    selector = EngineSelector(config, online_check=lambda: True)
    engine = LiveEngine(config, agent, FakeHistory(), None, selector,
                        session_factory=TalkativeSession, player=FakePlayer(),
                        on_cost=costs.append)
    assert engine.begin(FakeRecorder())
    deadline = time.time() + 5
    while not costs and time.time() < deadline:
        time.sleep(0.05)
    engine.end("test over")
    assert costs, "no cost updates were emitted"
    assert costs[0]["in_s"] == 30.0 and costs[0]["out_s"] == 60.0
    assert costs[0]["usd"] == pytest.approx(30 / 60 * 0.005 + 60 / 60 * 0.018,
                                            abs=1e-6)


def test_monthly_soft_cap_warns_but_never_blocks():
    warnings = []
    config = live_config(live_monthly_cap_usd=0.0001, live_idle_close_s=0.0)

    class TalkativeSession(FakeLiveSession):
        def stats(self):
            return {"audio_in_s": 600.0, "audio_out_s": 600.0}

    agent = FakeAgent(config)
    selector = EngineSelector(config, online_check=lambda: True)
    engine = LiveEngine(config, agent, FakeHistory(), None, selector,
                        session_factory=TalkativeSession, player=FakePlayer(),
                        notify=warnings.append)
    assert engine.begin(FakeRecorder())
    deadline = time.time() + 5
    while not warnings and time.time() < deadline:
        time.sleep(0.05)
    assert warnings and "cap" in warnings[0]
    assert engine.active            # warn only — the session keeps running
    engine.end("test over")


# ---- 10D.3: warm voice + affect + persona -----------------------------------------

def test_default_voice_is_warm_and_old_default_migrates(tmp_path):
    from app.config import AppConfig
    assert AppConfig().gemini_live_voice == "Sulafat"
    # A config saved by 10A with the old default migrates; a custom pick stays.
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gemini_live_voice": "Kore"}), encoding="utf-8")
    assert AppConfig.load(path).gemini_live_voice == "Sulafat"
    path.write_text(json.dumps({"gemini_live_voice": "Leda"}), encoding="utf-8")
    assert AppConfig.load(path).gemini_live_voice == "Leda"


def test_affective_dialog_applied_only_where_supported():
    # Verified 2026-07: v1alpha + 2.5 Live models only; 3.1 rejects the flag.
    assert not affective_dialog_supported("gemini-3.1-flash-live-preview")
    assert affective_dialog_supported("gemini-live-2.5-flash-preview")
    session = GeminiLiveSession(
        live_config(gemini_live_model="gemini-live-2.5-flash-preview",
                    live_affective_dialog=True),
        on_audio_out=lambda b: None)
    config_obj = session._build_config()
    assert getattr(config_obj, "enable_affective_dialog", None) is True
    session = GeminiLiveSession(live_config(), on_audio_out=lambda b: None)
    config_obj = session._build_config()   # 3.1 default model -> no flag
    assert not getattr(config_obj, "enable_affective_dialog", None)
    # the configured HD voice rides along
    voice = (config_obj.speech_config.voice_config
             .prebuilt_voice_config.voice_name)
    assert voice == "Sulafat"


def test_live_persona_includes_safe_memory_only():
    from app.llm.prompt_builder import live_persona_prompt

    class Mem:
        data = {"user_name": "Vaibhav", "preferred_browser": "chrome",
                "private_diary": "SECRET"}
        def get(self, key, default=None):
            return self.data.get(key, default)

    prompt = live_persona_prompt(make_config(), Mem())
    assert "Vaibhav" in prompt and "chrome" in prompt
    assert "SECRET" not in prompt            # private_ keys never in prompts
    assert "approval" in prompt              # tool etiquette for Live
