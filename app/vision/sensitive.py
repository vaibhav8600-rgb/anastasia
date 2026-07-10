"""Sensitive-content heuristics (11B.4).

If a frame looks like it holds credentials, keys, or banking/payment data,
Anna refuses to analyze it AT ALL — locally or in the cloud — until the user
explicitly says to. Cheap keyword/shape matching on the locally-extracted
text; deliberately biased toward false positives, because the cost of a false
positive is one extra question and the cost of a false negative is a leaked
password.

Detecting a masked password FIELD is UIA's job (11C, principle 8) — vision
alone can't tell a password box from any other box, so we never try.
"""

import re

# Phrases that mean "this screen is about secrets or money".
SENSITIVE_KEYWORDS = (
    "password", "passwd", "passphrase", "pass phrase",
    "api key", "api-key", "apikey", "secret key", "client secret",
    "private key", "seed phrase", "recovery phrase", "mnemonic",
    "access token", "bearer token", "auth token", "session token",
    "account number", "routing number", "sort code", "iban", "swift code",
    "card number", "credit card", "debit card", "cvv", "cvc",
    "security code", "expiry date", "social security", "ssn",
    "one time password", "otp", "verification code", "2fa code",
    "pin code", "net banking", "netbanking", "wire transfer",
)

# Shapes of real secrets that shouldn't be shipped anywhere.
SENSITIVE_PATTERNS = (
    (r"\bsk-[A-Za-z0-9_\-]{16,}", "an OpenAI-style secret key"),
    (r"\bgsk_[A-Za-z0-9]{20,}", "a Groq API key"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "a Google API key"),
    (r"\bghp_[A-Za-z0-9]{20,}", "a GitHub token"),
    (r"\bxox[baprs]-[A-Za-z0-9\-]{10,}", "a Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key block"),
    (r"\b(?:\d[ \-]?){15,18}\b", "something shaped like a card number"),
    (r"\b\d{3}[ \-]?\d{2}[ \-]?\d{4}\b", "something shaped like an SSN"),
    # A row of masking bullets/asterisks: a rendered password field.
    (r"[•●\*]{6,}", "a masked password field"),
)


def scan_text(text: str) -> list:
    """Reasons this text looks sensitive. Empty list = looks fine."""
    if not text:
        return []
    lowered = text.lower()
    reasons = []
    for word in SENSITIVE_KEYWORDS:
        if word in lowered:
            reasons.append(f"the words “{word}”")
            break            # one keyword reason is enough to ask
    for pattern, label in SENSITIVE_PATTERNS:
        if re.search(pattern, text):
            reasons.append(label)
    return reasons


def looks_sensitive(text: str) -> tuple[bool, str]:
    """(sensitive, human reason). The reason is spoken to the user, so it
    names WHAT was spotted — never the secret itself."""
    reasons = scan_text(text)
    if not reasons:
        return False, ""
    if len(reasons) == 1:
        return True, f"I can see {reasons[0]} on that screen"
    return True, ("I can see " + ", ".join(reasons[:-1])
                  + f" and {reasons[-1]} on that screen")
