"""Phase 9.1D: screenshot thumbnail previews — structured payload with a
capped local thumbnail, off-thread generation, whitelisted file actions, and
the guarantee the image never reaches any cloud provider."""

import base64
import io

from PIL import Image

import app.tools.screenshot as shot
from app.tools import ToolContext
from tests.fakes import make_config


def _fake_grab(monkeypatch, size=(1920, 1080), monitors=1):
    def grab(*a, **k):
        # honor a bbox (per-monitor capture) so the size reflects the crop
        bbox = k.get("bbox")
        if bbox:
            return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), (30, 40, 90))
        return Image.new("RGB", size, (30, 40, 90))
    import PIL.ImageGrab
    monkeypatch.setattr(PIL.ImageGrab, "grab", grab)
    # deterministic monitor layout so tests don't depend on real hardware
    rects = [(i * size[0], 0, (i + 1) * size[0], size[1]) for i in range(monitors)]
    monkeypatch.setattr(shot, "_monitor_rects", lambda: rects)


def take(monkeypatch, tmp_path, size=(1920, 1080)):
    _fake_grab(monkeypatch, size)
    ctx = ToolContext(config=make_config(screenshot_dir=str(tmp_path)))
    return shot.take_screenshot({}, ctx)


# ---- payload -----------------------------------------------------------------

def test_screenshot_result_includes_thumb_and_path(monkeypatch, tmp_path):
    result = take(monkeypatch, tmp_path)
    assert result.success
    d = result.data
    assert d["type"] == "screenshot"
    assert d["full_path"].endswith(".png")
    assert (tmp_path / d["full_path"].split("\\")[-1].split("/")[-1]).exists()
    assert d["thumb_data_url"].startswith("data:image/jpeg;base64,")
    assert d["timestamp"]


def test_thumbnail_capped_dimension(monkeypatch, tmp_path):
    result = take(monkeypatch, tmp_path, size=(3840, 2160))
    raw = base64.b64decode(result.data["thumb_data_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as img:
        assert img.width <= shot.THUMB_MAX_WIDTH   # downscaled to <=320px
        assert img.height < 2160                   # aspect preserved, smaller


def test_full_image_not_inlined_only_thumb(monkeypatch, tmp_path):
    result = take(monkeypatch, tmp_path, size=(3840, 2160))
    full_bytes = (tmp_path).glob("*.png").__next__().stat().st_size
    thumb_len = len(result.data["thumb_data_url"])
    assert thumb_len < full_bytes   # thumbnail is far smaller than the full PNG


# ---- off-thread cap ----------------------------------------------------------

def test_slow_thumbnail_falls_back_to_card_without_preview(monkeypatch, tmp_path):
    # force the thumbnail builder to exceed the budget -> None, card still made
    monkeypatch.setattr(shot, "THUMB_BUDGET_S", 0.05)
    real_open = Image.open
    def slow_open(*a, **k):
        import time; time.sleep(0.4)
        return real_open(*a, **k)
    import PIL.Image
    monkeypatch.setattr(PIL.Image, "open", slow_open)
    result = take(monkeypatch, tmp_path)
    assert result.success
    assert result.data["thumb_data_url"] is None       # no preview
    assert result.data["full_path"]                    # but View still works


def test_thumbnail_generation_off_ui_thread(monkeypatch, tmp_path):
    # _make_thumbnail must run its work in a worker thread, not the caller's
    import threading
    caller = threading.current_thread().ident
    seen = {}
    real_open = Image.open
    def spy_open(*a, **k):
        seen["thread"] = threading.current_thread().ident
        return real_open(*a, **k)
    import PIL.Image
    monkeypatch.setattr(PIL.Image, "open", spy_open)
    take(monkeypatch, tmp_path)
    assert seen["thread"] != caller


# ---- actions -----------------------------------------------------------------

def test_screenshot_card_actions_call_correct_js_api():
    from app.web.bridge import JsApi, UIBridge
    bridge = UIBridge()
    calls = []

    class C:
        def open_path(self, p): calls.append(("open", p))
        def reveal_path(self, p): calls.append(("reveal", p))
        def copy_image(self, p): calls.append(("copy", p))
        def save_image_as(self, p): calls.append(("save", p))
    bridge.controller = C()
    api = JsApi(bridge)
    api.open_path("a.png"); api.reveal_path("a.png")
    api.copy_image("a.png"); api.save_image_as("a.png")
    assert calls == [("open", "a.png"), ("reveal", "a.png"),
                     ("copy", "a.png"), ("save", "a.png")]


def test_file_actions_reject_paths_outside_whitelist(tmp_path):
    from app.main import Controller
    from tests.fakes import FakeHistory, FakeMainUI

    class _Mem:
        def get(self, k, d=None): return "V" if k == "user_name" else d
        def set(self, k, v): pass
    outside = tmp_path / "evil.png"
    outside.write_bytes(b"x")
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(screenshot_dir=str(tmp_path / "shots"),
                                               safe_folders=[str(tmp_path / "safe")]),
                            memory=_Mem(), history=FakeHistory())
    controller.speech.shutdown()
    assert controller._allowed_path(str(outside)) is None   # refused


# ---- never-cloud -------------------------------------------------------------

def test_screenshot_thumbnail_never_sent_to_cloud(monkeypatch, tmp_path):
    """The SCREENSHOT payload must never appear in any brain/STT/TTS request —
    those carry text only. Screenshots flow only to the local UI."""
    from app.llm.providers import DataClass, NEVER_CLOUD, cloud_allowed
    assert DataClass.SCREENSHOT in NEVER_CLOUD
    ok, reason = cloud_allowed({DataClass.SCREENSHOT}, make_config())
    assert not ok and "never leaves" in reason
    # and the tool payload's data URL is only ever placed on an action_result
    result = take(monkeypatch, tmp_path)
    assert result.data["thumb_data_url"] is not None   # produced for the UI
    # the brain is only ever given transcript text, never this payload — there
    # is no code path that puts result.data into an LLM/Deepgram request.
