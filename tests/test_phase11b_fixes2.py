"""Second round of 11B/live fixes, from real usage logs:
  * a full-screen look grabs the ACTIVE monitor, not the whole squashed
    virtual desktop (OCR was starved on a dual-2560x1440 rig);
  * the model's habitual extra take_screenshot after a look_at_screen is
    deduped by family;
  * "close the browser" resolves to a concrete browser instead of blocking;
  * browser_open(url=site-root, query=...) searches the site.
"""

from app.agent.safety import validate_action
from app.llm.intent_parser import ActionPlan
from app.tools.browser import build_target
from app.tools.window_control import normalize_window_app
from app.voice.live_engine import _tool_family
from tests.fakes import make_config

CFG = make_config()


# ---- OCR speed: cloud-primary, fast sensitive-scan, skip slow full OCR ----------

def test_cloud_path_skips_the_slow_full_ocr():
    """From logs: full OCR ran FIRST and timed out (12s) before cloud even
    started, so a 'look' took ~20s. With cloud on, only the FAST scan runs."""
    from app.vision.service import VisionService

    calls = {"fast": 0, "full": 0, "cloud": 0}

    def ocr(frame, fast=False):
        calls["fast" if fast else "full"] += 1
        return "just some normal screen text"

    def cloud(frame, q):
        calls["cloud"] += 1
        return "a code editor with several files open"

    def capture(scope="full", region=None, screen=0):
        from tests.test_phase11b import make_frame
        return make_frame()

    svc = VisionService(make_config(cloud_vision_consent=True),
                        capture=capture, ocr=ocr, cloud=cloud)
    result = svc.look()
    assert result.used_cloud
    assert calls["fast"] == 1        # sensitive pre-scan ran (fast)
    assert calls["full"] == 0        # the slow full OCR was SKIPPED
    assert calls["cloud"] == 1


def test_local_only_still_pays_for_full_ocr():
    from app.vision.service import VisionService
    calls = {"fast": 0, "full": 0}

    def ocr(frame, fast=False):
        calls["fast" if fast else "full"] += 1
        return "readable screen text"

    def capture(scope="full", region=None, screen=0):
        from tests.test_phase11b import make_frame
        return make_frame()

    svc = VisionService(make_config(cloud_vision_consent=False),
                        capture=capture, ocr=ocr)
    result = svc.look()
    assert not result.used_cloud
    assert calls["fast"] == 1 and calls["full"] == 1   # cloud off -> full OCR
    assert "readable screen text" in result.summary


def test_ocr_downscales_hard_before_running(monkeypatch):
    """The only lever for OCR speed is resolution, so it must downscale big
    frames aggressively before handing them to Tesseract."""
    import sys
    from types import SimpleNamespace
    from PIL import Image
    from app.vision import ocr as ocr_mod

    sizes = []

    def image_to_string(image, config=None, timeout=None):
        sizes.append(image.size)
        return "text"

    fake_pt = SimpleNamespace(
        image_to_string=image_to_string,
        pytesseract=SimpleNamespace(tesseract_cmd=""))
    monkeypatch.setattr(ocr_mod, "ocr_status", lambda cfg: (True, ""))
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pt)
    monkeypatch.setattr(ocr_mod, "_tesseract_exe", lambda cfg: "")

    frame = SimpleNamespace(image=Image.new("RGB", (2560, 1440)))
    cfg = make_config(ocr_fast_pixels=500_000, ocr_max_pixels=1_100_000)
    ocr_mod.extract_text(frame, cfg, fast=True)
    ocr_mod.extract_text(frame, cfg, fast=False)
    fast_px, full_px = sizes[0][0] * sizes[0][1], sizes[1][0] * sizes[1][1]
    assert fast_px <= 500_000 * 1.05        # fast pass is small
    assert full_px <= 1_100_000 * 1.05      # full pass still capped
    assert fast_px < full_px


# ---- active-monitor capture -----------------------------------------------------

def test_full_look_uses_active_monitor_on_multi_monitor(monkeypatch):
    from app.vision import screen as screen_mod

    two = [(0, 0, 2560, 1440), (2560, 0, 5120, 1440)]   # side-by-side 1440p
    monkeypatch.setattr("app.tools.screenshot._monitor_rects", lambda: two)
    # active window sits on the RIGHT monitor
    monkeypatch.setattr(screen_mod, "active_window_bbox",
                        lambda: (3000, 200, 4000, 900))
    assert screen_mod.active_monitor_rect() == (2560, 0, 5120, 1440)

    grabbed = {}

    class FakeGrab:
        @staticmethod
        def grab(bbox=None, all_screens=True):
            grabbed["bbox"] = bbox
            from PIL import Image
            w = (bbox[2] - bbox[0]) if bbox else 5120
            h = (bbox[3] - bbox[1]) if bbox else 1440
            return Image.new("RGB", (w, h))

    import PIL.ImageGrab
    monkeypatch.setattr(PIL.ImageGrab, "grab", FakeGrab.grab)
    monkeypatch.setattr(screen_mod, "active_window_title", lambda: "Chrome")
    frame = screen_mod.capture(make_config(vision_max_pixels=1_600_000),
                               scope="full")
    # captured ONE monitor (2560x1440), not the 5120-wide desktop
    assert grabbed["bbox"] == (2560, 0, 5120, 1440)
    assert frame.image.size[1] > 600            # legible height, not a letterbox


