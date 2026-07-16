"""Phase 0 commit 4: the anna-core WebSocket server — where production risk
starts. The five pinned requirements:

  1. A non-loopback connection is ACTUALLY refused (real connect attempt to
     this machine's LAN address), plus the handler's defence-in-depth peer
     check for the day the bind line changes.
  2. Port-in-use fails loud: one clear message, exit 2, no silent re-port,
     no crash-loop.
  3. Fan-out isolation: one slow/dead client never stalls core, the writer,
     or other clients — bounded per-client queue, laggard disconnected.
  4. Auth-failure bursts are rate-limited and land in the event log where
     --doctor surfaces them.
  5. (Core Safety Ritual through the daemon path lives in
     tests/test_phase0_ritual.py.)

Real sockets throughout: the server runs in a thread on an ephemeral port and
tests connect with websockets' sync client.
"""

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace as NS

import pytest
from websockets.sync.client import connect

from app.core.eventlog import EventLog
from app.core.protocol import PROTOCOL_VERSION, encode, make
from app.core.server import (AuthThrottle, CoreServer, PortInUseError,
                             REQUEST_METHODS, _Client, _is_loopback,
                             make_request_handler)

TOKEN = "d4" * 32
RECV_S = 5


class CoreServerThread:
    """A real CoreServer on an ephemeral port, loop in a daemon thread."""

    def __init__(self, expect_error: bool = False, **kw):
        kw.setdefault("token", TOKEN)
        kw.setdefault("port", 0)
        self.server = CoreServer(**kw)
        self.error = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), f"server failed to start: {self.error}"
        if not expect_error:
            assert self.error is None, f"server failed to start: {self.error}"

    def _run(self):
        async def main():
            await self.server.start()
            self._ready.set()
            await self.server.serve_forever()
        try:
            asyncio.run(main())
        except Exception as e:                    # noqa: BLE001 — surfaced above
            self.error = e
            self._ready.set()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}"

    def stop(self):
        self.server.stop()
        self._thread.join(5)


@pytest.fixture
def core():
    thread = CoreServerThread()
    yield thread
    thread.stop()


def hello(token=TOKEN):
    return encode(make("hello", {"token": token, "client": "test"}))


def connect_authed(url):
    ws = connect(url)
    ws.send(hello())
    reply = json.loads(ws.recv(timeout=RECV_S))
    assert reply["type"] == "hello_ok"
    return ws


# ---- smoke: the daemon path is real ------------------------------------------

def test_handshake_and_broadcast_over_a_real_socket(core):
    ws = connect_authed(core.url)
    core.server.broadcast("state_change", {"state": "thinking", "detail": ""})
    frame = json.loads(ws.recv(timeout=RECV_S))
    assert frame["type"] == "event" and frame["v"] == PROTOCOL_VERSION
    assert frame["payload"] == {"event": "state_change",
                                "data": {"state": "thinking", "detail": ""}}
    ws.close()


def test_unauthenticated_connection_never_sees_broadcasts(core):
    ws = connect(core.url)                        # connected, no hello
    core.server.broadcast("state_change", {"state": "secret"})
    ws.send(hello())                              # only NOW authenticate
    reply = json.loads(ws.recv(timeout=RECV_S))
    assert reply["type"] == "hello_ok"            # first frame is the ack,
    core.server.broadcast("state_change", {"state": "after"})
    frame = json.loads(ws.recv(timeout=RECV_S))   # next is post-auth traffic
    assert frame["payload"]["data"]["state"] == "after"   # never "secret"
    ws.close()


# ---- 1. loopback only -----------------------------------------------------------

def _lan_ip():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))            # UDP: no packet actually sent
        ip = probe.getsockname()[0]
        probe.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def test_non_loopback_connection_is_actually_refused(core):
    """Not 'we passed 127.0.0.1 to bind' — a REAL connect attempt to this
    machine's LAN address must fail to establish."""
    ip = _lan_ip()
    if ip is None:
        pytest.skip("machine has no non-loopback address (offline)")
    with pytest.raises(OSError):
        conn = socket.create_connection((ip, core.server.port), timeout=2)
        conn.close()                              # reached = the bind leaked
    # and loopback still works, same instant, same port:
    connect_authed(core.url).close()


def test_handler_peer_check_is_fail_closed():
    """Defence in depth for the day someone edits the bind line."""
    assert _is_loopback(("127.0.0.1", 4242))
    assert _is_loopback(("127.9.9.9", 1))
    assert _is_loopback(("::1", 4242, 0, 0))
    assert not _is_loopback(("192.168.1.50", 4242))
    assert not _is_loopback(("10.0.0.9", 4242))
    assert not _is_loopback(("0.0.0.0", 1))
    assert not _is_loopback(None)                 # unknown = NOT loopback
    assert not _is_loopback(())


