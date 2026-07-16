"""Per-install IPC token (Phase 0, commit 3).

The core's WebSocket server binds to 127.0.0.1 only, but localhost is not an
identity: any process on the machine can open a local socket. The token proves
a connecting client belongs to THIS install. It is minted on first use
(`secrets.token_hex`, 32 bytes of entropy), lives in the user-profile data dir
— which Windows ACLs to the user — and is compared with `hmac.compare_digest`
so a wrong token takes the same time however wrong it is.

It is a secret, and is treated like one (Protocol §4):
  * never in config.json — config gets shared in bug reports;
  * never logged — the event log's DENY_KEYS redacts token-shaped keys, and a
    test plants the real token and greps the whole log directory to prove it;
  * never echoed back in any protocol frame — hello_ok carries no token;
  * in .gitignore, so a checked-out repo can never ship one.
"""

import hmac
import secrets
from pathlib import Path

from app.config import DATA_DIR

TOKEN_PATH = DATA_DIR / "ipc_token"


def load_or_create_token(path=None) -> str:
    """The install's token, minting it on first use."""
    path = Path(path) if path else TOKEN_PATH
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except (FileNotFoundError, OSError):
        pass
    token = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


def token_matches(supplied, expected) -> bool:
    """Constant-time comparison. Empty/None on either side never matches —
    an install whose token failed to load must reject everyone, not admit
    everyone (fail closed)."""
    if not supplied or not expected:
        return False
    try:
        return hmac.compare_digest(str(supplied).encode("utf-8"),
                                   str(expected).encode("utf-8"))
    except Exception:
        return False