def test_single_monitor_still_grabs_the_whole_screen(monkeypatch):
    from app.vision import screen as screen_mod
    monkeypatch.setattr("app.tools.screenshot._monitor_rects",
                        lambda: [(0, 0, 1920, 1080)])
    assert screen_mod.active_monitor_rect() is None    # None -> whole (only) screen


# ---- family dedup ----------------------------------------------------------------

def test_screen_snapshot_tools_share_a_dedup_family():
    for tool in ("take_screenshot", "look_at_screen", "screen_capture",
                 "active_window_capture", "region_capture"):
        assert _tool_family(tool) == "screen_snapshot", tool
    # the camera is its own thing; unrelated tools are never merged
    assert _tool_family("camera_look") != "screen_snapshot"
    assert _tool_family("open_app") == "open_app"


def test_live_dedup_skips_redundant_screenshot_after_look():
    from tests.test_phase10c import make_live_engine, live_config
    from tests.fakes import FakeAgent
    from app.llm.intent_parser import ActionPlan
    from app.tools import ToolResult

    plan = ActionPlan(intent="look_at_screen", tool_name="look_at_screen")
    config = live_config()
    agent = FakeAgent(config, rule=lambda t: plan,
                      execute_result=ToolResult(True, "On your screen: a code editor"))
    engine, agent, *_ = make_live_engine(config=config, agent=agent)
    engine._on_input_transcript("what do you see on my screen?")
    assert len(agent.executed) == 1

    # the model, having heard the same audio, ALSO asks for a screenshot
    msg = engine._skip_check("take_screenshot", {})
    assert msg is not None and "captured" in msg.lower()
    # ...but an unrelated tool is not deduped
    assert engine._skip_check("open_app", {"app_name": "paint"}) is None


# ---- "close the browser" --------------------------------------------------------

def test_browser_word_resolves_to_a_concrete_browser():
    config = make_config(default_browser="edge")
    assert normalize_window_app("the browser", config) == "edge"
    assert normalize_window_app("browser", config) == "edge"
    # non-browser names pass through untouched (own-window guard still applies)
    assert normalize_window_app("python", config) == "python"
    assert normalize_window_app("notepad", config) == "notepad"


def test_window_control_maps_code_synonym_to_vscode():
    """From logs: 'minimize code' -> app='code' was blocked because 'code' is
    an alias VALUE, not a key. Synonyms now map to the alias key."""
    config = make_config()
    assert normalize_window_app("code", config) == "vscode"   # value -> key
    # "vs code" is already a valid alias key, so it needs no remapping
    assert "vs code" in {k.lower() for k in config.app_aliases}

    plan = ActionPlan(intent="window_control", tool_name="window_control",
                      arguments={"app": "code", "action": "minimize"})
    result = validate_action(plan, config)
    assert result.allowed and result.requires_confirmation   # no longer blocked


def test_close_browser_is_not_blocked_by_safety():
    # From logs: window_control app='browser' was blocked outright.
    config = make_config(default_browser="chrome")
    plan = ActionPlan(intent="window_control", tool_name="window_control",
                      arguments={"app": "browser", "action": "close"})
    result = validate_action(plan, config)
    assert result.allowed                          # no longer blocked
    assert result.requires_confirmation            # still needs an OK

    # a genuinely unknown app is still refused
    plan = ActionPlan(intent="window_control", tool_name="window_control",
                      arguments={"app": "python", "action": "minimize"})
    assert not validate_action(plan, config).allowed


# ---- browser_open url + query ---------------------------------------------------

def test_browser_open_searches_when_given_site_root_and_query():
    # the exact case from the logs
    target = build_target({"url": "https://www.youtube.com", "query": "funny videos"})
    assert target == "https://www.youtube.com/results?search_query=funny+videos"

    # unknown site root + query -> web search rather than the bare homepage
    target = build_target({"url": "example.com", "query": "cat pictures"})
    assert "google.com/search" in target and "cat+pictures" in target

    # a real url with a path is respected as-is (query ignored)
    target = build_target({"url": "https://youtube.com/watch?v=abc", "query": "x"})
    assert target == "https://youtube.com/watch?v=abc"

    # plain single-arg behaviour is unchanged
    assert build_target({"query": "hello world"}).endswith("q=hello+world")
    assert build_target({"url": "github.com"}) == "https://github.com"
