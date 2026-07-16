"""Core Safety Ritual S1–S8 — THROUGH THE DAEMON PATH (Phase 0, commit 4).

Protocol §11: any failure here means the phase failed, however good the new
feature is. This is the first commit that could hide a validator regression
behind a socket, so every step below drives the FULL daemon stack:

    real WebSocket → ProtocolSession → JsApi.send_text → Controller →
    CommandPipeline → validate_action (REAL) → ConfirmationManager (REAL)

Only the outermost edges are fakes: the Agent's *router* returns pinned plans
(so each S-step is deterministic) and its *executor* records instead of
touching the OS. The validator, the confirmation state machine, the strong
phrase, the event log and the wire are all production code.

Honest scope notes (the remainder stays on the MANUAL checklist, M-doc):
  * S4 here proves the "look anyway needs the strong phrase" half at the
    validator; the OCR pre-scan that *initially* refuses needs a real screen
    with a real password field — manual.
  * S6 exercises the kill switch over the wire against a REAL screen-watch
    thread on the dev machine.
"""

import json
import threading
import time

import pytest

from app.agent.safety import set_target_resolver
from app.core.eventlog import EventLog
from app.core.daemon import wire_controller
from app.core.inspect_events import scan_secrets
from app.core.protocol import encode, make
from app.llm.intent_parser import ActionPlan
from tests.fakes import FakeAgent, FakeHistory, make_config
from tests.test_phase0_daemon import RECV_S, TOKEN, CoreServerThread, connect_authed


def plan(tool, **args):
    return ActionPlan(intent=tool, tool_name=tool, arguments=dict(args))


# Pinned routing for the ritual utterances — deterministic, so a failure is a
# safety failure, never a routing flake.
RITUAL_PLANS = [
    ("delete junk", plan("delete_files", target="junk.txt")),
    ("draft an email", plan("compose_email", to="bob@example.com",
                            subject="hi", body="hello")),
    ("send the email", plan("send_email", to="bob@example.com",
                            subject="hi", body="hello")),
    ("pay the bill", plan("make_payment", amount="500")),
    ("look at my screen anyway", plan("look_at_screen",
                                      allow_sensitive="true")),
    ("close chrome", plan("window_control", action="close", app="chrome")),
]


def route(text: str):
    lowered = (text or "").lower()
    for key, planned in RITUAL_PLANS:
        if key in lowered:
            return planned
    return None


class FakeMemory:
    def get(self, key, default=None):
        return default

    def set(self, key, value):
        pass


class Wire:
    """One authenticated client: sends requests, watches the event stream."""

    def __init__(self, url):
        self.ws = connect_authed(url)
        self.seen = []

    def request(self, method, **args):
        frame = make("request", {"method": method, "args": args})
        self.ws.send(encode(frame))
        deadline = time.monotonic() + RECV_S
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv(timeout=RECV_S))
            if msg["type"] == "event":
                self.seen.append(msg["payload"])
                continue
            if msg["type"] == "response" and msg.get("re") == frame["id"]:
                return msg["payload"]
        raise AssertionError(f"no response to {method}")

    def say(self, text):
        assert self.request("send_text", text=text)["ok"]

    def wait_for(self, event, where=None, timeout=8):
        for payload in list(self.seen):
            if payload["event"] == event and (where is None or where(payload["data"])):
                self.seen.remove(payload)
                return payload["data"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv(timeout=timeout))
            if msg["type"] != "event":
                continue
            payload = msg["payload"]
            if payload["event"] == event and (where is None or where(payload["data"])):
                return payload["data"]
            self.seen.append(payload)
        recent = [p["event"] for p in self.seen[-15:]]
        raise AssertionError(f"never saw event {event!r}; recent: {recent}")

    def assert_never(self, event, seconds=1.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self.ws.recv(timeout=deadline - time.monotonic()))
            except Exception:
                break
            if msg["type"] == "event":
                if msg["payload"]["event"] == event:
                    raise AssertionError(f"{event!r} arrived — it must not")
                self.seen.append(msg["payload"])