def test_handler_closes_non_loopback_peer_before_reading(tmp_path):
    """Even if a foreign peer somehow reached the handler, it is closed
    before a single frame is read — and the refusal is audited."""
    log = EventLog(tmp_path / "events.sqlite")
    server = CoreServer(token=TOKEN, eventlog=log)
    closed = []

    class ForeignWs:
        remote_address = ("192.168.1.99", 55555)
        async def close(self, code=1000, reason=""):
            closed.append((code, reason))
        def __aiter__(self):
            raise AssertionError("handler read from a non-loopback peer")

    asyncio.run(server._handler(ForeignWs()))
    assert closed == [(1008, "loopback only")]
    assert log.flush(timeout=10)
    rows = log.recent(event_type="error")
    assert any("non-loopback" in r["payload"].get("message", "") for r in rows)
    log.close()


# ---- 2. port-in-use fails loud ----------------------------------------------------

def test_port_in_use_fails_loud_no_reporting_no_crash_loop(capsys):
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    try:
        holder = CoreServerThread(port=port, expect_error=True)
        holder.stop()
        assert isinstance(holder.error, PortInUseError)
        message = str(holder.error)
        assert str(port) in message
        assert "already in use" in message
        assert "--port" in message                # tells the human the way out
        assert "Refusing to pick a different port" in message

        # And the daemon entrypoint maps it to a clean, LOUD exit 2 — once.
        # (The port is bound before the Controller exists, so this returns
        # without ever touching mic/hotkeys/event log.)
        from app.core.daemon import run_daemon
        started = time.monotonic()
        code = run_daemon(["--port", str(port)])
        elapsed = time.monotonic() - started
        assert code == 2
        assert elapsed < 10, "run_daemon retried instead of failing loud"
        out = capsys.readouterr().out
        assert "already in use" in out and str(port) in out
    finally:
        squatter.close()


# ---- 3. fan-out isolation ----------------------------------------------------------

def _fake_client(queue_max=2):
    client = _Client(ws=NS(close=None), session=NS(authenticated=True),
                     queue_max=queue_max)
    client.ready = True
    return client


