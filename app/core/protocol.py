"""The core ⇄ UI wire protocol (Phase 0, commit 3).

Every frame is ONE JSON envelope:

    {"v": 1, "id": "<unique>", "ts": "<iso>", "type": "<name>", "payload": {…}}

Replies additionally carry `"re": "<id of the frame being answered>"`. Two
channels ride the envelope:

  * **events** (core → client): fire-and-forget state/display frames. The
    payload wraps the existing `ui.dispatch({type, payload})` vocabulary as
    `{"event": <name>, "data": {…}}` — the 30 UI event types keep their shape.
  * **request/response** (client → core): `request` carries
    `{"method": <name>, "args": {…}}`; the `response` answers with
    `{"ok": true, "result": …}` or `{"ok": false, "error": "<category>"}`.
    Errors are categories, never tracebacks — internals don't ride the wire.

**Approvals are deliberately NOT generic requests.** An `approval` frame must
name the confirmation it answers (`{"confirmation_id": N, "decision":
"approve"|"cancel"}`) and is routed through `app.core.approvals`, which refuses
to act without the id. See that module for why.

Version policy — mismatch is EXPLICIT, never tolerated: a frame whose `v` is
missing or not exactly PROTOCOL_VERSION gets a `protocol_mismatch` frame and a
clean close. There is no "best effort" mode; a client that speaks a different
protocol gets told so in the one frame both sides are guaranteed to understand,
then the conversation ends.

Auth policy — first frame or nothing: the first frame MUST be a `hello`
carrying the per-install token (`app.core.auth`). Until that succeeds, every
frame — malformed, wrong version, wrong type, wrong token — is answered with
its specific rejection and a close. Nothing else is processed pre-auth, and
the rejection never says how close the caller got.

This module is TRANSPORT-AGNOSTIC and imports no UI: the WebSocket server
(commit 4) feeds `ProtocolSession.receive()` raw frames and sends back whatever
it returns, closing when `session.state == "closed"`. That keeps every rule
here unit-testable without a socket.
"""

import json
import uuid
from datetime import datetime

from app.agent.devlog import devlog
from app.core.auth import token_matches

PROTOCOL_VERSION = 1

# Every type on the wire. Each has a golden fixture in tests/fixtures/protocol/
# — envelope drift fails CI, not a connected client.
MESSAGE_TYPES = frozenset({
    "hello",              # client → core: {token, client} — the ONLY pre-auth frame
    "hello_ok",           # core → client: auth accepted; server info, no token echo
    "protocol_mismatch",  # core → client: version wrong/missing → clean close
    "auth_failed",        # core → client: pre-auth rejection → clean close
    "event",              # core → client: {event, data} — wraps ui.dispatch types
    "request",            # client → core: {method, args}
    "response",           # core → client: re=<request id>, {ok, result|error}
    "approval",           # client → core: {confirmation_id, decision}
    "approval_result",    # core → client: re=<approval id>, {outcome, …}
    "error",              # core → client: post-auth bad frame; session stays open
})


# ------------------------------------------------------------------ envelope

def make(msg_type: str, payload: dict = None, *, re: str = None,
         msg_id: str = None, ts: str = None) -> dict:
    """Build an envelope. `msg_id`/`ts` are injectable for the golden tests."""
    msg = {"v": PROTOCOL_VERSION,
           "id": msg_id or uuid.uuid4().hex,
           "ts": ts or datetime.now().isoformat(timespec="milliseconds"),
           "type": str(msg_type),
           "payload": dict(payload or {})}
    if re is not None:
        msg["re"] = str(re)
    return msg


def encode(msg: dict) -> str:
    """Canonical wire form: sorted keys, no whitespace. Canonical so the golden
    fixtures are byte-comparable."""
    return json.dumps(msg, sort_keys=True, separators=(",", ":"), default=str)


def parse(raw):
    """(msg, None) for structurally-plausible JSON objects, (None, reason)
    otherwise. Only structure — version/shape checks are separate stages so the
    session can answer each failure with its specific frame."""
    try:
        msg = json.loads(raw)
    except Exception:
        return None, "malformed-frame"
    if not isinstance(msg, dict):
        return None, "malformed-frame"
    return msg, None


def validate(msg: dict):
    """Envelope-shape check for a version-verified frame. Returns an error
    category or None. Unknown types are rejected here — within one protocol
    version the vocabulary is closed."""
    if not isinstance(msg.get("id"), str) or not msg["id"]:
        return "bad-id"
    if not isinstance(msg.get("ts"), str) or not msg["ts"]:
        return "bad-ts"
    if msg.get("type") not in MESSAGE_TYPES:
        return "unknown-type"
    if not isinstance(msg.get("payload"), dict):
        return "bad-payload"
    return None


# ------------------------------------------------------------------- session

