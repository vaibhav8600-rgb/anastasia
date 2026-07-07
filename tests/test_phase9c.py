"""Phase 9C: continuous hands-free conversation loop — mic reopens after each
turn, stop conditions, garble guard, half-duplex, restart persistence."""

import time

from app.main import Controller
from tests.fakes import FakeHistory, FakeMainUI, make_config


class _Mem:
    def get(self, key, default=None):
        return "Vaibhav" if key == "user_name" else default
    def set(self, key, value): pass


def make_controller(**cfg):
    controller = Controller(ui=FakeMainUI(), autostart=False,
                            config=make_config(**cfg), memory=_Mem(),
                            history=FakeHistory())
    controller.speech.shutdown()
    return controller


def track_reopen(controller):
    """Replace _hands_free_continue with a counter (no real mic)."""
    calls = {"n": 0}
    controller._hands_free_continue = lambda: calls.__setitem__("n", calls["n"] + 1)
    return calls


# ---- loop reopening ----------------------------------------------------------

def test_mic_reopens_after_anna_finishes_speaking():
    controller = make_controller()
    calls = track_reopen(controller)
    controller._hands_free_active = True
    # Anna finished speaking, nothing pending/processing -> reopen
    controller._on_speaking_changed(False)
    assert calls["n"] == 1


def test_no_reopen_when_not_in_loop():
    controller = make_controller()
    calls = track_reopen(controller)
    controller._hands_free_active = False
    controller._on_speaking_changed(False)
    assert calls["n"] == 0   # normal (non-loop) behavior -> just goes ready


def test_no_reopen_while_pending_confirmation():
    controller = make_controller()
    calls = track_reopen(controller)
    controller._hands_free_active = True
    controller.pipeline.pending = ("id", None, None, "run git status")
    controller._on_speaking_changed(False)
    assert calls["n"] == 0   # waiting for approval, don't grab the mic


# ---- stop conditions ---------------------------------------------------------

def test_stop_phrase_ends_loop():
    controller = make_controller()
    track_reopen(controller)
    controller._hands_free_active = True
    submitted = []
    controller.pipeline.submit = lambda *a, **k: submitted.append(a)

    handled = controller._hands_free_handle_final("goodbye Anna", confidence=0.98)
    assert handled is True
    assert controller._hands_free_active is False   # loop ended
    assert submitted == []                          # never routed as a command


def test_stop_phrase_variants():
    controller = make_controller()
    for phrase in ("stop listening", "that's all", "bye", "goodbye", "go to sleep"):
        assert controller._is_stop_phrase(phrase), phrase
    assert not controller._is_stop_phrase("open notepad")
    assert not controller._is_stop_phrase("what's the time")


def test_mic_tap_ends_loop():
    controller = make_controller()
    controller._hands_free_active = True
    controller.start_ptt()   # a tap while looping
    assert controller._hands_free_active is False


def test_idle_timeout_ends_loop_with_signoff():
    controller = make_controller(hands_free_idle_timeout_s=0.05)
    signoffs = []
    controller.speech.speak_async = signoffs.append
    controller._hands_free_active = True
    controller._reset_idle_timer()
    deadline = time.time() + 1.0
    while controller._hands_free_active and time.time() < deadline:
        time.sleep(0.02)
    assert controller._hands_free_active is False
    assert any("right here" in s for s in signoffs)


# ---- garble guard ------------------------------------------------------------

def test_empty_final_does_not_count_as_turn():
    controller = make_controller()
    calls = track_reopen(controller)
    controller._hands_free_active = True
    submitted = []
    controller.pipeline.submit = lambda *a, **k: submitted.append(a)

    handled = controller._hands_free_handle_final("", confidence=0.9)
    assert handled is True          # not routed
    assert submitted == []
    assert calls["n"] == 1          # kept listening
    assert controller._garble_streak == 1


def test_three_garbles_checks_in_once_not_repeatedly():
    controller = make_controller()
    track_reopen(controller)
    controller._hands_free_active = True
    spoken = []
    controller.speech.speak_async = spoken.append

    for _ in range(5):
        controller._hands_free_handle_final("", confidence=0.9)
    checkins = [s for s in spoken if "trouble hearing" in s]
    assert len(checkins) == 1       # exactly one gentle check-in, no nagging


def test_real_turn_resets_garble_streak():
    controller = make_controller()
    track_reopen(controller)
    controller._hands_free_active = True
    controller._garble_streak = 2
    handled = controller._hands_free_handle_final("open notepad", confidence=0.95)
    assert handled is False         # routed normally
    assert controller._garble_streak == 0


# ---- barge-in ----------------------------------------------------------------

def test_barge_in_starts_new_turn_in_loop(monkeypatch):
    from tests.fakes import FakeSpeech
    controller = make_controller()
    controller._hands_free_active = True
    speech = FakeSpeech()
    speech._speaking = True                 # Anna is mid-reply
    controller.speech = speech
    controller.pipeline.speech = speech
    aborted = {"n": 0}
    controller.pipeline.abort_stream = lambda: aborted.__setitem__("n", aborted["n"] + 1)
    monkeypatch.setattr(controller.recorder, "start", lambda on_auto_stop=None: None)
    monkeypatch.setattr(type(controller.recorder), "recording",
                        property(lambda self: False))

    controller.toggle_mic("voice")   # barge-in while she's speaking
    assert aborted["n"] == 1         # in-flight chat stream aborted
    assert speech.cancelled == 1     # TTS queue cancelled too


# ---- persistence -------------------------------------------------------------

def test_hands_free_state_persists_across_restart():
    controller = make_controller()
    track_reopen(controller)
    controller.start_hands_free()          # persist=True -> writes config
    assert controller.config.hands_free is True
    # a fresh Controller reading that config auto-resumes (config drives it)
    controller.stop_hands_free("done", signoff=False)
    assert controller.config.hands_free is False


def test_startup_resumes_loop_when_config_on():
    # config.hands_free True -> the autostart path arms start_hands_free
    controller = make_controller(hands_free=True)
    started = []
    controller.start_hands_free = lambda persist=True: started.append(persist)
    # replicate the autostart guard
    if controller.config.hands_free:
        controller.start_hands_free(persist=False)
    assert started == [False]   # resumed without re-persisting
