"""Phase 0 commit 5: the UI-as-client seam.

Two halves:
  * the JS transport's own behaviour, pinned headlessly under node
    (tests/js/transport.test.cjs), run here so it lives in the normal suite;
  * the SERVER guarantees the transport relies on — reconnect re-hydration is
    authoritative and exact, an approval is never applied while the client is
    gone, a mid-card drop leaves the card intact and re-hydratable by id, and
    the localhost WS hop does not regress what a turn costs.

The server half runs the REAL stack (wire_controller → real pipeline → real
validator → real confirmation manager) over real sockets; only the plan router
and executor are pinned fakes, so a confirm card is deterministic.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from websockets.sync.client import connect

from app.core.daemon import wire_controller
from app.core.eventlog import EventLog
from app.core.protocol import encode, make
from app.core.server import REQUEST_METHODS
from app.llm.intent_parser import ActionPlan
from app.tools import TOOL_SPECS
from tests.fakes import FakeAgent, FakeHistory, make_config
from tests.test_phase0_daemon import RECV_S, TOKEN, CoreServerThread, hello

REPO = Path(__file__).resolve().parents[1]


# ---- the JS transport suite, in the Python run --------------------------------

def test_js_transport_suite():
    """Runs tests/js/transport.test.cjs under node and fails on non-zero exit,
    surfacing node's own assertion output."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, str(REPO / "tests" / "js" / "transport.test.cjs")],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"JS transport tests failed:\n{result.stdout}\n{result.stderr}")
    assert "assertions passed" in result.stdout


# ---- the call surface agrees end to end ---------------------------------------

def test_arg_spec_ws_methods_match_the_server_whitelist():
    """Every non-native, non-approval method in transport.js's ARG_SPEC must be
    a real WS request method, and every WS request method must be in ARG_SPEC —
    a method that exists on one side only is drift, caught here."""
    text = (REPO / "app" / "web" / "transport.js").read_text(encoding="utf-8")
    block = text[text.index("ARG_SPEC = {"):text.index("PROTOCOL_VERSION = 1")]
    ws_methods, native, approval = set(), set(), set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith(("ready", "get_ws_config")) and ":" not in line:
            continue
        name = line.split(":", 1)[0].strip()
        if not name.isidentifier():
            continue
        if "native: true" in line:
            native.add(name)
        elif "approval: true" in line:
            approval.add(name)
        else:
            ws_methods.add(name)
    assert approval == {"confirm"}
    assert {"get_ws_config", "pick_voice_file", "ready"} <= native
    # The WS methods are exactly the server's request whitelist.
    assert ws_methods == set(REQUEST_METHODS), (
        f"ARG_SPEC vs server drift: "
        f"only-in-js={ws_methods - set(REQUEST_METHODS)}, "
        f"only-in-server={set(REQUEST_METHODS) - ws_methods}")


# ---- the server-side stack ----------------------------------------------------

def _plan(tool, **args):
    return ActionPlan(intent=tool, tool_name=tool, arguments=dict(args))


def _route(text):
    if "close chrome" in (text or "").lower():
        return _plan("window_control", action="close", app="chrome")
    return None


class UiStack:
    def __init__(self, tmp_path):
        self.log = EventLog(tmp_path / "events.sqlite")
        self.core = CoreServerThread(eventlog=self.log)
        self.controller = wire_controller(
            self.core.server, config=make_config(), memory=_Mem(),
            history=FakeHistory(), autostart=False)
        self.controller.speech.shutdown()
        self.agent = FakeAgent(make_config(), rule=_route)
        self.controller.pipeline.agent = self.agent
        # Deterministic: assert execution state without racing a worker thread.
        self.controller.pipeline.run_async = False

    def close(self):
        try:
            self.core.stop()
        finally:
            self.log.close()


class _Mem:
    def get(self, k, d=None): return d
    def set(self, k, v): pass


class Client:
    """A UI client: authenticate, collect events, send requests/approvals."""

    def __init__(self, url):
        self.ws = connect(url)
        self.ws.send(hello())
        self.events = []
        self.full_states = []
        # core sends hello_ok, then pushes full_state (on_client_ready). Pump
        # until that snapshot lands, so a reconnecting client always re-hydrates.
        assert self._pump_for_type("hello_ok"), "no hello_ok"
        assert self._pump_until_event("full_state"), "no full_state after connect"

    def _pump_for_type(self, want_type, timeout=RECV_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self.ws.recv(timeout=deadline - time.monotonic()))
            except Exception:
                break
            if msg["type"] == "event":
                self._record(msg["payload"])
            if msg["type"] == want_type:
                return msg
        return None

    def _pump_until_event(self, event_name, timeout=RECV_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self.ws.recv(timeout=deadline - time.monotonic()))
            except Exception:
                break
            if msg["type"] == "event":
                self._record(msg["payload"])
                if msg["payload"]["event"] == event_name:
                    return msg
        return None

    def _record(self, payload):
        self.events.append(payload)
        if payload["event"] == "full_state":
            self.full_states.append(payload["data"])

    def request(self, method, **args):
        f = make("request", {"method": method, "args": args})
        self.ws.send(encode(f))
        return self._pump_for_type("response")["payload"]

    def approve(self, cid, decision="approve"):
        f = make("approval", {"confirmation_id": cid, "decision": decision})
        self.ws.send(encode(f))
        return self._pump_for_type("approval_result")["payload"]

    def drain_events(self, seconds=0.6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self.ws.recv(timeout=deadline - time.monotonic()))
            except Exception:
                break
            if msg["type"] == "event":
                self.events.append(msg["payload"])
                if msg["payload"]["event"] == "full_state":
                    self.full_states.append(msg["payload"]["data"])

    def event_names(self):
        return [e["event"] for e in self.events]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


