"""Confirmation state machine + voice approval parsing (Phase 11A).

The safety backbone for everything that clicks, types or sends on the user's
behalf. It owns exactly three things:

  1. **State.** A dedicated machine, separate from the pipeline's busy flag:
     idle -> pending_confirmation -> listening_for_confirmation -> approved |
     cancelled | expired -> idle. Exactly ONE confirmation may be pending;
     a second risky action is politely DEFERRED, never silently overwritten.
  2. **Expiry.** A pending confirmation auto-cancels after `timeout_s`
     (default 30s) so nothing sits armed forever.
  3. **Phrase parsing.** Only when something is actually pending — a random
     "yes" with nothing pending returns NONE and falls through to normal
     chat/command routing.

It deliberately does NOT execute anything. `handle_utterance()` is a pure
parse + read of state; the caller (CommandPipeline) decides what to run.
That keeps the one execution path (safety validator -> executor) intact.

IMPORTANT — parse the RAW utterance, never the normalized one. The command
normalizer strips a leading wake word, so `normalize_command("anna approve")`
yields "approve": feeding it here would silently downgrade the STRONG phrase
to a casual one on exactly the actions that most need the strong phrase.
"""

import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.agent.devlog import devlog

DEFAULT_TIMEOUT_S = 30.0


class ConfirmState(str, Enum):
    IDLE = "idle"
    PENDING = "pending_confirmation"
    LISTENING = "listening_for_confirmation"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Outcome(str, Enum):
    NONE = "none"                  # nothing pending -> route normally
    APPROVED = "approved"
    CANCELLED = "cancelled"
    NEEDS_STRONG = "needs_strong"  # casual "yes" on a destructive-tier action
    REPEAT = "repeat"              # "what are you asking?"
    DETAILS = "details"            # "show details"
    UNCLEAR = "unclear"            # ambiguous -> ask again, never execute


# ---- phrases ------------------------------------------------------------------
# Checked in this order: strong approve wins before the casual sets, so
# "anna approve" is never mistaken for a bare "approve".
STRONG_APPROVE = {"anna approve", "i approve", "confirm action",
                  "anastasia approve", "approve action"}

STANDARD_APPROVE = {"approve", "approved", "yes", "yes anna", "yeah", "yep",
                    "yup", "haan", "do it", "go ahead", "confirm", "send it",
                    "run it", "okay do it", "ok do it", "yes please"}

STANDARD_CANCEL = {"cancel", "no", "nope", "stop", "don't do it", "dont do it",
                   "abort", "leave it", "not now", "never mind", "nevermind",
                   "no thanks", "don't", "dont"}

REPEAT_PHRASES = {"what are you asking", "what are you asking me", "repeat",
                  "repeat that", "say that again", "what was that",
                  "come again", "what did you say"}

DETAILS_PHRASES = {"show details", "details", "show me the details",
                   "show the details", "more details", "expand",
                   "show me more"}

# Leading/trailing conversational filler stripped before the CASUAL sets are
# consulted (never before the strong set — "anna" must survive there).
_FILLERS = {"okay", "ok", "um", "uh", "please", "hey", "so", "well", "just"}

# Tools whose confirmation always demands the strong phrase, independent of
# whatever risk the calling plan claimed for itself (11A.2). `run_terminal`
# is medium-risk to the validator but destructive-tier to a human.
STRONG_TOOLS = {"run_terminal", "delete_files", "send_email", "send_message",
                "submit_form", "move_files", "rename_files"}


def _normalize(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)      # drop punctuation, keep apostrophes
    return re.sub(r"\s+", " ", t).strip()


def _strip_fillers(text: str) -> str:
    words = text.split()
    while words and words[0] in _FILLERS:
        words.pop(0)
    while words and words[-1] in _FILLERS:
        words.pop()
    return " ".join(words)


def requires_strong_approval(plan, safety) -> bool:
    """Destructive-tier actions need `anna approve` / `i approve`, never a
    casual "yes". Reads the SAFETY result (not the plan's self-declared risk)
    plus a hardcoded tool list, so an under-stated plan cannot dodge it.
    `destructive_target` is set by the validator in 11C for resolved clicks
    whose accessible name matches Send/Submit/Pay/Delete/…"""
    tool = (getattr(plan, "tool_name", "") or getattr(plan, "intent", "") or "")
    if tool.strip().lower() in STRONG_TOOLS:
        return True
    if str(getattr(safety, "risk_level", "")).lower() == "high":
        return True
    return bool(getattr(safety, "destructive_target", False))


@dataclass
class PendingAction:
    id: int
    plan: object
    safety: object
    transcript: str
    kind: str = "safety"          # "safety" | "fuzzy" | "live_tool"
    message: str = ""
    # kind="live_tool" (10C): approve/cancel/timeout fires callback(approved)
    # exactly once instead of executing through the pipeline — the Gemini
    # Live tool bridge owns execution and the tool_response.
    callback: object = None
    strong_required: bool = False
    details: dict = field(default_factory=dict)   # target app, text, screenshot


@dataclass
class RequestResult:
    accepted: bool
    action_id: Optional[int] = None
    deferred: bool = False        # something else is already pending
    reason: str = ""