def test_one_stuffed_client_is_evicted_others_get_everything(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")
    server = CoreServer(token=TOKEN, eventlog=log, client_queue_max=2)
    laggard, healthy = _fake_client(queue_max=2), _fake_client(queue_max=64)
    server._clients.update({laggard, healthy})

    for n in range(5):
        server._fanout(f"frame-{n}")

    assert laggard not in server._clients          # evicted at overflow
    assert healthy in server._clients
    assert healthy.queue.qsize() == 5              # got every frame
    assert laggard.queue.qsize() == 2              # bounded — RAM capped
    assert log.flush(timeout=10)
    rows = log.recent(event_type="error")
    assert any("overflow" in r["payload"].get("message", "") for r in rows)
    log.close()


def test_stalled_peer_never_disturbs_a_draining_client():
    """A stalled reader merely lags (kernel buffers absorb a paced stream);
    the draining client gets every frame in order and the broadcasting thread
    never blocks. The eviction threshold itself is pinned deterministically in
    test_one_stuffed_client_is_evicted…; true socket saturation is next."""
    core = CoreServerThread(client_queue_max=64)
    try:
        healthy = connect_authed(core.url)
        # A genuinely slow CONSUMER: websockets' sync client drains the socket
        # on a background thread regardless of recv() calls, so "just don't
        # recv" creates no TCP backpressure. max_queue=1 makes its reader stop
        # after one buffered frame — real backpressure at the transport.
        stalled = connect(core.url, max_queue=1)
        stalled.send(hello())
        assert json.loads(stalled.recv(timeout=RECV_S))["type"] == "hello_ok"

        received, reader_error = [], []

        def drain():
            try:
                while len(received) < 300:
                    frame = json.loads(healthy.recv(timeout=RECV_S))
                    if frame["type"] == "event":
                        received.append(frame["payload"]["data"]["n"])
            except Exception as e:                 # surfaced by the assert below
                reader_error.append(repr(e))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        padding = "x" * 8192
        started = time.monotonic()
        for n in range(300):
            core.server.broadcast("state_change", {"n": n, "pad": padding})
            time.sleep(0.002)                      # a real pipeline's cadence
        broadcast_cost = time.monotonic() - started
        assert broadcast_cost < 5, "broadcast blocked the calling thread"

        reader.join(30)
        assert not reader_error, f"healthy client failed: {reader_error}"
        assert received == list(range(300))        # every frame, in order
        healthy.close()
        stalled.close()
    finally:
        core.stop()


def test_saturated_client_is_disconnected_end_to_end():
    """True saturation over a real socket: an unpaced 25MB burst at a stalled
    reader exceeds what Windows loopback auto-tuning will absorb (a paced
    ~2.5MB sails through — measured), so TCP backpressure reaches the sender,
    the bounded queue overflows, and the client is force-disconnected. The
    burst must still cost the broadcasting thread nothing."""
    from websockets.exceptions import ConnectionClosed
    core = CoreServerThread(client_queue_max=64)
    try:
        slow = connect(core.url, max_queue=1)
        slow.send(hello())
        assert json.loads(slow.recv(timeout=RECV_S))["type"] == "hello_ok"

        padding = "x" * 65536
        started = time.monotonic()
        for n in range(400):                       # 25MB, no pacing
            core.server.broadcast("state_change", {"n": n, "pad": padding})
        assert time.monotonic() - started < 5, "broadcast blocked the caller"

        deadline = time.monotonic() + 15
        while core.server._clients and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not core.server._clients, "saturated client was never evicted"

        with pytest.raises(ConnectionClosed):      # disconnected, not lagging
            while True:
                slow.recv(timeout=RECV_S)
    finally:
        core.stop()


def test_dead_client_does_not_stall_broadcast(core):
    victim = connect_authed(core.url)
    victim.socket.close()                          # dies without a goodbye
    survivor = connect_authed(core.url)
    for n in range(10):
        core.server.broadcast("state_change", {"n": n})
    seen = [json.loads(survivor.recv(timeout=RECV_S))["payload"]["data"]["n"]
            for _ in range(10)]
    assert seen == list(range(10))
    survivor.close()


# ---- 4. auth-failure bursts: rate-limited + surfaced -------------------------------

def test_auth_throttle_sliding_window():
    clock = NS(now=1000.0)
    throttle = AuthThrottle(max_failures=3, window_s=60,
                            clock=lambda: clock.now)
    assert not throttle.blocked()
    for _ in range(3):
        throttle.record_failure()
    assert throttle.blocked()
    clock.now += 61                                # window slides — recovery
    assert not throttle.blocked()


def test_auth_failure_burst_locks_out_even_the_right_token(tmp_path):
    log = EventLog(tmp_path / "events.sqlite")
    core = CoreServerThread(eventlog=log,
                            throttle=AuthThrottle(max_failures=3, window_s=60))
    try:
        for _ in range(3):                         # three failed handshakes
            ws = connect(core.url)
            ws.send(hello(token="wrong-" + TOKEN))
            reply = json.loads(ws.recv(timeout=RECV_S))
            assert reply["type"] == "auth_failed"
            assert reply["payload"]["reason"] == "bad-token"
            ws.close()

        ws = connect(core.url)                     # correct token, too late
        reply = json.loads(ws.recv(timeout=RECV_S))
        assert reply["type"] == "auth_failed"
        assert reply["payload"]["reason"] == "rate-limited"
        ws.close()

        # ...and the burst is in the event log, where --doctor looks.
        assert log.flush(timeout=10)
        summary = log.error_summary("ipc-auth", hours=1.0)
        assert summary["count"] >= 3 and summary["last"]
    finally:
        core.stop()
        log.close()


def test_error_summary_respects_component_and_window(tmp_path):
    import sqlite3
    log = EventLog(tmp_path / "events.sqlite")
    log.emit("error", source="ipc", component="ipc-auth", message="bad token")
    log.emit("error", source="ipc", component="ipc-auth", message="bad token")
    log.emit("error", source="ipc", component="ipc", message="unrelated")
    log.emit("error", source="brain", component="groq", message="unrelated")
    assert log.flush(timeout=10)
    log.close()
    # A failure from last week must fall OUT of the 24h window.
    conn = sqlite3.connect(str(log.path))
    conn.execute("INSERT INTO events (ts, type, source, payload_json) "
                 "VALUES ('2026-07-09T00:00:00', 'error', 'ipc', ?)",
                 ('{"component": "ipc-auth", "message": "ancient"}',))
    conn.commit()
    conn.close()
    reader = EventLog(log.path, start=False)
    assert reader.error_summary("ipc-auth", hours=1.0)["count"] == 2
    assert reader.error_summary("ipc-auth", hours=24 * 30)["count"] == 3


# ---- the request surface is a whitelist ---------------------------------------------

def test_request_whitelist_confirm_and_dunders_do_not_exist():
    calls = []

    class Api:
        def send_text(self, text):
            calls.append(text)
        def confirm(self, action_id, approved):    # exists on the object...
            raise AssertionError("confirm must be unreachable over IPC")

    handler = make_request_handler(Api())
    handler("send_text", {"text": "hi"})
    assert calls == ["hi"]
    for forbidden in ("confirm", "ready", "pick_voice_file",
                      "__init__", "__class__", "_private"):
        with pytest.raises(ValueError):
            handler(forbidden, {})
    assert "confirm" not in REQUEST_METHODS        # approvals ride approval frames


def test_request_methods_all_exist_on_the_real_jsapi():
    from app.web.bridge import JsApi
    for name in REQUEST_METHODS:
        assert callable(getattr(JsApi, name, None)), f"JsApi.{name} missing"
