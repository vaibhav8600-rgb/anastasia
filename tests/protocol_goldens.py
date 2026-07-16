"""Golden wire-format fixtures for the IPC protocol (Phase 0, commit 3).

One canonical frame per message type, encoded with pinned id/ts, committed
under tests/fixtures/protocol/. The test rebuilds each frame from the live
constructors and compares byte-for-byte, so ANY drift in the envelope — a
renamed key, a changed default, a reordered field, a new type without a
fixture — fails CI instead of surprising a connected client.

Deliberate protocol changes regenerate the fixtures:

    python -m tests.protocol_goldens
"""

from pathlib import Path

from app.core.protocol import PROTOCOL_VERSION, encode, make

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "protocol"
TS = "2026-07-16T12:00:00.000"          # pinned — goldens must be deterministic
FAKE_TOKEN = "0" * 64                    # shape of a real token, value obviously not


def build() -> dict:
    """Every message type, one canonical example each."""
    return {
        "hello": make(
            "hello", {"token": FAKE_TOKEN, "client": "anna-ui/1.0"},
            msg_id="c-hello", ts=TS),
        "hello_ok": make(
            "hello_ok", {"protocol": PROTOCOL_VERSION, "server": "anna-core"},
            re="c-hello", msg_id="s-hello-ok", ts=TS),
        "protocol_mismatch": make(
            "protocol_mismatch",
            {"reason": "version", "expected_version": PROTOCOL_VERSION,
             "got_version": 2},
            msg_id="s-mismatch", ts=TS),
        "auth_failed": make(
            "auth_failed", {"reason": "bad-token"}, msg_id="s-auth", ts=TS),
        "event": make(
            "event", {"event": "state", "data": {"state": "ready", "detail": ""}},
            msg_id="s-event", ts=TS),
        "request": make(
            "request", {"method": "get_history", "args": {"limit": 20}},
            msg_id="c-req", ts=TS),
        "response": make(
            "response", {"ok": True, "result": {"items": []}},
            re="c-req", msg_id="s-resp", ts=TS),
        "approval": make(
            "approval", {"confirmation_id": 3, "decision": "approve"},
            msg_id="c-appr", ts=TS),
        "approval_result": make(
            "approval_result",
            {"outcome": "applied", "confirmation_id": 3, "decision": "approve",
             "reason": "card 3 resolved"},
            re="c-appr", msg_id="s-appr", ts=TS),
        "error": make(
            "error", {"reason": "unknown-type"}, re="c-bad", msg_id="s-err", ts=TS),
    }


def regen() -> list:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, msg in sorted(build().items()):
        path = FIXTURE_DIR / f"{name}.json"
        path.write_text(encode(msg) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in regen():
        print(f"wrote {path}")
