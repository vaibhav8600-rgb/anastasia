"""anna-core's WebSocket server (Phase 0, commit 4).

Binds `app.core.protocol.ProtocolSession` to real sockets and fans core events
out to connected UI clients. Everything security- or robustness-shaped is
explicit and tested:

  * **Loopback only, twice.** The listener binds 127.0.0.1 — a LAN connection
    is refused by the OS (tested with a real connect attempt) — and the
    handler ALSO checks the peer address before reading a byte, so a future
    bind-address mistake still cannot expose the socket (defence in depth).
  * **Port-in-use fails loud.** One clear message naming the port and the
    likely cause, non-zero exit. Never a silent re-port (a UI would connect to
    the wrong instance — or a squatter), never a retry loop.
  * **Fan-out isolation.** Broadcast never awaits a socket: each client gets a
    BOUNDED queue drained by its own sender task, and `put_nowait` is the only
    coupling. A slow or dead client overflows its own queue and is
    disconnected (close 1013); core threads, the event-log writer and every
    other client never notice. A disconnected UI re-hydrates from `full_state`
    on reconnect, so dropping a laggard's backlog loses nothing durable.
  * **Auth-failure bursts are throttled and recorded.** After
    `AUTH_MAX_FAILURES` failed handshakes in `AUTH_WINDOW_S`, new connections
    get `auth_failed {reason: rate-limited}` and a close — no token
    comparison, no oracle to hammer. Every failure lands in the event log as
    component "ipc-auth", which `--doctor` surfaces.

The request channel exposes an EXPLICIT whitelist mirroring the JsApi surface.
`confirm` is deliberately NOT on it: over IPC an approval must be an
`approval` frame naming its confirmation id (commit 3), so the window's
"approve whatever is up" shortcut physically does not exist remotely.
"""

import asyncio
import threading
import time

from app.agent.devlog import devlog
from app.core.protocol import ProtocolSession, encode, make
from app.web.bridge import UIBridge

DEFAULT_PORT = 8765
CLIENT_QUEUE_MAX = 512       # frames buffered per client before eviction
AUTH_MAX_FAILURES = 5
AUTH_WINDOW_S = 60.0

# The closed request surface (mirrors JsApi, app/web/bridge.py). Absent from
# this set = does not exist over IPC, whatever the client asks for.
#   - "confirm" is excluded BY DESIGN: approvals ride `approval` frames with a
#     confirmation id, never a generic request.
#   - "ready" is excluded: connection + hello replaces it (full_state follows).
#   - "pick_voice_file" is excluded: it opens a native file dialog, which
#     belongs to the window process.
REQUEST_METHODS = frozenset({
    "send_text", "start_ptt", "stop_ptt",
    "set_toggle", "open_settings", "save_settings", "live_consent",
    "camera_frame", "privacy_mode", "recheck",
    "test_voice", "validate_piper", "validate_kokoro",
    "validate_deepgram_tts", "test_microphone", "test_model",
    "get_brain_info", "get_history", "open_path", "reveal_path",
    "copy_image", "save_image_as", "clear_history",
})


class PortInUseError(RuntimeError):
    """The IPC port is taken. Loud by design — see explain()."""

    def __init__(self, port: int):
        self.port = port
        super().__init__(
            f"anna-core: port {port} is already in use.\n"
            f"  Most likely another anna-core is already running — check the "
            f"tray, or: python app/main.py --doctor\n"
            f"  If something else owns the port, start with: "
            f"python -m app.core.daemon --port <other>\n"
            f"  Refusing to pick a different port silently: the UI would "
            f"connect to the wrong thing.")


def make_request_handler(api):
    """Bind the protocol's request channel to a JsApi instance through the
    whitelist. Args are keyword-only; unknown methods and unlisted names are
    the same error — the surface doesn't admit what else exists."""
    def handle(method: str, args: dict):
        if method not in REQUEST_METHODS:
            raise ValueError(f"unknown method {method!r}")
        fn = getattr(api, method)
        return fn(**args) if args else fn()
    return handle


