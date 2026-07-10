"""Third round of fixes from real usage:
  * voice approval works in a Gemini Live conversation (spoken "approve"/
    "cancel" resolve the pending card — before, only clicking worked);
  * a camera frame is described by cloud vision (person/object/scene) and is
    NOT run through screen OCR;
  * a blank first camera frame is retried instead of failing.
"""

import pytest

from app.agent.confirmation_manager import ConfirmationManager, Outcome
from app.agent.safety import validate_action
from app.llm.intent_parser import ActionPlan
from tests.fakes import make_config
from tests.test_phase10c import live_config, make_live_engine


# ---- voice approval in Live mode ------------------------------------------------------

def _pending_confirmation(engine, *, tool="window_control",
                          args=None, strong=False):
    plan = ActionPlan(intent=tool, tool_name=tool,
                      arguments=args or {"action": "minimize", "app": "chrome"})
    safety = validate_action(plan, engine.config)
    outcomes = []
    accepted = engine.pipeline.request_external_confirmation(
        plan, safety, f"Gemini Live: {tool}", outcomes.append)
    assert accepted and engine.pipeline.confirm.has_pending()
    return outcomes


def test_spoken_approve_resolves_a_live_confirmation():
    engine, agent, *_ = make_live_engine()
    outcomes = _pending_confirmation(engine)
    # the words the user speaks are transcribed by Gemini and land here
    engine._on_input_transcript("yeah okay, approve that.")
    assert outcomes == [True]                      # resolved by VOICE, no click
    assert not engine.pipeline.confirm.has_pending()


def test_spoken_cancel_resolves_a_live_confirmation():
    engine, agent, *_ = make_live_engine()
    outcomes = _pending_confirmation(engine)
    engine._on_input_transcript("no, cancel that please.")
    assert outcomes == [False]
    assert not engine.pipeline.confirm.has_pending()


def test_cancel_wins_when_both_words_are_heard():
    engine, *_ = make_live_engine()
    outcomes = _pending_confirmation(engine)
    engine._on_input_transcript("approve — no wait, cancel.")
    assert outcomes == [False]                     # safe direction wins


def test_strong_tier_rejects_casual_yes_by_voice():
    engine, agent, *_ = make_live_engine()
    notes = []
    engine.notify = notes.append
    outcomes = _pending_confirmation(
        engine, tool="run_terminal", args={"command": "git status"})
    assert engine.pipeline.confirm.pending.strong_required
    engine._on_input_transcript("yes, go ahead.")   # casual — insufficient
    assert outcomes == []                           # NOT resolved
    assert engine.pipeline.confirm.has_pending()
    assert any("Anna approve" in n for n in notes)
    # the strong phrase then approves it by voice
    engine._on_input_transcript("okay, Anna approve.")
    assert outcomes == [True]


def test_non_confirmation_speech_flows_to_gemini():
    """Ordinary chatter during a pending card is NOT swallowed — Gemini keeps
    the conversation and the card stays up."""
    engine, *_ = make_live_engine()
    outcomes = _pending_confirmation(engine)
    engine._on_input_transcript("what was I saying earlier?")
    assert outcomes == []
    assert engine.pipeline.confirm.has_pending()


def test_classify_is_pure_no_reprompt_mutation():
    plan = ActionPlan(intent="window_control", tool_name="window_control",
                      arguments={"action": "close", "app": "chrome"})
    mgr = ConfirmationManager(timeout_s=0)
    mgr.request(plan, validate_action(plan, make_config()), "close chrome")
    for _ in range(5):
        mgr.classify("blah blah")                  # pure: no reprompt bump
    assert mgr.reprompts == 0
    assert mgr.handle_utterance("blah") is Outcome.UNCLEAR
    assert mgr.reprompts == 1                       # handle_utterance DOES track


# ---- camera: cloud description, no OCR ------------------------------------------------

def _camera_session(config):
    from PIL import Image
    from app.vision.camera import CameraSession

    class Stream:
        def read(self):
            img = Image.new("RGB", (640, 480))
            img.putpixel((5, 5), (200, 30, 30))    # non-blank
            return img
        def stop(self):
            pass

    return CameraSession(config, opener=Stream)


def test_camera_frame_goes_to_cloud_not_ocr():
    from app.vision.service import VisionService

    calls = {"ocr": 0, "cloud": 0}

    def ocr(frame, fast=False):
        calls["ocr"] += 1
        return "should never OCR a photo"

    def cloud(frame, q):
        calls["cloud"] += 1
        return "a person at a desk in a bright room"

    config = make_config(cloud_vision_consent=True)
    svc = VisionService(config, camera=_camera_session(config),
                        ocr=ocr, cloud=cloud)
    result = svc.camera_look()
    assert calls["ocr"] == 0                        # a photo is never OCR'd
    assert calls["cloud"] == 1
    assert "person" in result.summary and result.used_cloud


def test_camera_without_cloud_asks_to_enable_it():
    from app.vision.service import VisionService
    config = make_config(cloud_vision_consent=False)
    svc = VisionService(config, camera=_camera_session(config),
                        ocr=lambda f, fast=False: "")
    result = svc.camera_look()
    assert not result.used_cloud
    assert "cloud vision" in result.summary.lower()


# ---- camera: retry a blank first frame -----------------------------------------------

def test_camera_retries_a_blank_frame_then_succeeds():
    from PIL import Image
    from app.vision.camera import CameraSession

    frames = [Image.new("RGB", (640, 480), "black"),      # cold sensor
              Image.new("RGB", (640, 480))]
    frames[1].putpixel((10, 10), (200, 40, 40))           # real content
    opened = {"n": 0}

    class Stream:
        def __init__(self):
            self.idx = opened["n"]
            opened["n"] += 1
        def read(self):
            return frames[min(self.idx, len(frames) - 1)]
        def stop(self):
            pass

    session = CameraSession(make_config(), opener=Stream)
    frame = session.capture_once()                 # retries past the black one
    assert opened["n"] == 2                         # two attempts, fresh opens
    assert frame.source == "camera"
    assert not session.active


def test_camera_all_blank_fails_honestly():
    from PIL import Image
    from app.vision import CameraUnavailable
    from app.vision.camera import CameraSession

    class BlackStream:
        def read(self):
            return Image.new("RGB", (640, 480), "black")
        def stop(self):
            pass

    session = CameraSession(make_config(), opener=BlackStream)
    with pytest.raises(CameraUnavailable):
        session.capture_once(attempts=2)
    assert not session.active                       # indicator cleared
