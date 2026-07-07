"""Fixes from real-usage logs: SpeechConfidence crash in the hands-free garble
guard, Deepgram close/error de-duplication, and multi-screen screenshots."""

from app.agent.router import match_rule
from app.main import _confidence_is_low
from app.voice.stt_providers import STTResult
from app.voice.stt_whisper import SpeechConfidence
from tests.fakes import make_config

CFG = make_config()


# ---- crash fix: type-safe confidence ----------------------------------------

def test_confidence_is_low_handles_float():
    assert _confidence_is_low(0.2) is True
    assert _confidence_is_low(0.9) is False
    assert _confidence_is_low(None) is False


def test_confidence_is_low_handles_speech_confidence_object():
    # the exact crash: a SpeechConfidence compared to a float
    good = SpeechConfidence(avg_logprob=-0.2, no_speech_prob=0.05,
                            compression_ratio=1.8)
    bad = SpeechConfidence(avg_logprob=-2.0, no_speech_prob=0.95,
                           compression_ratio=3.0)
    assert _confidence_is_low(good) is False
    assert _confidence_is_low(bad) is True   # never raises TypeError


def test_hands_free_final_with_whisper_confidence_no_crash():
    from app.main import Controller
    from tests.fakes import FakeHistory, FakeMainUI

    class _Mem:
        def get(self, k, d=None): return "V" if k == "user_name" else d
        def set(self, k, v): pass
    controller = Controller(ui=FakeMainUI(), autostart=False, config=make_config(),
                            memory=_Mem(), history=FakeHistory())
    controller.speech.shutdown()
    controller._hands_free_active = True
    controller._hands_free_continue = lambda: None
    conf = SpeechConfidence(avg_logprob=-2.5, no_speech_prob=0.99)
    # this line previously raised TypeError and killed the STT thread
    handled = controller._hands_free_handle_final("uhh", conf)
    assert handled is True   # treated as garble, kept listening, no crash


# ---- Deepgram close/error de-duplication ------------------------------------

def make_stream():
    from app.voice.stt_providers import DeepgramSTT
    provider = DeepgramSTT(make_config(stt_mode="streaming",
                                       deepgram_api_key="dg_TESTKEY1234567890"))
    errors = []
    provider._connect = lambda s: setattr(s, "ws", object()) or s.ws
    stream = provider.start_stream(lambda t, e: None, lambda r: None, errors.append)
    return stream, errors


def test_deepgram_reports_only_one_error_per_stream():
    stream, errors = make_stream()
    stream._handle_error("socket is already closed.")
    stream._handle_error("socket is already closed.")
    stream._handle_error("fin=1 opcode=8 data=b'\\x03\\xe8'")
    assert len(errors) == 1          # de-duplicated, no spam


def test_deepgram_close_after_final_is_not_an_error():
    stream, errors = make_stream()
    stream._final_sent = True        # we already got the transcript
    stream._handle_error("fin=1 opcode=8 data=b'\\x03\\xe8'")  # clean close
    assert errors == []              # not counted against the circuit


def test_send_audio_after_error_is_silent():
    stream, errors = make_stream()

    class DeadWS:
        def send(self, *a, **k): raise OSError("socket is already closed.")
    stream.ws = DeadWS()
    stream.send_audio(b"\x00" * 320)   # first failure reports once
    stream.send_audio(b"\x00" * 320)   # subsequent sends are silent
    stream.send_audio(b"\x00" * 320)
    assert len(errors) == 1


# ---- multi-screen screenshots -----------------------------------------------

def test_router_parses_screen_number():
    assert match_rule("take a screenshot", CFG).arguments == {}
    assert match_rule("take a screenshot of screen 2", CFG).arguments == {"screen": 2}
    assert match_rule("screenshot of monitor 1", CFG).arguments == {"screen": 1}
    assert match_rule("grab a screenshot of display 3", CFG).arguments == {"screen": 3}
    assert match_rule("take a screenshot of all screens", CFG).arguments == {"screen": 0}


def test_router_parses_screen_words():
    assert match_rule("take a screenshot of screen two", CFG).arguments == {"screen": 2}
    assert match_rule("screenshot of the primary screen", CFG).arguments == {"screen": 1}


def test_screenshot_captures_specific_monitor(monkeypatch, tmp_path):
    import app.tools.screenshot as shot
    from PIL import Image
    from app.tools import ToolContext

    rects = [(0, 0, 2560, 1440), (2560, 0, 5120, 1440)]
    monkeypatch.setattr(shot, "_monitor_rects", lambda: rects)
    grabbed = {}

    def grab(*a, **k):
        grabbed["bbox"] = k.get("bbox")
        grabbed["all"] = k.get("all_screens")
        box = k.get("bbox") or (0, 0, 5120, 1440)
        return Image.new("RGB", (box[2] - box[0], box[3] - box[1]))
    import PIL.ImageGrab
    monkeypatch.setattr(PIL.ImageGrab, "grab", grab)

    ctx = ToolContext(config=make_config(screenshot_dir=str(tmp_path)))
    result = shot.take_screenshot({"screen": 2}, ctx)
    assert grabbed["bbox"] == (2560, 0, 5120, 1440)   # captured monitor 2
    assert "screen 2" in result.message
    assert result.data["screen"] == 2 and result.data["monitor_count"] == 2


def test_screenshot_nonexistent_screen_grabs_all(monkeypatch, tmp_path):
    import app.tools.screenshot as shot
    from PIL import Image
    from app.tools import ToolContext
    monkeypatch.setattr(shot, "_monitor_rects", lambda: [(0, 0, 2560, 1440),
                                                         (2560, 0, 5120, 1440)])
    monkeypatch.setattr("PIL.ImageGrab.grab",
                        lambda *a, **k: Image.new("RGB", (5120, 1440)))
    ctx = ToolContext(config=make_config(screenshot_dir=str(tmp_path)))
    result = shot.take_screenshot({"screen": 5}, ctx)   # only 2 monitors
    assert "only I see 2" in result.message or "only see 2" in result.message \
        or "captured all" in result.message
    assert result.success
