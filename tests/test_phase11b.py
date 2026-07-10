"""Phase 11B: on-demand screen + camera vision.

Nothing captures until explicitly triggered; watching is periodic single
frames, never a stream; frames are never logged or kept; cloud vision needs
its own consent; sensitive screens are not analyzed at all until the user
says so; privacy mode kills everything at once.

No real screen, camera or network is touched — every backend is injected.
"""

import time

import pytest

from app.agent.devlog import devlog
from app.agent.router import match_rule
from app.agent.safety import validate_action
from app.llm.providers import (NEVER_CLOUD, DataClass, PrivacyViolation,
                               vision_cloud_allowed)
from app.vision import CameraUnavailable, VisionFrame, VisionResult
from app.vision.camera import CameraSession
from app.vision.sensitive import looks_sensitive
from app.vision.service import VisionService
from app.vision.watcher import ScreenWatcher
from tests.fakes import make_config


class FakeImage:
    """Stands in for a PIL image; records that it was closed."""

    def __init__(self, size=(800, 600)):
        self.size = size
        self.closed = False

    def close(self):
        self.closed = True


def make_frame(source="screen", scope="full", title="Notepad"):
    image = FakeImage()
    return VisionFrame(image=image, source=source, scope=scope,
                       width=image.size[0], height=image.size[1],
                       window_title=title)


def make_service(config=None, ocr_text="", cloud=None, **kw):
    """A service whose capture/OCR/cloud are all injected."""
    config = config or make_config()
    calls = {"capture": 0, "cloud": 0, "frames": []}

    def capture(scope="full", region=None, screen=0):
        calls["capture"] += 1
        frame = make_frame(scope=scope)
        calls["frames"].append(frame)
        return frame

    def fake_cloud(frame, question):
        calls["cloud"] += 1
        return "cloud says: a code editor"

    service = VisionService(config, capture=capture,
                            ocr=lambda frame: ocr_text,
                            cloud=cloud or fake_cloud, **kw)
    return service, calls


# ---- default: nothing is captured ------------------------------------------------

def test_screen_capture_off_by_default():
    """Constructing vision must not capture, watch, or open a camera."""
    service, calls = make_service()
    assert calls["capture"] == 0
    assert not service.watching
    assert not service.camera_active
    assert not service.cloud_enabled()          # cloud vision off by default
    config = make_config()
    assert config.cloud_vision_consent is False
    assert config.vision_save_captures is False


def test_ondemand_capture_only_on_trigger_phrase():
    """Mode A fires only on an explicit trigger; ordinary speech never does."""
    config = make_config()
    for phrase in ("look at my screen", "what's on my screen",
                   "read this error", "summarize this page"):
        plan = match_rule(phrase, config)
        assert plan is not None and plan.tool_name == "look_at_screen", phrase
    assert match_rule("analyze this window", config).tool_name == "active_window_capture"
    assert match_rule("what do you see", config).tool_name == "camera_look"

    for phrase in ("how are you", "open notepad", "what's the weather",
                   "tell me a joke", "screen"):
        plan = match_rule(phrase, config)
        tool = plan.tool_name if plan else ""
        assert tool not in ("look_at_screen", "camera_look",
                            "active_window_capture", "start_screen_watch"), phrase

    # ...and the tool itself captures exactly one frame per call.
    service, calls = make_service(ocr_text="hello")
    service.look()
    assert calls["capture"] == 1


# ---- watching mode (Mode B) --------------------------------------------------------

def test_watching_mode_is_periodic_frames_not_continuous_stream():
    """Each tick opens an independent capture and drops the pixels — there is
    no persistent stream handle and no frame survives its tick."""
    grabbed, images = [], []

    def capture():
        frame = make_frame()
        grabbed.append(frame)
        images.append(frame.image)     # keep a handle to prove it got closed
        return frame

    watcher = ScreenWatcher(make_config(), on_frame=lambda f: None,
                            capture=capture, interval_s=0.05,
                            idle_timeout_s=60)
    watcher.start()
    time.sleep(0.28)
    watcher.stop("test")

    assert watcher.frames_captured >= 3           # periodic, not one-shot
    # one capture call per frame: a stream would open ONCE and read many times
    assert len(grabbed) == watcher.frames_captured
    assert all(f.image is None for f in grabbed)  # no frame outlives its tick
    assert all(img.closed for img in images)      # pixels actually freed
    assert not watcher.watching