class Stack:
    def __init__(self, tmp_dir):
        self.log = EventLog(tmp_dir / "events.sqlite")
        self.core = CoreServerThread(eventlog=self.log)
        self.agent = FakeAgent(make_config(), rule=route)
        self.controller = wire_controller(
            self.core.server, config=make_config(), memory=FakeMemory(),
            history=FakeHistory(), autostart=False)
        self.controller.speech.shutdown()
        # Deterministic router/executor at the EDGES; validator + confirmation
        # manager + pipeline remain production code.
        self.controller.pipeline.agent = self.agent
        self.wire = Wire(self.core.url)

    @property
    def pipeline(self):
        return self.controller.pipeline

    def executed_tools(self):
        return [p.tool_name for p in self.agent.executed]

    def settle(self):
        """No pending card may leak into the next S-step."""
        if self.pipeline.pending is not None:
            self.pipeline.cancel_pending(reason="ritual-cleanup")
        self.agent.executed.clear()
        self.wire.seen.clear()

    def close(self):
        set_target_resolver(None)
        try:
            self.wire.ws.close()
        except Exception:
            pass
        self.core.stop()
        self.log.close()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("ritual"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _settled(stack):
    stack.settle()
    yield
    stack.settle()


# ---- S1: delete needs the STRONG phrase; a plain "yes" is refused --------------

def test_s1_delete_red_card_plain_yes_refused_strong_phrase_works(stack):
    stack.wire.say("delete junk please")
    card = stack.wire.wait_for("confirm_request")
    assert card["tool"] == "delete_files"
    assert card["strong_required"] is True
    assert card["risk"] == "high"
    assert stack.executed_tools() == []            # nothing ran on the card

    stack.wire.say("yes")                          # casual — REFUSED
    stack.wire.wait_for("anna_message",
                        where=lambda d: "anna approve" in d["text"].lower())
    assert stack.pipeline.pending is not None      # card still armed
    assert stack.executed_tools() == []

    stack.wire.say("anna approve")                 # strong — accepted
    stack.wire.wait_for("confirm_resolved")
    assert stack.executed_tools() == ["delete_files"]
    assert stack.pipeline.pending is None


# ---- S2: drafting is free; SENDING needs the strong phrase ----------------------

def test_s2_draft_opens_without_a_card_send_demands_strong_phrase(stack):
    stack.wire.say("draft an email to bob")        # compose = SAFE tier
    stack.wire.assert_never("confirm_request", seconds=1.0)
    assert stack.executed_tools() == ["compose_email"]

    stack.settle()
    stack.wire.say("send the email")               # send = strong tier
    card = stack.wire.wait_for("confirm_request")
    assert card["tool"] == "send_email" and card["strong_required"] is True
    assert "compose_email" not in stack.executed_tools()
    assert "send_email" not in stack.executed_tools()   # NOTHING sent

    stack.wire.say("yes")                          # casual still refused
    assert stack.pipeline.pending is not None
    stack.wire.say("cancel")
    stack.wire.wait_for("confirm_resolved")
    assert "send_email" not in stack.executed_tools()


# ---- S3: payments are REFUSED OUTRIGHT, never merely confirmed -------------------

def test_s3_payment_refused_outright_no_card_no_execution(stack):
    stack.wire.say("pay the bill")
    stack.wire.wait_for("anna_message")            # she says no
    stack.wire.assert_never("confirm_request", seconds=1.0)
    assert stack.executed_tools() == []
    assert stack.pipeline.pending is None

    # Same law for a resolved "Pay now" BUTTON (11C path, hardcoded in the
    # validator): blocked before anything is clicked.
    set_target_resolver(lambda plan, config: {
        "name": "Pay now", "control_type": "Button", "backend": "uia",
        "confidence": 1.0})
    try:
        stack.agent.rule = lambda t: plan("click_control", hint="Pay now") \
            if "click pay" in t.lower() else route(t)
        stack.wire.say("click pay now")
        stack.wire.wait_for("anna_message")
        stack.wire.assert_never("confirm_request", seconds=1.0)
        assert stack.executed_tools() == []
    finally:
        set_target_resolver(None)
        stack.agent.rule = route


# ---- S4: "look anyway" at sensitive content needs explicit strong approval -------

def test_s4_sensitive_look_anyway_needs_strong_approval(stack):
    stack.wire.say("look at my screen anyway")
    card = stack.wire.wait_for("confirm_request")
    assert card["tool"] == "look_at_screen"
    assert card["risk"] == "high" and card["strong_required"] is True
    assert stack.executed_tools() == []            # not one pixel analyzed yet
    stack.wire.say("cancel")
    stack.wire.wait_for("confirm_resolved")
    assert stack.executed_tools() == []


# ---- S5: "cancel" cancels instantly, every time -----------------------------------

def test_s5_cancel_works_instantly_every_time(stack):
    for _ in range(3):
        stack.wire.say("close chrome")
        stack.wire.wait_for("confirm_request")
        stack.wire.say("cancel")
        stack.wire.wait_for("confirm_resolved")
        assert stack.pipeline.pending is None
        assert stack.executed_tools() == []
        stack.settle()


# ---- S6: privacy mode stops everything, over the wire ------------------------------

def test_s6_privacy_mode_kill_switch_over_the_wire(stack):
    vision = stack.controller.vision
    assert vision.start_watching()                 # a REAL screen-watch thread
    assert vision.watching
    reply = stack.wire.request("privacy_mode")
    assert reply["ok"]
    assert not vision.watching                     # stopped, immediately


# ---- S7: a random word is NOT approval; the card stays parked ----------------------

def test_s7_random_word_is_not_approval(stack):
    stack.wire.say("close chrome")
    stack.wire.wait_for("confirm_request")
    stack.wire.say("banana")
    stack.wire.assert_never("confirm_resolved", seconds=1.0)
    assert stack.pipeline.pending is not None      # still parked
    assert stack.executed_tools() == []
    stack.wire.say("banana banana banana")         # persistence changes nothing
    assert stack.pipeline.pending is not None
    assert stack.executed_tools() == []
    stack.wire.say("cancel")
    stack.wire.wait_for("confirm_resolved")


# ---- S8: the session's log holds no secrets (runs LAST — scans S1–S7's traffic) ----

def test_s8_event_log_of_the_whole_ritual_holds_no_secrets(stack):
    assert stack.log.flush(timeout=10)
    rows = stack.log.recent(limit=500)
    assert rows, "the ritual session logged nothing at all?"
    hits = scan_secrets(stack.log.path)            # every byte, every file
    assert hits == [], f"secret-shaped bytes in the ritual log: {hits}"
    body = "\n".join(json.dumps(r, default=str) for r in rows)
    assert TOKEN not in body                       # the IPC token stayed out
