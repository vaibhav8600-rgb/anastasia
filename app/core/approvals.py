"""Approval routing for IPC clients (Phase 0, commit 3).

An approval that arrives over the wire MUST name the confirmation it answers.
The window's buttons can get away with "approve whatever is up" because the
card is literally under the user's cursor when they click; a remote client has
latency, retries and reconnects, and "whatever is up" can change between the
user's eyes and their packet. The canonical hazard: card A expires, card B
arms, and a stale "approve" for A lands — it must be a logged no-op, never a
fresh approval of B.

This router adds NO second execution path (Protocol §4). It delegates to the
same `pipeline.approve_pending` / `cancel_pending` the window buttons call,
which pop the card atomically by id (`ConfirmationManager.take` — first wins,
a mismatched id takes nothing). All the router does is refuse to delegate
without an id, report what actually happened, and write the no-ops down:

    applied            — the named card was resolved by this decision
    rejected-stale     — a DIFFERENT card is pending; the named one is gone
    rejected-unknown   — nothing pending under that id (expired, already
                         resolved, duplicate answer, or lost a first-wins race)
    rejected-invalid   — missing/nonsense id or decision; nothing consulted
"""

from app.agent.devlog import devlog

VALID_DECISIONS = ("approve", "cancel")


class ApprovalRouter:
    def __init__(self, manager, approve, cancel, *, eventlog=None):
        """`manager` is the ConfirmationManager (read-only here, for honest
        outcome labels); `approve`/`cancel` are the pipeline's own methods —
        callable(action_id=…) -> bool, True when the id landed."""
        self.manager = manager
        self._approve = approve
        self._cancel = cancel
        self._eventlog = eventlog

    def resolve(self, confirmation_id, decision, *, channel: str = "ipc") -> dict:
        decision = str(decision or "").strip().lower()
        if decision not in VALID_DECISIONS:
            return self._done(confirmation_id, decision, "rejected-invalid",
                              "unknown decision", channel)
        # bool is an int subclass; `approve card true` is nonsense, not id 1.
        if not isinstance(confirmation_id, int) or isinstance(confirmation_id, bool):
            return self._done(confirmation_id, decision, "rejected-invalid",
                              "approval did not reference a confirmation id",
                              channel)

        # Best-effort snapshot for the audit line; correctness never depends on
        # it — take(action_id) inside approve/cancel is the atomic gate.
        before = self.manager.pending
        tool = (getattr(getattr(before, "plan", None), "tool_name", "")
                if before is not None else "")

        landed = bool((self._approve if decision == "approve"
                       else self._cancel)(action_id=confirmation_id))
        if landed:
            return self._done(confirmation_id, decision, "applied",
                              f"card {confirmation_id} resolved", channel,
                              tool=tool)

        now = self.manager.pending
        if now is not None and now.id != confirmation_id:
            # THE hazard case: the named card is gone and a different one is
            # armed. The pending card was not touched — say so explicitly.
            return self._done(confirmation_id, decision, "rejected-stale",
                              f"card {confirmation_id} is gone; card {now.id} "
                              "is pending and was not touched", channel)
        return self._done(confirmation_id, decision, "rejected-unknown",
                          f"no pending card {confirmation_id} (expired, "
                          "already resolved, or duplicate)", channel)

    def _done(self, confirmation_id, decision, outcome, reason, channel,
              *, tool: str = "") -> dict:
        line = (f"[approval:{channel}] {decision} for card {confirmation_id!r}"
                f" -> {outcome} ({reason})")
        (devlog.log if outcome == "applied" else devlog.warn)(line)
        if self._eventlog is not None:
            try:
                self._eventlog.emit("confirmation", source=channel,
                                    outcome=outcome, tool=tool,
                                    channel=channel, reason=reason)
            except Exception:
                pass
        return {"outcome": outcome, "confirmation_id": confirmation_id,
                "decision": decision, "reason": reason}