class WsFanout(UIBridge):
    """The daemon's `ui`: the same controller-facing surface as UIBridge, but
    dispatch() broadcasts a protocol `event` frame to every authenticated
    client instead of evaluate_js. No window, no buffering — a client that
    connects later re-hydrates from `full_state`, and the durable trail is the
    event log, not a display queue."""

    def __init__(self, server: "CoreServer"):
        super().__init__()
        self._server = server

    def dispatch(self, type_: str, payload: dict = None) -> None:
        self._server.broadcast(type_, payload or {})

    def mark_ready(self) -> None:      # no page to wait for
        pass

    def destroy(self) -> None:         # no window to destroy
        pass

    def has_attached_ui(self) -> bool:
        """D-0.5: is a window actually connected right now? Drives whether a
        confirmation may be answered by voice (only when nobody can click)."""
        return any(getattr(c, "ready", False)
                   for c in list(self._server._clients))


class AuthThrottle:
    """Sliding-window rate limit on failed handshakes. Thread-safe; clock is
    injectable for tests."""

    def __init__(self, max_failures: int = AUTH_MAX_FAILURES,
                 window_s: float = AUTH_WINDOW_S, clock=time.monotonic):
        self.max_failures = int(max_failures)
        self.window_s = float(window_s)
        self._clock = clock
        self._failures = []
        self._lock = threading.Lock()

    def record_failure(self) -> None:
        with self._lock:
            self._failures.append(self._clock())

    def blocked(self) -> bool:
        cutoff = self._clock() - self.window_s
        with self._lock:
            self._failures = [t for t in self._failures if t >= cutoff]
            return len(self._failures) >= self.max_failures


class _Client:
    def __init__(self, ws, session, queue_max: int):
        self.ws = ws
        self.session = session
        self.queue = asyncio.Queue(maxsize=queue_max)
        self.send_lock = asyncio.Lock()
        self.sender = None
        # Fan-out eligibility. NOT the same as session.authenticated: the
        # session flips that inside receive() on a worker thread, so a
        # concurrent broadcast could beat the hello_ok onto the wire. A client
        # joins the fan-out only after its ack is actually sent.
        self.ready = False


def _is_loopback(peer) -> bool:
    """True only for a 127.0.0.0/8 or ::1 peer. Anything unknown is NOT
    loopback — fail closed."""
    try:
        host = str(peer[0])
    except Exception:
        return False
    return host == "::1" or host.startswith("127.")