def test_watching_mode_shows_persistent_badge():
    events = []
    watcher = ScreenWatcher(make_config(), on_frame=lambda f: None,
                            capture=make_frame, interval_s=0.05,
                            idle_timeout_s=60,
                            on_indicator=events.append)
    watcher.start()
    assert events == [True]                       # badge on for the whole life
    time.sleep(0.15)
    assert watcher.watching and events == [True]  # still on, not flickering
    watcher.stop("test")
    assert events == [True, False]                # off exactly once, at the end


def test_watching_mode_idle_timeout_stops_capture():
    events = []
    watcher = ScreenWatcher(make_config(), on_frame=lambda f: None,
                            capture=make_frame, interval_s=0.02,
                            idle_timeout_s=0.1, on_indicator=events.append)
    watcher.start()
    deadline = time.time() + 3
    while watcher.watching and time.time() < deadline:
        time.sleep(0.02)
    assert not watcher.watching                   # stopped itself
    assert events[-1] is False                    # badge cleared
    frames_at_stop = watcher.frames_captured
    time.sleep(0.15)
    assert watcher.frames_captured == frames_at_stop   # really stopped


# ---- camera ---------------------------------------------------------------------------

class FakeCameraStream:
    """Counts reads and stops, like a real getUserMedia track would."""

    def __init__(self, log):
        self.log = log
        log["opened"] += 1

    def read(self):
        self.log["reads"] += 1
        return FakeImage((640, 480))

    def stop(self):
        self.log["stops"] += 1


def test_camera_never_autostarts():
    log = {"opened": 0, "reads": 0, "stops": 0}
    session = CameraSession(make_config(), opener=lambda: FakeCameraStream(log))
    assert not session.active and log["opened"] == 0
    # ...and building the whole service doesn't touch it either
    service, _calls = make_service()
    assert not service.camera_active
    # no opener wired at all -> an explicit, honest failure, never a silent open
    with pytest.raises(CameraUnavailable):
        CameraSession(make_config()).capture_once()


def test_camera_single_frame_stream_stops_immediately_after():
    log = {"opened": 0, "reads": 0, "stops": 0}
    session = CameraSession(make_config(), opener=lambda: FakeCameraStream(log))
    frame = session.capture_once()
    assert log == {"opened": 1, "reads": 1, "stops": 1}   # exactly one frame
    assert frame.source == "camera" and frame.width == 640
    assert not session.active
    # a failure mid-read still stops the stream (camera light never sticks on)
    class Boom(FakeCameraStream):
        def read(self):
            raise RuntimeError("lens cap")
    log2 = {"opened": 0, "reads": 0, "stops": 0}
    session2 = CameraSession(make_config(), opener=lambda: Boom(log2))
    with pytest.raises(RuntimeError):
        session2.capture_once()
    assert log2["stops"] == 1 and not session2.active


def test_camera_indicator_active_only_during_capture():
    log = {"opened": 0, "reads": 0, "stops": 0}
    events = []
    seen_during = {}

    class WatchingStream(FakeCameraStream):
        def read(self):
            seen_during["active"] = session.active   # red light is ON right now
            return super().read()

    session = CameraSession(make_config(),
                            opener=lambda: WatchingStream(log),
                            on_indicator=events.append)
    assert events == []                        # nothing before
    session.capture_once()
    assert seen_during["active"] is True       # on for exactly the capture
    assert events == [True, False]
    assert not session.active                  # off immediately after


# ---- cloud vision consent ---------------------------------------------------------------

def test_cloud_vision_requires_explicit_consent_toggle():
    from app.vision import cloud as cloud_mod
    frame = make_frame()

    allowed, reason = vision_cloud_allowed(make_config())
    assert not allowed and "consent" in reason
    with pytest.raises(PrivacyViolation):
        cloud_mod.describe_frame(frame, "", make_config())

    # A look that *asks* for cloud stays fully local while consent is off.
    service, calls = make_service(ocr_text="hello world")
    result = service.look(allow_cloud=True)
    assert calls["cloud"] == 0 and not result.used_cloud
    assert "hello world" in result.summary

    # With consent it goes to the cloud describer.
    service, calls = make_service(config=make_config(cloud_vision_consent=True),
                                  ocr_text="hello world")
    result = service.look(allow_cloud=True)
    assert calls["cloud"] == 1 and result.used_cloud
    assert result.summary == "cloud says: a code editor"

    # Frames stay never-cloud for the TEXT brain regardless of the toggle.
    assert DataClass.SCREENSHOT in NEVER_CLOUD
    assert DataClass.CAMERA in NEVER_CLOUD


