"""Camera improvements: device selection config, native Gemini Live vision
(the camera frame goes straight into the running session as one image), and
the send_image plumbing."""

import time

from PIL import Image

from app.llm.intent_parser import ActionPlan
from app.tools import ToolResult
from app.vision import VisionFrame
from tests.fakes import FakeAgent
from tests.test_phase10c import live_config, make_live_engine


class _FakeCamera:
    def __init__(self):
        self.captures = 0

    def capture_once(self):
        self.captures += 1
        img = Image.new("RGB", (64, 48), (10, 60, 90))
        return VisionFrame(image=img, source="camera", scope="camera",
                           width=64, height=48)


class _FakeVision:
    def __init__(self):
        self.camera = _FakeCamera()


def _camera_engine(**over):
    opts = {"cloud_vision_consent": True, "live_native_camera": True}
    opts.update(over)
    config = live_config(**opts)
    plan = ActionPlan(intent="camera_look", tool_name="camera_look")
    agent = FakeAgent(config, rule=lambda t: plan,
                      execute_result=ToolResult(True, "described via cloud"))
    agent.vision = _FakeVision()
    engine, agent, shown, recorder, selector = make_live_engine(
        config=config, agent=agent)
    return engine, agent, shown


# ---- send_image plumbing --------------------------------------------------------------

def test_send_image_puts_picture_and_prompt_in_one_turn(monkeypatch):
    """Verified against the real API: sending the frame on the realtime video
    channel and the question as a separate turn makes the model hallucinate
    (it called a red circle "a yellow triangle"). Image + prompt must ride in
    ONE structured turn."""
    from tests.test_phase10a import make_session
    session, fakes, _ = make_session(monkeypatch)
    session.start()
    session.send_image(b"\xff\xd8jpegdata", "What do you see?")
    deadline = time.time() + 2
    while not fakes[0].sent_text and time.time() < deadline:
        time.sleep(0.02)
    assert fakes[0].sent_text, "image never sent to the session"
    turn = fakes[0].sent_text[0]
    parts = turn.parts
    assert parts[0].inline_data.mime_type == "image/jpeg"
    assert parts[0].inline_data.data == b"\xff\xd8jpegdata"
    assert parts[1].text == "What do you see?"      # same turn as the picture
    assert not fakes[0].sent_video                  # NOT the realtime channel
    session.close()


# ---- native Live camera --------------------------------------------------------------

def test_camera_look_sends_frame_natively_into_live_session():
    engine, agent, _ = _camera_engine()
    engine._on_input_transcript("Can you open the camera and tell me what you see?")
    deadline = time.time() + 3
    session = engine.session
    while not getattr(session, "sent_images", None) and time.time() < deadline:
        time.sleep(0.02)
    assert getattr(session, "sent_images", None), "frame not sent natively"
    jpeg, prompt, mime = session.sent_images[0]
    assert mime == "image/jpeg" and jpeg[:2] == b"\xff\xd8"   # real JPEG bytes
    assert agent.vision.camera.captures == 1
    # the prompt rides WITH the picture, and forbids identifying anyone
    assert "camera" in prompt.lower() and "describe" in prompt.lower()
    assert "identify" in prompt.lower() or "name" in prompt.lower()
    # the cloud-describe tool was NOT run — the model sees the image itself
    assert not any(p.tool_name == "camera_look" for p in agent.executed)


def test_native_camera_off_falls_back_to_the_tool():
    # live_native_camera disabled -> the ordinary cloud-describe tool runs
    engine, agent, _ = _camera_engine(live_native_camera=False)
    engine._on_input_transcript("Open the camera and tell me what you see.")
    deadline = time.time() + 2
    while not agent.executed and time.time() < deadline:
        time.sleep(0.02)
    assert any(p.tool_name == "camera_look" for p in agent.executed)
    assert not getattr(engine.session, "sent_images", None)


def test_native_camera_requires_cloud_vision_consent():
    engine, agent, _ = _camera_engine(cloud_vision_consent=False)
    assert not engine._native_camera_ok()          # no consent -> no native send
    engine._on_input_transcript("What do you see through the camera?")
    deadline = time.time() + 2
    while not agent.executed and time.time() < deadline:
        time.sleep(0.02)
    # falls back to the tool (which then says it needs cloud vision)
    assert any(p.tool_name == "camera_look" for p in agent.executed)
    assert not getattr(engine.session, "sent_images", None)


def test_native_camera_failure_tells_the_model():
    from app.vision import CameraUnavailable
    engine, agent, _ = _camera_engine()

    def boom():
        raise CameraUnavailable("lens is covered")
    agent.vision.camera.capture_once = boom

    engine._on_input_transcript("Open the camera please.")
    deadline = time.time() + 2
    while not engine.session.sent_text and time.time() < deadline:
        time.sleep(0.02)
    said = " ".join(engine.session.sent_text).lower()
    assert "camera" in said and ("cover" in said or "couldn't" in said)
    assert not getattr(engine.session, "sent_images", None)


# ---- config -------------------------------------------------------------------------

def test_camera_selection_config_defaults():
    from tests.fakes import make_config
    c = make_config()
    assert c.camera_device == ""            # default camera
    assert c.camera_preview is True         # self-view on by default
    assert c.live_native_camera is True