class CoreServer:
    def __init__(self, *, token: str, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT, on_request=None, approvals=None,
                 eventlog=None, on_client_ready=None,
                 client_queue_max: int = CLIENT_QUEUE_MAX, throttle=None):
        self.host = host
        self.port = int(port)
        self.on_request = on_request
        self.approvals = approvals
        self.eventlog = eventlog
        self.on_client_ready = on_client_ready   # e.g. controller.send_full_state
        self.client_queue_max = int(client_queue_max)
        self.throttle = throttle or AuthThrottle()
        # Public and late-bindable: the daemon binds the port BEFORE loading
        # the token. An empty token fails closed (token_matches refuses it),
        # so the gap admits nobody rather than everybody.
        self.token = token
        self._clients = set()
        self._loop = None
        self._server = None
        self._stopped = None

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        from websockets.asyncio.server import serve
        self._loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        try:
            self._server = await serve(self._handler, self.host, self.port)
        except OSError as e:
            if getattr(e, "errno", None) in (48, 98, 10048) or "10048" in str(e):
                raise PortInUseError(self.port) from e
            raise
        # port=0 lets tests take an ephemeral port; report what we really got.
        self.port = self._server.sockets[0].getsockname()[1]
        devlog.log(f"[ipc] anna-core listening on ws://{self.host}:{self.port}")

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        await self._stopped.wait()
        self._server.close()
        await self._server.wait_closed()

    def stop(self) -> None:
        """Thread-safe shutdown request."""
        if self._loop is not None and self._stopped is not None:
            try:
                self._loop.call_soon_threadsafe(self._stopped.set)
            except RuntimeError:
                pass

    # ------------------------------------------------------------- broadcast
    def broadcast(self, event: str, data: dict) -> None:
        """Fan an event out to every authenticated client. Callable from ANY
        thread; never blocks, never raises — display traffic must never cost
        the pipeline a beat."""
        if self._loop is None:
            return
        try:
            frame = encode(make("event", {"event": str(event),
                                          "data": data or {}}))
        except Exception as e:
            devlog.warn(f"[ipc] unserializable event {event!r} dropped ({e})")
            return
        try:
            self._loop.call_soon_threadsafe(self._fanout, frame)
        except RuntimeError:
            pass                      # loop shut down; nothing to tell

    def _fanout(self, frame: str) -> None:
        for client in list(self._clients):
            if not client.ready:
                continue              # pre-hello_ok connections see nothing
            try:
                client.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # THE isolation rule: the laggard pays, nobody else does.
                self._evict(client, "send buffer overflow (client too slow)")

    def _evict(self, client, reason: str) -> None:
        self._clients.discard(client)
        devlog.warn(f"[ipc] disconnecting client ({reason})")
        self._audit_error(f"client disconnected: {reason}")
        if client.sender is not None:
            client.sender.cancel()
        try:
            asyncio.get_running_loop().create_task(
                client.ws.close(1013, "too slow"))
        except RuntimeError:
            pass                      # no loop (unit tests) — set is authoritative

    # ------------------------------------------------------------ connection
    async def _handler(self, ws) -> None:
        peer = getattr(ws, "remote_address", None)
        if not _is_loopback(peer):
            # Defence in depth: the bind already prevents this; if it ever
            # happens anyway, say nothing useful and close.
            self._audit_error("non-loopback connection refused", auth=True)
            await ws.close(1008, "loopback only")
            return
        if self.throttle.blocked():
            self._audit_error("connection refused: auth failures rate-limited",
                              auth=True)
            try:
                await ws.send(encode(make("auth_failed",
                                          {"reason": "rate-limited"})))
            finally:
                await ws.close(1013, "rate-limited")
            return

        session = ProtocolSession(token=self.token,
                                  on_request=self.on_request,
                                  approvals=self.approvals,
                                  eventlog=self.eventlog)
        client = _Client(ws, session, self.client_queue_max)
        self._clients.add(client)
        client.sender = asyncio.create_task(self._sender(client))
        loop = asyncio.get_running_loop()
        try:
            async for raw in ws:
                was_authed = session.authenticated
                # The session may run pipeline work (send_text): keep it off
                # the event loop so one client's request can't stall another's.
                replies = await loop.run_in_executor(None, session.receive, raw)
                for reply in replies:
                    async with client.send_lock:
                        await ws.send(encode(reply))
                if session.state == "closed":
                    if not was_authed:
                        self.throttle.record_failure()
                    break
                if session.authenticated and not was_authed:
                    client.ready = True     # hello_ok is on the wire — join
                    devlog.log(f"[ipc] client ready "
                               f"({session.client or 'unnamed'})")
                    if self.on_client_ready is not None:
                        await loop.run_in_executor(None, self.on_client_ready)
        except Exception as e:                     # abrupt disconnects included
            devlog.log(f"[ipc] connection ended: {' '.join(str(e).split())[:120]}")
        finally:
            self._clients.discard(client)
            if client.sender is not None:
                client.sender.cancel()
            try:
                await ws.close()
            except Exception:
                pass

    async def _sender(self, client) -> None:
        """One task per client, draining that client's own queue. A slow
        socket blocks only this task; the queue above it fills; _fanout
        evicts. Nothing upstream ever awaits."""
        while True:
            frame = await client.queue.get()
            async with client.send_lock:
                await client.ws.send(frame)

    # ---------------------------------------------------------------- audit
    def _audit_error(self, message: str, *, auth: bool = False) -> None:
        if self.eventlog is None:
            return
        try:
            self.eventlog.emit("error", source="ipc",
                               component="ipc-auth" if auth else "ipc",
                               message=message)
        except Exception:
            pass
