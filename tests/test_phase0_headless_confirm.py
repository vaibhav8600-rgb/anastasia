"""Phase 0 commit 7 / D-0.5: headless voice-confirm — a NEW route to approving
actions, so the Core Safety Ritual runs THROUGH it.

The load-bearing claim: this is a new INPUT channel to the SAME validated path,
never a softer one. Every answer goes through `handle_confirmation_utterance`,
so a destructive-tier card still demands "Anna approve", a casual "yes" is still
refused, an ambiguous word still parks the card, and it only ever happens with
NO window attached — after Anna speaks the question, with an audible cue, for
one short window.
"""

import pytest

from app.agent.safety import set_target_resolver, validate_action
from app.llm.intent_parser import ActionPlan
from tests.fakes import FakeAgent, FakeHistory, FakeMainUI, make_config


class HeadlessUI(FakeMainUI):
    """A FakeMainUI that reports NO attached window (the daemon, windowless)."""

    def __init__(self, attached=False):
        super().__init__()
        self._attached = attached

    def has_attached_ui(self):
        return self._attached

    def dispatch(self, *a, **k):
        pass


class _Mem:
    def get(self, k, d=None): return d
    def set(self, k, v): pass


def build(attached=False, **cfg):
    from app.main import Controller
    ui = HeadlessUI(attached=attached)
    c = Controller(ui=ui, autostart=False, config=make_config(**cfg),
                   memory=_Mem(), history=FakeHistory())
    c.speech.shutdown()
    c.pipeline.run_async = False                  # deterministic execution
    c.agent = c.pipeline.agent = FakeAgent(c.config)
    # Neutralise the real audio/TTS; record what the flow WOULD do.
    c._events = {"spoke": [], "cue": 0}
    c.speech.speak_async = lambda text: c._events["spoke"].append(text)
    c._wait_until_quiet = lambda timeout=15.0: None
    c._play_mic_cue = lambda: c._events.__setitem__("cue", c._events["cue"] + 1)
    return c, ui


@pytest.fixture(autouse=True)
def _clean_resolver():
    yield
    set_target_resolver(None)


def stage(c, tool, **args):
    """Raise a real pending card (real plan + real validator verdict)."""
    plan = ActionPlan(intent=tool, tool_name=tool, arguments=dict(args))
    safety = validate_action(plan, c.config)
    if not safety.allowed:
        return None, safety            # blocked -> no card at all
    res = c.pipeline.confirm.request(plan, safety, f"do {tool}")
    return res.action_id, safety


def answer(c, action_id, utterance):
    """Drive the headless flow synchronously with a heard phrase."""
    c._hear_confirmation_answer = lambda: utterance
    c._run_headless_confirm(action_id)


def executed(c):
    return [p.tool_name for p in c.agent.executed]


# ---- gating: only when nobody can click --------------------------------------

def test_headless_confirm_never_triggers_when_a_window_is_attached():
    c, ui = build(attached=True)             # a window IS connected
    action_id, _ = stage(c, "delete_files", target="junk.txt")
    started = []
    c._run_headless_confirm = lambda aid: started.append(aid)
    c._headless_voice_confirm(action_id)     # the trigger
    assert started == [], "with a window attached, voice-confirm must not run"


def test_headless_confirm_respects_the_kill_switch():
    c, ui = build(attached=False, headless_voice_confirm=False)
    action_id, _ = stage(c, "delete_files", target="junk.txt")
    started = []
    c._run_headless_confirm = lambda aid: started.append(aid)
    c._headless_voice_confirm(action_id)
    assert started == []


def test_spoken_question_first_then_cue_then_listen():
    c, ui = build(attached=False)
    order = []
    c.speech.speak_async = lambda t: order.append("speak")
    c._play_mic_cue = lambda: order.append("cue")
    c._hear_confirmation_answer = lambda: order.append("listen") or "cancel"
    action_id, _ = stage(c, "window_control", action="close", app="chrome")
    c._run_headless_confirm(action_id)
    # The mic opens ONLY after the question is spoken and the cue plays. (Any
    # further speech is the outcome being confirmed — the first three are the
    # contract: ask → cue → open mic.)
    assert order[:3] == ["speak", "cue", "listen"], order


# ---- Core Safety Ritual S1–S8, THROUGH the headless voice route --------------

def test_s1_headless_delete_casual_yes_refused_strong_phrase_works():
    c, _ = build()
    action_id, safety = stage(c, "delete_files", target="junk.txt")
    assert safety.risk_level == "high"

    answer(c, action_id, "yes")                       # casual — REFUSED
    assert c.pipeline.pending is not None             # card still armed
    assert executed(c) == []

    answer(c, action_id, "anna approve")              # strong — accepted
    assert executed(c) == ["delete_files"]
    assert c.pipeline.pending is None


def test_s2_headless_send_email_needs_the_strong_phrase():
    c, _ = build()
    action_id, safety = stage(c, "send_email", to="bob@example.com",
                              subject="hi", body="hello")
    assert safety.destructive_target or safety.risk_level == "high"
    answer(c, action_id, "yeah go on")                # casual — refused
    assert "send_email" not in executed(c)
    assert c.pipeline.pending is not None
    answer(c, action_id, "anna approve")
    assert executed(c) == ["send_email"]


def test_s3_headless_payment_is_blocked_so_there_is_no_card_to_answer():
    c, _ = build()
    action_id, safety = stage(c, "make_payment", amount="500")
    assert action_id is None and safety.allowed is False   # refused outright
    assert c.pipeline.pending is None                      # nothing to approve


def test_s5_headless_cancel_cancels():
    c, _ = build()
    action_id, _ = stage(c, "window_control", action="close", app="chrome")
    answer(c, action_id, "cancel")
    assert c.pipeline.pending is None and executed(c) == []


def test_s7_headless_random_word_parks_the_card():
    c, _ = build()
    action_id, _ = stage(c, "window_control", action="close", app="chrome")
    answer(c, action_id, "banana")
    assert c.pipeline.pending is not None             # still parked
    assert executed(c) == []
    answer(c, action_id, "banana banana")             # persistence changes nothing
    assert c.pipeline.pending is not None and executed(c) == []


def test_headless_nothing_heard_leaves_the_card_for_expiry():
    c, _ = build()
    action_id, _ = stage(c, "window_control", action="close", app="chrome")
    answer(c, action_id, "")                          # silence / STT blank
    assert c.pipeline.pending is not None             # untouched; expires as usual
    assert executed(c) == []


def test_headless_answer_goes_through_the_same_validated_path():
    """A tier-0-looking casual approval on a destructive card is refused by the
    SAME handle_confirmation_utterance the buttons use — the voice route can
    never be the soft path."""
    c, _ = build()
    action_id, _ = stage(c, "run_terminal", command="git status")
    answer(c, action_id, "okay do it")                # casual on strong-tier tool
    assert c.pipeline.pending is not None
    assert executed(c) == []
