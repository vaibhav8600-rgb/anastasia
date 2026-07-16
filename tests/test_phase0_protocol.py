"""Phase 0 commit 3: the wire protocol — versioned envelope, auth-first
handshake, id-referencing approvals, and golden fixtures.

The four pinned requirements:
  1. Version mismatch is EXPLICIT: unknown/missing `v` → protocol_mismatch +
     clean close, never silent tolerance. A correct token cannot outrank a
     wrong version.
  2. Auth is the FIRST frame: hello + per-install token, everything pre-auth
     rejected and closed. The token is never echoed, never logged.
  3. Approvals over IPC must reference a confirmation id. Duplicate, stale and
     unknown ids are logged no-ops — an approve for expired card A never
     touches pending card B. Tested through the REAL pipeline + manager.
  4. Golden files pin the wire format of every message type — envelope drift
     fails CI, not a connected client.
"""

import json

import pytest

from app.core.auth import load_or_create_token, token_matches
from app.core.approvals import ApprovalRouter
from app.core.eventlog import EventLog
from app.core.protocol import (MESSAGE_TYPES, PROTOCOL_VERSION,
                               ProtocolSession, encode, make, parse, validate)
from tests.protocol_goldens import FIXTURE_DIR, build
from tests.test_phase11a import make_pipeline, pending_close_chrome

TOKEN = "a1" * 32


def hello_frame(token=TOKEN, **payload_extra):
    return encode(make("hello", {"token": token, "client": "test-ui",
                                 **payload_extra}))


def authed_session(**kw):
    session = ProtocolSession(token=TOKEN, **kw)
    replies = session.receive(hello_frame())
    assert replies[0]["type"] == "hello_ok" and session.authenticated
    return session


