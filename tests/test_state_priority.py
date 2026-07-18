"""The status line must not lie about the mic (found in the Phase-1 sanity pass).

A streaming reply lets the pipeline finish (→ "ready") and, in hands-free,
reopens the mic (→ "listening") BEFORE the TTS queue drains — so the on-screen
status would show "Ready"/"Listening" while Anna is audibly speaking. The new
burstier brain (gpt-oss-120b) made the race visible; the bug is a latent flap in
the shared Controller.set_state, so the fix (display priority speaking >
listening > ready) lives there and covers both the split and legacy.
"""

import types

from app.main import Controller
from tests.fakes import FakeHistory, FakeMainUI, make_config


class _Mem:
    def get(self, k, d=None): return d
    def set(self, k, v): pass


def _controller():
    c = Controller(ui=FakeMainUI(), autostart=False, config=make_config(),
                   memory=_Mem(), history=FakeHistory())
    c.speech.shutdown()
    # Drive the speaking gate directly; set_state only reads speech.speaking.
    c.speech = types.SimpleNamespace(speaking=False)
    c._live = None
    c.ui.states.clear()
    return c


def test_speaking_blocks_a_racing_ready_or_listening():
    c = _controller()
    c.speech.speaking = True
    c.set_state("ready")          # pipeline finished while audio still plays
    c.set_state("listening")      # hands-free reopened the mic mid-reply
    assert c.ui.states == [], "status must not downgrade while she is speaking"


def test_speaking_itself_and_other_states_still_pass_while_speaking():
    c = _controller()
    c.speech.speaking = True
    c.set_state("speaking")
    c.set_state("thinking")       # not a downgrade — genuine progression
    assert c.ui.states == ["speaking", "thinking"]


def test_resting_state_lands_once_speech_ends():
    c = _controller()
    c.speech.speaking = True
    c.set_state("ready")          # blocked
    assert c.ui.states == []
    c.speech.speaking = False     # _on_speaking_changed(False) fires here
    c.set_state("ready")          # now the true resting state passes
    assert c.ui.states == ["ready"]


def test_barge_in_listening_is_not_blocked():
    """Barge-in cancels TTS (clears the gate) THEN opens the mic — so
    'listening' must show. The guard only blocks while the gate is still set."""
    c = _controller()
    c.speech.speaking = False      # cancel() already cleared it
    c.set_state("listening")
    assert c.ui.states == ["listening"]


def test_live_priority_still_applies():
    """The pre-existing Live guard (ready → listening while a session is up)
    is unchanged by the new speaking guard."""
    c = _controller()
    c._live = object()
    c.speech.speaking = False
    c.set_state("ready")
    assert c.ui.states == ["listening"]
    assert c.last_state == "listening"


# ---- Gemini Live: native audio bypasses SpeechOutput, so drive the word ------

def test_live_speaking_drives_the_status_word():
    """In Live mode the resting state is 'listening (Live)'; the model's audio
    stream must flip it to Speaking while she talks, then back."""
    c = _controller()
    c._live = object()
    c.speech.speaking = False            # Live audio is not SpeechOutput
    c._on_live_speaking(True)
    assert c.ui.states[-1] == "speaking"
    c._on_live_speaking(False)
    assert c.ui.states[-1] == "listening"


def test_live_speaking_is_a_noop_without_a_session():
    c = _controller()
    c._live = None
    c._on_live_speaking(True)
    assert c.ui.states == []


def test_live_model_speaking_transitions_dedup():
    """The drain loop calls _set_model_speaking every chunk / every idle beat;
    on_speaking must fire only on the actual edges."""
    from app.voice.live_engine import LiveEngine
    calls = []
    eng = LiveEngine(make_config(), None, None, None, None,
                     on_speaking=calls.append)
    eng._set_model_speaking(True)
    eng._set_model_speaking(True)        # still speaking — no repeat
    eng._set_model_speaking(False)
    eng._set_model_speaking(False)       # still idle — no repeat
    assert calls == [True, False]