# ---- sensitive content ---------------------------------------------------------------------

def test_sensitive_content_heuristic_asks_before_analyzing():
    sensitive, reason = looks_sensitive("Enter your password below")
    assert sensitive and "password" in reason

    # Consent ON, but the frame looks sensitive: analyze NOTHING, ask first.
    service, calls = make_service(
        config=make_config(cloud_vision_consent=True),
        ocr_text="Online banking\nAccount number: 1234\nCVV")
    result = service.look(allow_cloud=True)
    assert result.needs_ack and not result.summary
    assert calls["cloud"] == 0                 # never left the machine
    assert result.ack_reason

    # Overriding it is a high-risk action the validator forces to confirm.
    plan = match_rule("look at my screen anyway", make_config())
    assert plan.tool_name == "look_at_screen"
    assert plan.arguments == {"allow_sensitive": True}
    safety = validate_action(plan, make_config())
    assert safety.allowed and safety.requires_confirmation
    assert safety.risk_level == "high"

    # ...and once acknowledged, the look proceeds.
    result = service.look(allow_cloud=True, allow_sensitive=True)
    assert not result.needs_ack and result.summary


def test_password_shaped_text_is_treated_as_sensitive():
    assert looks_sensitive("••••••••")[0]
    assert looks_sensitive("sk-abcdefghijklmnopqrstuvwxyz123")[0]
    assert not looks_sensitive("Just a normal sentence about cats.")[0]


# ---- privacy mode ------------------------------------------------------------------------------

def test_privacy_mode_stops_screen_and_camera_and_live_session():
    live = {"stopped": False}

    def stop_live():
        live["stopped"] = True
        return True

    service, _calls = make_service(stop_live=stop_live)
    log = {"opened": 0, "reads": 0, "stops": 0}
    service.camera = CameraSession(service.config,
                                   opener=lambda: FakeCameraStream(log))
    service.camera._active = True                 # pretend it's stuck on
    service.start_watching()
    assert service.watching

    stopped = service.privacy_mode()

    assert stopped == {"screen": True, "camera": True, "live": True}
    assert not service.watching
    assert not service.camera_active
    assert live["stopped"] is True


# ---- raw frames never logged ----------------------------------------------------------------------

def test_raw_frames_not_logged_by_default():
    entries = []
    devlog.subscribe(lambda e: entries.append(e.get("message", "")))
    service, calls = make_service(ocr_text="some text on screen")
    result = service.look()

    assert result.saved_path is None                 # nothing written to disk
    frame = calls["frames"][0]
    assert frame.image is None                       # pixels released
    joined = " ".join(entries)
    for banned in ("data:image", "base64", "FakeImage object at"):
        assert banned not in joined
    # the describe() line is safe: dimensions and scope only
    assert "800x600" in make_frame().describe()


def test_saving_a_capture_is_opt_in(tmp_path):
    saved = []
    service, _calls = make_service(
        config=make_config(screenshot_dir=str(tmp_path)))
    service._save = lambda frame: saved.append(frame) or "C:/x.png"
    assert service.look().saved_path is None         # default: not saved
    assert service.look(save=True).saved_path == "C:/x.png"


def test_screen_words_never_route_to_the_camera():
    """From real logs: "What do you see on the screen one?" opened the WEBCAM.
    Naming the screen must always beat the bare camera phrase."""
    config = make_config()
    for phrase in ("what do you see on the screen one",
                   "what do you see on screen one",
                   "what do you see on my screen",
                   "what do you see in this window"):
        plan = match_rule(phrase, config)
        assert plan is not None, phrase
        assert plan.tool_name != "camera_look", f"{phrase!r} opened the camera"

    assert match_rule("what do you see on screen two", config).arguments["screen"] == 2
    # ...while genuine camera requests still reach the camera.
    for phrase in ("what do you see", "look through the camera",
                   "what do you see through the webcam"):
        assert match_rule(phrase, config).tool_name == "camera_look", phrase


