"""Watcher base class (Phase 1A commit 2).

Every watcher inherits: a supervised thread, its own circuit breaker (throws
3× → benched, logged, doctor-visible — never crashes core), per-event rate
limiting, threshold **hysteresis** (fire on crossing, re-arm only past a
recovery margin — boundary-hover must never spam the feed), a `simulated`
tag on emitted events, and a `test_emit` hook.

Watchers PUBLISH; they never consume, never touch a tool, never speak. A
proactive event is data on the bus, nothing more.
"""

import threading
import time

from app.agent.devlog import devlog

BREAKER_FAILURES = 3


class Watcher:
    name = "watcher"                 # subclasses set this; config key is watch_<name>_enabled

    def __init__(self, config, bus, *, clock=time.monotonic):
        self.config = config
        self.bus = bus
        self._clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._failures = 0
        self.benched = False
        self._last_emit = {}         # rate-limit: (type, kind) -> last monotonic
        self._armed = {}             # hysteresis: metric -> armed?

    # ------------------------------------------------------------- lifecycle
    def enabled(self) -> bool:
        return bool(getattr(self.config, f"watch_{self.name}_enabled", True))

    def start(self) -> bool:
        """Start the supervised thread. Returns False (and does nothing) if the
        watcher is disabled in config."""
        if not self.enabled():
            return False
        self._thread = threading.Thread(target=self._run_guarded, daemon=True,
                                        name=f"anna-watch-{self.name}")
        self._thread.start()
        return True

    def _run_guarded(self) -> None:
        try:
            self.run()
        except Exception as e:       # an unhandled crash benches, never propagates
            self._bench(f"crashed: {' '.join(str(e).split())[:100]}")

    def run(self) -> None:
        raise NotImplementedError

    def sleep(self, seconds: float) -> bool:
        """Interruptible poll sleep. True if the watcher was asked to stop."""
        return self._stop.wait(seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.5)

    # ------------------------------------------------- emit (rate-limited)
    def emit(self, type, payload=None, *, key="", kind="", simulated=False,
             min_interval_s=0.0) -> bool:
        """Publish an event. `min_interval_s` throttles the same (type, kind) so
        a chatty watcher can't flood; `simulated` tags test injections."""
        rl = (type, kind or key)
        now = self._clock()
        if min_interval_s and (now - self._last_emit.get(rl, -1e18)) < min_interval_s:
            return False
        self._last_emit[rl] = now
        body = dict(payload or {})
        if simulated:
            body["simulated"] = True
        try:
            self.bus.publish(type, source=self.name, payload=body, key=key)
            self._failures = 0
            return True
        except Exception as e:
            self._on_failure(e)
            return False

    # ------------------------------------------- hysteresis (no boundary spam)
    def crossed_below(self, metric, value, alert_below, rearm_above) -> bool:
        """True exactly once when `value` drops below `alert_below`; re-arms only
        after it recovers to `rearm_above`. Disk <10% alerts, re-arms >12%."""
        armed = self._armed.get(metric, True)
        if armed and value < alert_below:
            self._armed[metric] = False
            return True
        if not armed and value >= rearm_above:
            self._armed[metric] = True
        return False

    def crossed_above(self, metric, value, alert_above, rearm_below) -> bool:
        """Mirror of crossed_below for high-side thresholds (RAM >90%, re-arm <87%)."""
        armed = self._armed.get(metric, True)
        if armed and value > alert_above:
            self._armed[metric] = False
            return True
        if not armed and value <= rearm_below:
            self._armed[metric] = True
        return False

    # ------------------------------------------------------ circuit breaker
    def _on_failure(self, exc) -> None:
        self._failures += 1
        devlog.warn(f"watcher[{self.name}]: {' '.join(str(exc).split())[:100]} "
                    f"[{self._failures}/{BREAKER_FAILURES}]")
        if self._failures >= BREAKER_FAILURES:
            self._bench(f"{self._failures} consecutive failures")

    def _bench(self, reason: str) -> None:
        if self.benched:
            return
        self.benched = True
        self._stop.set()
        devlog.warn(f"watcher[{self.name}] BENCHED: {reason} — core unaffected.")
        # Doctor-visible: the bus tees this to the event log as an error row.
        try:
            self.bus.publish("error", source=self.name,
                             payload={"component": f"watch-{self.name}",
                                      "message": reason})
        except Exception:
            pass

    # ------------------------------------------------- test hook (simulated)
    def test_emit(self, kind, *, simulated=True) -> bool:
        """Subclass emits a representative event for `kind`. Always tagged
        `simulated` by default so the soak dataset stays clean."""
        raise NotImplementedError