def all_bytes(directory) -> str:
    """Every byte in every file — the WAL lesson: never grep one file."""
    out = []
    for f in directory.iterdir():
        if f.is_file():
            out.append(f.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(out)


# ---- 4. golden fixtures: envelope drift fails CI ------------------------------

def test_goldens_cover_every_message_type():
    """A NEW message type without a committed golden is itself drift."""
    assert set(build()) == set(MESSAGE_TYPES)


def test_wire_format_matches_goldens_byte_for_byte():
    for name, msg in build().items():
        fixture = FIXTURE_DIR / f"{name}.json"
        assert fixture.exists(), (
            f"missing golden for {name!r} — python -m tests.protocol_goldens")
        assert encode(msg) == fixture.read_text(encoding="utf-8").strip(), (
            f"envelope drift in {name!r}. If the change is deliberate, "
            "regenerate: python -m tests.protocol_goldens")


def test_goldens_parse_and_validate_cleanly():
    for name in MESSAGE_TYPES:
        raw = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
        msg, err = parse(raw)
        assert err is None and msg["v"] == PROTOCOL_VERSION
        assert validate(msg) is None, f"{name}: {validate(msg)}"


# ---- 1. version mismatch: explicit, never tolerated ----------------------------

def test_missing_version_is_explicit_mismatch_and_close():
    session = ProtocolSession(token=TOKEN)
    msg = make("hello", {"token": TOKEN})
    del msg["v"]
    replies = session.receive(encode(msg))
    assert replies[0]["type"] == "protocol_mismatch"
    assert replies[0]["payload"]["expected_version"] == PROTOCOL_VERSION
    assert session.state == "closed"
    assert session.receive(hello_frame()) == []      # closed means closed


def test_unknown_version_names_both_versions_then_closes():
    session = ProtocolSession(token=TOKEN)
    msg = make("hello", {"token": TOKEN})
    msg["v"] = 2
    replies = session.receive(encode(msg))
    payload = replies[0]["payload"]
    assert replies[0]["type"] == "protocol_mismatch"
    assert payload["expected_version"] == PROTOCOL_VERSION
    assert payload["got_version"] == 2
    assert session.state == "closed"


def test_correct_token_cannot_outrank_a_wrong_version():
    """Version is checked before auth: across a version boundary nothing in
    the envelope can be trusted, including where the token would be."""
    session = ProtocolSession(token=TOKEN)
    msg = make("hello", {"token": TOKEN})
    msg["v"] = "1"                                    # right number, wrong type
    replies = session.receive(encode(msg))
    assert replies[0]["type"] == "protocol_mismatch"
    assert not session.authenticated


def test_mid_session_version_change_closes():
    session = authed_session()
    msg = make("request", {"method": "ping", "args": {}})
    msg["v"] = 99
    replies = session.receive(encode(msg))
    assert replies[0]["type"] == "protocol_mismatch"
    assert session.state == "closed"


# ---- 2. auth handshake: first frame or nothing ---------------------------------

def test_handshake_succeeds_and_never_echoes_the_token():
    session = ProtocolSession(token=TOKEN)
    replies = session.receive(hello_frame())
    reply = replies[0]
    assert reply["type"] == "hello_ok"
    assert reply["payload"]["protocol"] == PROTOCOL_VERSION
    assert session.authenticated and session.client == "test-ui"
    assert TOKEN not in encode(reply)


def test_everything_pre_auth_is_rejected_and_closed():
    """A perfectly well-formed request is still not a hello."""
    session = ProtocolSession(token=TOKEN)
    replies = session.receive(
        encode(make("request", {"method": "get_history", "args": {}})))
    assert replies[0]["type"] == "auth_failed"
    assert session.state == "closed"


def test_bad_and_missing_tokens_are_rejected_identically():
    for bad in ("b2" * 32, "", None):
        session = ProtocolSession(token=TOKEN)
        payload = {"client": "test-ui"} if bad is None else {"token": bad,
                                                             "client": "test-ui"}
        replies = session.receive(encode(make("hello", payload)))
        assert replies[0]["type"] == "auth_failed"
        assert replies[0]["payload"]["reason"] == "bad-token"
        assert TOKEN not in encode(replies[0])        # no oracle
        assert session.state == "closed"


def test_malformed_pre_auth_frame_closes():
    session = ProtocolSession(token=TOKEN)
    replies = session.receive("this is not json {")
    assert replies[0]["type"] == "protocol_mismatch"
    assert replies[0]["payload"]["reason"] == "malformed-frame"
    assert session.state == "closed"


def test_malformed_post_auth_frame_is_an_error_but_stays_open():
    """Post-auth garbage is a client bug, not an attacker — the session
    answers and survives."""
    session = authed_session()
    replies = session.receive("oops{")
    assert replies[0]["type"] == "error"
    assert session.state == "ready"


def test_request_response_channel_round_trip():
    seen = []

    def handler(method, args):
        seen.append((method, args))
        return {"echo": method}

    session = authed_session(on_request=handler)
    request = make("request", {"method": "get_history", "args": {"limit": 5}})
    replies = session.receive(encode(request))
    reply = replies[0]
    assert reply["type"] == "response" and reply["re"] == request["id"]
    assert reply["payload"] == {"ok": True, "result": {"echo": "get_history"}}
    assert seen == [("get_history", {"limit": 5})]


def test_request_failure_is_a_category_never_a_traceback():
    def handler(method, args):
        raise ValueError("secret internal path C:/Users/x/.ssh/id_rsa")

    session = authed_session(on_request=handler)
    replies = session.receive(encode(make("request", {"method": "boom"})))
    encoded = encode(replies[0])
    assert replies[0]["payload"] == {"ok": False, "error": "request-failed"}
    assert "secret internal" not in encoded and "id_rsa" not in encoded


def test_unknown_type_post_auth_is_an_error_not_a_close():
    session = authed_session()
    replies = session.receive(encode(make("hello_ok", {})))   # clients don't send this
    assert replies[0]["type"] == "error"
    assert replies[0]["payload"]["reason"].startswith("unexpected-type")
    assert session.state == "ready"


# ---- 3. approvals must reference a confirmation id ------------------------------

def router_for(pipeline, eventlog=None):
    return ApprovalRouter(pipeline.confirm, pipeline.approve_pending,
                          pipeline.cancel_pending, eventlog=eventlog)


def test_approve_for_expired_card_a_never_touches_pending_card_b(tmp_path):
    """THE pinned scenario. Card A expires; card B arms; a stale 'approve A'
    arrives over IPC. Nothing may run, card B must stay pending, and the no-op
    must be written down."""
    pipeline, ui, speech, agent = pending_close_chrome()
    card_a = pipeline.pending.id
    # Expire A exactly the way the timer does (see CommandPipeline.__init__).
    pipeline.cancel_pending(reason="timeout", action_id=card_a)
    assert pipeline.pending is None

    pipeline.submit("close chrome", source="voice")             # card B arms
    card_b = pipeline.pending.id
    assert card_b != card_a

    log = EventLog(tmp_path / "events.sqlite")
    result = router_for(pipeline, eventlog=log).resolve(card_a, "approve")

    assert result["outcome"] == "rejected-stale"
    assert pipeline.pending is not None and pipeline.pending.id == card_b
    assert agent.executed == []                     # NOTHING ran
    assert log.flush(timeout=10)
    rows = log.recent(event_type="confirmation")
    assert rows and rows[0]["outcome"] == "rejected-stale"
    assert rows[0]["payload"]["channel"] == "ipc"
    log.close()


def test_named_approval_lands_and_duplicate_is_a_noop():
    pipeline, ui, speech, agent = pending_close_chrome()
    card = pipeline.pending.id
    router = router_for(pipeline)

    assert router.resolve(card, "approve")["outcome"] == "applied"
    ran = len(agent.executed)
    assert ran == 1 and pipeline.pending is None

    duplicate = router.resolve(card, "approve")     # answer arrives twice
    assert duplicate["outcome"] == "rejected-unknown"
    assert len(agent.executed) == ran               # did not run twice


def test_unknown_id_and_invalid_shapes_are_rejected_without_consulting_state():
    pipeline, ui, speech, agent = pending_close_chrome()
    card = pipeline.pending.id
    router = router_for(pipeline)

    assert router.resolve(9999, "approve")["outcome"] == "rejected-stale"
    assert router.resolve(None, "approve")["outcome"] == "rejected-invalid"
    assert router.resolve("1", "approve")["outcome"] == "rejected-invalid"
    assert router.resolve(True, "approve")["outcome"] == "rejected-invalid"
    assert router.resolve(card, "sudo")["outcome"] == "rejected-invalid"
    # Through all of it, the pending card was never touched.
    assert pipeline.pending is not None and pipeline.pending.id == card
    assert agent.executed == []


def test_cancel_by_id_works_over_ipc():
    pipeline, ui, speech, agent = pending_close_chrome()
    card = pipeline.pending.id
    result = router_for(pipeline).resolve(card, "cancel")
    assert result["outcome"] == "applied"
    assert pipeline.pending is None and agent.executed == []


def test_approval_frame_through_the_full_session():
    """Wire → session → router → real pipeline, end to end."""
    pipeline, ui, speech, agent = pending_close_chrome()
    card = pipeline.pending.id
    session = authed_session(approvals=router_for(pipeline))

    frame = make("approval", {"confirmation_id": card, "decision": "approve"})
    replies = session.receive(encode(frame))
    reply = replies[0]
    assert reply["type"] == "approval_result" and reply["re"] == frame["id"]
    assert reply["payload"]["outcome"] == "applied"
    assert len(agent.executed) == 1

    # An approval that names no card is refused without touching anything.
    replies = session.receive(encode(make("approval", {"decision": "approve"})))
    assert replies[0]["payload"]["outcome"] == "rejected-invalid"


# ---- the token is a secret --------------------------------------------------

def test_token_is_created_once_and_stable(tmp_path):
    path = tmp_path / "ipc_token"
    first = load_or_create_token(path)
    second = load_or_create_token(path)
    assert first == second and len(first) == 64
    int(first, 16)                                   # 32 bytes of hex


def test_empty_tokens_never_match():
    """Fail closed: an install whose token failed to load rejects everyone."""
    assert not token_matches("", "")
    assert not token_matches(None, TOKEN)
    assert not token_matches(TOKEN, None)
    assert not token_matches(TOKEN, "")
    assert token_matches(TOKEN, TOKEN)


def test_token_never_reaches_the_event_log(tmp_path):
    """Plant the REAL token in every frame a session audits — failed hellos,
    successful hello, post-auth garbage — then grep every byte the log wrote."""
    log = EventLog(tmp_path / "events.sqlite")
    session = ProtocolSession(token=TOKEN, eventlog=log)
    session.receive(encode(make("hello", {"token": "wrong-" + TOKEN})))
    session = ProtocolSession(token=TOKEN, eventlog=log)
    session.receive(hello_frame())                   # success is audited too
    session.receive(f'{{"broken": "{TOKEN}"')        # malformed, token inside
    assert log.flush(timeout=10)
    log.close()
    body = all_bytes(tmp_path)
    assert "ipc" in body                             # the audits DID land
    assert TOKEN not in body, "the IPC token leaked into the event log"