class ConfirmationManager:
    """Owns pending-confirmation state, expiry and phrase parsing. Thread-safe
    for the ways it's used (GUI thread, timer thread, worker threads)."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 on_expire=None, timer_factory=threading.Timer):
        self.timeout_s = float(timeout_s)
        self.on_expire = on_expire            # callback(pending)
        self._timer_factory = timer_factory
        self._state = ConfirmState.IDLE
        self._pending: Optional[PendingAction] = None
        self._timer = None
        self._counter = 0
        self._lock = threading.RLock()
        self.reprompts = 0                    # ambiguous replies for this card

    # ------------------------------------------------------------- state
    @property
    def state(self) -> ConfirmState:
        with self._lock:
            return self._state

    @property
    def pending(self) -> Optional[PendingAction]:
        with self._lock:
            return self._pending

    @pending.setter
    def pending(self, value) -> None:
        # Back-compat escape hatch (older tests poke pipeline.pending directly).
        with self._lock:
            self._pending = value
            self._state = (ConfirmState.PENDING if value is not None
                           else ConfirmState.IDLE)

    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    # ----------------------------------------------------------- request
    def request(self, plan, safety, transcript: str, *, kind: str = "safety",
                message: str = "", timeout_s: float = None,
                callback=None, strong_required: bool = None,
                details: dict = None) -> RequestResult:
        """Arm a confirmation. Refuses (deferred) if one is already pending —
        a risky action must never silently replace another risky action."""
        with self._lock:
            if self._pending is not None:
                devlog.warn(f"Confirmation deferred: '{transcript}' — "
                            f"'{self._pending.transcript}' is still pending.")
                return RequestResult(accepted=False, deferred=True,
                                     reason="another confirmation is pending")
            if strong_required is None:
                strong_required = requires_strong_approval(plan, safety)
            self._counter += 1
            action_id = self._counter
            self._pending = PendingAction(
                action_id, plan, safety, transcript, kind=kind,
                message=message, callback=callback,
                strong_required=bool(strong_required),
                details=dict(details or {}))
            self._state = ConfirmState.PENDING
            self.reprompts = 0
            timeout = self.timeout_s if timeout_s is None else float(timeout_s)
            self._arm_timer(action_id, timeout)
        return RequestResult(accepted=True, action_id=action_id)

    def _arm_timer(self, action_id: int, timeout: float) -> None:
        self._cancel_timer()
        if timeout <= 0:
            return
        timer = self._timer_factory(timeout, self._expire, (action_id,))
        timer.daemon = True
        timer.start()
        self._timer = timer

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _expire(self, action_id: int) -> None:
        with self._lock:
            if self._pending is None or self._pending.id != action_id:
                return                    # already resolved — stale timer
            pending = self._pending
            self._state = ConfirmState.EXPIRED
        if self.on_expire is not None:
            try:
                self.on_expire(pending)
            except Exception as e:
                devlog.exception(e, context="confirmation expiry")

    def begin_listening(self) -> None:
        """The card is up and the summary spoken — voice or click, whichever
        arrives first, wins."""
        with self._lock:
            if self._state is ConfirmState.PENDING:
                self._state = ConfirmState.LISTENING

    def take(self, action_id=None, *, outcome: ConfirmState = None):
        """Pop the pending action; None if there is none or the id is stale
        (a click on a card that already timed out, or the loser of a
        voice/click race — first wins, the second is a harmless no-op)."""
        with self._lock:
            if self._pending is None:
                return None
            if action_id is not None and self._pending.id != action_id:
                return None
            pending, self._pending = self._pending, None
            self._cancel_timer()
            self._state = ConfirmState.IDLE
            self.reprompts = 0
        if outcome is not None:
            devlog.log(f"Confirmation {outcome.value}: {pending.transcript!r}")
        return pending

    # --------------------------------------------------- phrase parsing
    def handle_utterance(self, raw_text: str) -> Outcome:
        """Classify an utterance heard while a confirmation is pending.

        Pure: never executes and never resolves the pending action — the
        caller acts on the Outcome. Pass the RAW text (see module docstring).
        """
        with self._lock:
            pending = self._pending
        if pending is None:
            return Outcome.NONE           # 11A.1 gate: no pending, no parsing

        text = _normalize(raw_text)
        if not text:
            return Outcome.UNCLEAR

        # Strong first: "anna approve" must never be read as a bare "approve".
        if text in STRONG_APPROVE:
            return Outcome.APPROVED
        if text in REPEAT_PHRASES:
            return Outcome.REPEAT
        if text in DETAILS_PHRASES:
            return Outcome.DETAILS

        casual = _strip_fillers(text)
        # Cancelling is always allowed with a casual phrase — the safe
        # direction never needs a stronger word.
        if casual in STANDARD_CANCEL or text in STANDARD_CANCEL:
            return Outcome.CANCELLED
        if casual in STRONG_APPROVE:
            return Outcome.APPROVED
        if casual in REPEAT_PHRASES:
            return Outcome.REPEAT
        if casual in DETAILS_PHRASES:
            return Outcome.DETAILS
        if casual in STANDARD_APPROVE:
            if pending.strong_required:
                return Outcome.NEEDS_STRONG
            return Outcome.APPROVED

        with self._lock:
            self.reprompts += 1
        return Outcome.UNCLEAR

    # ------------------------------------------------------------ views
    def summary(self) -> str:
        """Short line Anna speaks (and repeats on request)."""
        pending = self.pending
        if pending is None:
            return ""
        base = (pending.message or getattr(pending.plan, "confirmation_message", "")
                or getattr(pending.safety, "reason", "")
                or "This one needs your OK.")
        if pending.strong_required:
            return f"{base} Say 'Anna approve' to go ahead, or 'cancel'."
        return f"{base} Say 'approve' or 'cancel'."

    def payload(self) -> Optional[dict]:
        """Serializable view of the pending confirmation (confirm card)."""
        pending = self.pending
        if pending is None:
            return None
        return {"id": pending.id, "transcript": pending.transcript,
                "tool": pending.plan.tool_name,
                "arguments": pending.plan.arguments,
                "risk": pending.safety.risk_level,
                "kind": pending.kind,
                "strong_required": pending.strong_required,
                "details": pending.details,
                "message": pending.message or pending.plan.confirmation_message
                           or pending.safety.reason}