@pytest.fixture
def stack(tmp_path):
    s = UiStack(tmp_path)
    yield s
    s.close()


# ---- 2/4. reconnect re-hydration is authoritative and exact -------------------

def test_full_state_pushed_on_every_connect(stack):
    client = Client(stack.core.url)
    assert client.full_states, "no full_state on first connect"
    assert client.full_states[-1]["state"]         # a real snapshot
    client.close()


def test_reconnect_after_activity_rehydrates_without_dupes_or_gaps(stack):
    client = Client(stack.core.url)
    client.request("send_text", text="close chrome")     # activity → a card
    client.drain_events()
    assert "confirm_request" in client.event_names()
    card_id = stack.controller.pipeline.pending.id

    client.close()                                        # drop mid-card
    time.sleep(0.2)
    # While the UI is gone the card is untouched and nothing executed.
    assert stack.controller.pipeline.pending is not None
    assert stack.controller.pipeline.pending.id == card_id
    assert stack.agent.executed == []

    rejoin = Client(stack.core.url)                       # reconnect
    snap = rejoin.full_states[-1]
    # Authoritative snapshot: the card survives, by id.
    assert snap["pending"] is not None and snap["pending"]["id"] == card_id
    # No missed state: the "close chrome" turn is in the snapshot...
    convo = " ".join(json.dumps(m) for m in snap["conversation"])
    assert "close chrome" in convo
    # ...and exactly once — the snapshot IS the state, not an append to it.
    assert convo.count("close chrome") == 1
    rejoin.close()


# ---- 3. an approval is never applied while the client is gone -----------------

def test_no_approval_is_applied_while_disconnected_then_it_works_on_return(stack):
    client = Client(stack.core.url)
    client.request("send_text", text="close chrome")
    client.drain_events()
    card_id = stack.controller.pipeline.pending.id
    client.close()

    # No approval frame can arrive while disconnected; the card just waits.
    time.sleep(0.3)
    assert stack.controller.pipeline.pending is not None
    assert stack.agent.executed == []

    rejoin = Client(stack.core.url)
    assert rejoin.full_states[-1]["pending"]["id"] == card_id   # re-hydrated
    result = rejoin.approve(card_id, "approve")                 # NOW it lands
    assert result["outcome"] == "applied"
    assert [p.tool_name for p in stack.agent.executed] == ["window_control"]
    rejoin.close()


def test_stale_approval_after_reconnect_touches_nothing(stack):
    """A client that reconnects and fires an approval for a card that expired
    while it was away gets a logged no-op, not a surprise action."""
    client = Client(stack.core.url)
    client.request("send_text", text="close chrome")
    client.drain_events()
    card_id = stack.controller.pipeline.pending.id
    client.close()

    stack.controller.pipeline.cancel_pending(reason="timeout", action_id=card_id)
    assert stack.controller.pipeline.pending is None

    rejoin = Client(stack.core.url)
    result = rejoin.approve(card_id, "approve")
    assert result["outcome"] in ("rejected-unknown", "rejected-stale")
    assert stack.agent.executed == []
    rejoin.close()


# ---- 5. the localhost WS hop does not regress the felt latency ----------------

def test_ws_hop_latency_is_negligible(stack):
    """Measure the round-trip the WS seam ADDS: a request/response cycle for a
    cheap synchronous method. It must be small next to a real turn (STT + LLM +
    TTS is hundreds of ms to seconds); a localhost JSON hop should be single-
    digit ms. Budget generously (CI is noisy) but assert it never balloons."""
    client = Client(stack.core.url)
    client.request("get_history", page=0)                # warm the path
    samples = []
    for _ in range(40):
        t0 = time.perf_counter()
        reply = client.request("get_history", page=0)
        samples.append((time.perf_counter() - t0) * 1000)
        assert reply["ok"]
    samples.sort()
    median = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    print(f"\nWS hop round-trip: median={median:.2f}ms p95={p95:.2f}ms "
          f"min={samples[0]:.2f}ms max={samples[-1]:.2f}ms")
    # A turn the user feels is dominated by STT/LLM/TTS (100s of ms–seconds).
    # The added hop must stay well under that; 50ms median is a loud alarm.
    assert median < 50, f"WS hop median {median:.1f}ms — too slow"
    assert p95 < 150, f"WS hop p95 {p95:.1f}ms — too slow"
    client.close()
