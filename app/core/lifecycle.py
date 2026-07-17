"""Graceful teardown for anna-core (Phase 0, commit 6).

Quit is a CONTRACT, not a best effort. `graceful_teardown` runs one ordered,
idempotent sequence and returns the steps it took, so the order can be asserted
in a test rather than hoped for:

  1. A pending confirmation is **cancelled by the shutdown** — logged, and
     NEVER executed. A half-answered card must not run just because the process
     is going down.
  2. The (metered) Gemini Live session is closed and the mic/camera/browser
     released — via `controller.shutdown()` — and the **session-end row is
     emitted BEFORE the log flushes**, so the audit trail records the billed
     session ending. Live before flush, always.
  3. The server stops accepting connections.
  4. The event log **flush verdict is honored**: if it cannot flush in time we
     say so (an audit log that silently drops its tail is worse than one that
     admits it), then close.
  5. The tray stops LAST — it is a convenience, never part of the safety trail.

Idempotent: guarded so a tray Quit and the `_main` finally can't double-run it.
"""

import threading

_LOCK = threading.Lock()


def graceful_teardown(controller, eventlog, *, server=None, tray=None,
                      reason="quit", flush_timeout=5.0, _guard=None):
    """Tear core down in the one right order. Returns the ordered step list
    (for tests). Safe to call more than once — the second call is a no-op."""
    guard = _guard if _guard is not None else _default_guard(eventlog)
    with _LOCK:
        if guard.is_set():
            return ["already-torn-down"]
        guard.set()

    steps = []

    def emit(*args, **kwargs):
        try:
            eventlog.emit(*args, **kwargs)
        except Exception:
            pass

    # 1) A pending confirmation dies WITH the shutdown — recorded, never run.
    pipeline = getattr(controller, "pipeline", None)
    pending = getattr(pipeline, "pending", None) if pipeline is not None else None
    if pending is not None:
        tool = getattr(getattr(pending, "plan", None), "tool_name", "")
        emit("confirmation", source="shutdown", outcome="cancelled-by-shutdown",
             tool=tool, channel="shutdown", reason="core is shutting down")
        try:
            # cancel_pending pops-and-discards; it does not execute the action.
            pipeline.cancel_pending(reason="shutdown")
        except Exception:
            pass
        steps.append("cancel_pending")

    # 2) Close Live + release mic/camera/browser, THEN record the session end —
    #    emitted before the flush below so the row is guaranteed to land.
    live_active = getattr(controller, "_live", None) is not None
    try:
        controller.shutdown()
    except Exception:
        pass
    steps.append("controller.shutdown")
    if live_active:
        emit("engine_state", source="shutdown", component="gemini_live",
             state="closed", reason=reason)
        steps.append("live_session_end")
    emit("engine_state", source="shutdown", component="core",
         state="stopped", reason=reason)

    # 3) Stop accepting connections before the log goes.
    if server is not None:
        try:
            server.stop()
        except Exception:
            pass
        steps.append("server.stop")

    # 4) Honor the flush verdict.
    flushed = False
    try:
        flushed = bool(eventlog.flush(timeout=flush_timeout))
    except Exception:
        flushed = False
    if not flushed:
        from app.agent.devlog import devlog
        devlog.warn(f"event log did not flush within {flush_timeout}s during "
                    "shutdown — the audit trail may be missing its tail.")
    steps.append(f"flush={flushed}")
    try:
        eventlog.close()
    except Exception:
        pass
    steps.append("eventlog.close")

    # 5) Tray last.
    if tray is not None:
        try:
            tray.stop()
        except Exception:
            pass
        steps.append("tray.stop")

    return steps


def _default_guard(eventlog):
    """Reuse one Event per eventlog so repeated calls with the same core are
    de-duped without the caller having to thread a flag through."""
    guard = getattr(eventlog, "_teardown_guard", None)
    if guard is None:
        guard = threading.Event()
        try:
            eventlog._teardown_guard = guard
        except Exception:
            pass
    return guard