class ProtocolSession:
    """One client connection's state machine: awaiting_hello → ready | closed.

    Transport-agnostic and fail-closed. `receive(raw)` returns the frames to
    send back; after it runs, `state == "closed"` tells the transport to close
    the connection. Nothing a client sends can make the session skip a state.
    """

    def __init__(self, *, token: str, on_request=None, approvals=None,
                 eventlog=None):
        self.state = "awaiting_hello"
        self.client = ""                  # client-declared name, post-auth only
        self._token = token
        self._on_request = on_request     # callable(method, args) -> result
        self._approvals = approvals       # app.core.approvals.ApprovalRouter
        self._eventlog = eventlog

    @property
    def authenticated(self) -> bool:
        return self.state == "ready"

    # ------------------------------------------------------------ receiving
    def receive(self, raw) -> list:
        """Process one incoming frame; return the frames to send back."""
        if self.state == "closed":
            return []
        msg, err = parse(raw)
        if self.state == "awaiting_hello":
            return self._pre_auth(msg, err)
        return self._post_auth(msg, err)

    # Pre-auth: the ONLY acceptable frame is a well-formed, version-matched
    # hello with the right token. Each failure gets its specific frame, then
    # the conversation ends. Order matters: version is checked before anything
    # else because nothing later in the envelope can be trusted across a
    # version boundary.
    def _pre_auth(self, msg, err) -> list:
        if err:
            self._audit("pre-auth malformed frame")
            return self._close(make("protocol_mismatch", {
                "reason": "malformed-frame",
                "expected_version": PROTOCOL_VERSION}))
        got = msg.get("v")
        if got != PROTOCOL_VERSION:
            self._audit(f"protocol mismatch (got v={got!r})")
            return self._close(make("protocol_mismatch", {
                "reason": "version",
                "expected_version": PROTOCOL_VERSION,
                "got_version": got if isinstance(got, (int, str)) else None}))
        if validate(msg) is not None or msg["type"] != "hello":
            self._audit("pre-auth frame was not a hello")
            return self._close(make("auth_failed",
                                    {"reason": "not-authenticated"}))
        supplied = msg["payload"].get("token")
        if not token_matches(supplied, self._token):
            # Never say whether the token was absent, short or nearly right.
            self._audit("bad or missing token")
            return self._close(make("auth_failed", {"reason": "bad-token"}))
        self.state = "ready"
        self.client = str(msg["payload"].get("client") or "")[:80]
        self._audit(f"client authenticated ({self.client or 'unnamed'})",
                    ok=True)
        return [make("hello_ok",
                     {"protocol": PROTOCOL_VERSION, "server": "anna-core"},
                     re=msg["id"])]

    # Post-auth: a malformed or misshapen frame is a client BUG, not an
    # attacker — answer with an error and stay open (a UI typo must not kill
    # the session). A version change mid-session, though, means we no longer
    # know what the bytes mean: explicit mismatch, clean close, fail closed.
    def _post_auth(self, msg, err) -> list:
        if err:
            return [make("error", {"reason": err})]
        if msg.get("v") != PROTOCOL_VERSION:
            self._audit(f"mid-session version change (got v={msg.get('v')!r})")
            return self._close(make("protocol_mismatch", {
                "reason": "version",
                "expected_version": PROTOCOL_VERSION,
                "got_version": msg.get("v") if isinstance(msg.get("v"), (int, str)) else None}))
        shape = validate(msg)
        reply_to = msg.get("id") if isinstance(msg.get("id"), str) else None
        if shape is not None:
            return [make("error", {"reason": shape}, re=reply_to)]
        if msg["type"] == "request":
            return [self._handle_request(msg)]
        if msg["type"] == "approval":
            return [self._handle_approval(msg)]
        # Clients don't send events/responses/hellos once authenticated.
        return [make("error", {"reason": f"unexpected-type:{msg['type']}"},
                     re=msg["id"])]

    # ------------------------------------------------------------- handlers
    def _handle_request(self, msg) -> dict:
        method = str(msg["payload"].get("method") or "")
        args = msg["payload"].get("args")
        if self._on_request is None:
            return make("response", {"ok": False, "error": "no-handler"},
                        re=msg["id"])
        try:
            result = self._on_request(method,
                                      args if isinstance(args, dict) else {})
            return make("response", {"ok": True, "result": result},
                        re=msg["id"])
        except Exception as e:
            devlog.exception(e, context=f"ipc request {method!r}")
            # Category only — a traceback is an internals leak.
            return make("response", {"ok": False, "error": "request-failed"},
                        re=msg["id"])

    def _handle_approval(self, msg) -> dict:
        if self._approvals is None:
            return make("approval_result",
                        {"outcome": "rejected-invalid",
                         "reason": "no-approval-router"}, re=msg["id"])
        result = self._approvals.resolve(
            msg["payload"].get("confirmation_id"),
            msg["payload"].get("decision"))
        return make("approval_result", dict(result), re=msg["id"])

    # -------------------------------------------------------------- plumbing
    def _close(self, frame: dict) -> list:
        self.state = "closed"
        return [frame]

    def _audit(self, message: str, *, ok: bool = False) -> None:
        """Devlog + event log. Never includes token material — `message` is
        always our own words, never client bytes."""
        (devlog.log if ok else devlog.warn)(f"[ipc] {message}")
        if self._eventlog is not None:
            try:
                self._eventlog.emit("error" if not ok else "engine_state",
                                    source="ipc",
                                    **({"component": "ipc", "message": message}
                                       if not ok else
                                       {"component": "ipc", "state": "ready",
                                        "reason": message}))
            except Exception:
                pass