def test_blank_camera_frame_is_rejected_not_described():
    """From real logs: two saved 1280x720 camera frames were PURE BLACK
    (mean=0, one unique value). A cold sensor must fail honestly."""
    from PIL import Image
    from app.vision.camera import is_blank

    assert is_blank(Image.new("RGB", (64, 48), "black"))
    assert is_blank(Image.new("RGB", (64, 48), "white"))
    real = Image.new("RGB", (64, 48), "black")
    real.putpixel((10, 10), (200, 30, 30))
    assert not is_blank(real)

    class BlackStream:
        def __init__(self, log):
            self.log = log
        def read(self):
            return Image.new("RGB", (1280, 720), "black")
        def stop(self):
            self.log["stops"] += 1

    log = {"stops": 0}
    session = CameraSession(make_config(), opener=lambda: BlackStream(log))
    with pytest.raises(CameraUnavailable) as excinfo:
        session.capture_once(attempts=1)   # single attempt: no retry here
    assert "blank" in str(excinfo.value).lower()
    assert log["stops"] == 1              # the camera still gets turned off
    assert not session.active


def test_ocr_finds_tesseract_when_winget_left_it_off_path(monkeypatch, tmp_path):
    """winget installs Tesseract to Program Files but does NOT add it to PATH,
    so shutil.which() missed a perfectly good install."""
    from app.vision import ocr

    exe = tmp_path / "tesseract.exe"
    exe.write_text("")
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr, "_KNOWN_PATHS", (str(exe),))
    assert ocr._tesseract_exe(make_config()) == str(exe)
    # an explicitly configured path still wins, and a bogus one is ignored
    assert ocr._tesseract_exe(make_config(tesseract_exe=str(exe))) == str(exe)
    assert ocr._tesseract_exe(make_config(tesseract_exe="C:/nope.exe")) == str(exe)


def test_full_screen_summary_is_not_labelled_with_the_focused_window():
    """It read "On Anastasia (Anna) I can read…" while grabbing the whole
    desktop — the focused window title is not what we looked at."""
    service, _calls = make_service(ocr_text="build failed")
    frame = make_frame(scope="full", title="Anastasia (Anna)")
    summary = service._local_summary(frame, "build failed")
    assert "Anastasia (Anna)" not in summary
    assert "your screen" in summary
    # a window-scoped grab still names the window
    window = make_frame(scope="window", title="Visual Studio Code")
    assert "Visual Studio Code" in service._local_summary(window, "build failed")


def test_cloud_vision_falls_back_across_models_but_never_past_consent(monkeypatch):
    """Preview models 404/503 without warning, so one retry on another model
    turns an outage into a blip. The consent gate is checked BEFORE any of it."""
    from app.vision import cloud as cloud_mod
    tried = []

    class FakeModels:
        def generate_content(self, model, contents):
            tried.append(model)
            if len(tried) == 1:
                raise RuntimeError("503 UNAVAILABLE")
            return type("R", (), {"text": "a build error dialog"})()

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr("google.genai.Client", FakeClient)
    config = make_config(cloud_vision_consent=True, gemini_api_key="AIzaTEST123456")
    text = cloud_mod.describe_frame(make_frame(), "", config)
    assert text == "a build error dialog"
    assert len(tried) == 2 and tried[0] == config.vision_cloud_model

    # No consent -> refused before a single model is contacted.
    tried.clear()
    with pytest.raises(PrivacyViolation):
        cloud_mod.describe_frame(make_frame(), "", make_config())
    assert tried == []


def test_downscale_uses_area_budget_not_long_edge():
    """A dual 2560x1440 desktop is 5120px wide. A long-edge cap would leave
    ~1280x360 — unreadable. The area budget keeps it legible."""
    from PIL import Image
    from app.vision.screen import _downscale

    wide = _downscale(Image.new("RGB", (5120, 1440)), 1_600_000, 2600)
    assert wide.size[1] > 600, "dual-monitor grab squashed too short to OCR"
    assert wide.size[0] * wide.size[1] <= 1_600_000 * 1.02
    assert abs(wide.size[0] / wide.size[1] - 5120 / 1440) < 0.05   # aspect kept

    # small frames are never upscaled or touched
    assert _downscale(Image.new("RGB", (800, 600)), 1_600_000, 2600).size == (800, 600)
    # the long edge is still a hard backstop
    assert max(_downscale(Image.new("RGB", (20000, 40)), 1_600_000, 2600).size) <= 2600


def test_vision_frame_release_drops_pixels():
    frame = make_frame()
    image = frame.image
    frame.release()
    assert frame.image is None and image.closed
