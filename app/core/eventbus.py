"""In-process event bus for the proactive loop (Phase 1A, commit 1).

Watchers *publish* events (a file changed, the battery is low, the window
switched); the salience engine and the feed *subscribe*. Phase 0 built an
append-only event LOG (audit, to disk) and a UI fan-out — neither is a
pub/sub bus, so this is the missing piece. It reuses the event log's proven
survival design and adds coalescing:

  * **Non-blocking publish from any thread** — a watcher must never block on a
    slow consumer, exactly as `emit()` never blocks a voice turn.
  * **Per-subscriber bounded queue, drop-OLDEST, dropped-count marker** — a
    stalled subscriber can't eat RAM, and the loss is admitted, not silent.
  * **Coalescing by (type, key)** — an editor save-storm or an unzip of 500
    files collapses to ONE event per key (the latest), via a trailing-edge
    debounce with a max-hold cap so a busy key still lands.
  * **Per-subscriber circuit breaker** — a callback that throws 3× is benched
    (logged, doctor-visible); the bus and every other subscriber stay healthy.
  * **Tee to the event LOG** — every published event is also emitted for the
    audit trail and the feed, so there is one store, not two.

The coalescing/back-pressure LOGIC is separated from the threading (`_Coalescer`
takes an injected clock) so it is tested deterministically, not by sleeping.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from app.agent.devlog import devlog

QUEUE_MAX = 2000
COALESCE_MS = 200          # trailing-edge debounce window for same-key storms
MAX_HOLD_MS = 1000         # a busy key still lands within this
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 120.0


@dataclass
class Event:
    type: str
    source: str = ""
    payload: dict = field(default_factory=dict)
    key: str = ""                    # coalescing key; "" = never coalesced
    salience: object = None          # local rules may stamp a score (commit 5)
    ts: float = field(default_factory=time.time)


class _Coalescer:
    """Trailing-edge debounce, keyed by (type, key). Pure logic + injected
    clock — the worker calls offer()/ready(); tests drive them with a fake
    clock. Bounded: distinct keys past `capacity` drop-oldest with a count."""

    def __init__(self, window_ms, max_hold_ms, capacity, clock):
        self.window = window_ms / 1000.0
        self.max_hold = max_hold_ms / 1000.0
        self.capacity = capacity
        self._clock = clock
        self._pending = OrderedDict()    # key -> [event, deadline, first_seen]
        self.dropped = 0

    def offer(self, ev, now=None) -> None:
        now = self._clock() if now is None else now
        if ev.key == "":
            self._pending[object()] = [ev, now, now]      # unique → ready now
        else:
            k = (ev.type, ev.key)
            slot = self._pending.get(k)
            if slot is not None:
                slot[0] = ev                              # keep the latest
                slot[1] = min(slot[2] + self.max_hold, now + self.window)
                self._pending.move_to_end(k)
            else:
                self._pending[k] = [ev, now + self.window, now]
        while len(self._pending) > self.capacity:
            self._pending.popitem(last=False)             # drop-oldest
            self.dropped += 1

    def ready(self, now=None) -> list:
        now = self._clock() if now is None else now
        out = []
        for k in list(self._pending):
            ev, deadline, _first = self._pending[k]
            if now >= deadline:
                out.append(ev)
                del self._pending[k]
        return out

    def next_deadline(self):
        return min((slot[1] for slot in self._pending.values()), default=None)

    def take_dropped(self) -> int:
        n, self.dropped = self.dropped, 0
        return n


class _Subscriber:
    def __init__(self, name, callback, *, window_ms, max_hold_ms, capacity,
                 clock, on_bench=None):
        self.name = name
        self._cb = callback
        self._on_bench = on_bench
        self._coalescer = _Coalescer(window_ms, max_hold_ms, capacity, clock)
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._failures = 0
        self.benched = False
        self.delivered = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"anna-bus-{self.name}")
        self._thread.start()

    def offer(self, ev) -> None:
        if self.benched:
            return
        with self._lock:
            self._coalescer.offer(ev)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                ready = self._coalescer.ready()
                dropped = self._coalescer.take_dropped()
                nxt = self._coalescer.next_deadline()
            if dropped:
                devlog.warn(f"eventbus[{self.name}]: dropped {dropped} coalesced "
                            "events under load (oldest first).")
            for ev in ready:
                self._deliver(ev)
                if self.benched:
                    return
            # Sleep until the next deadline (or until woken by a new event).
            if nxt is not None:
                timeout = max(0.0, nxt - self._clock())
            else:
                timeout = 0.5
            self._wake.wait(timeout)
            self._wake.clear()

    def _deliver(self, ev) -> None:
        try:
            self._cb(ev)
            self._failures = 0
            self.delivered += 1
        except Exception as e:
            self._failures += 1
            devlog.warn(f"eventbus[{self.name}]: subscriber raised "
                        f"({' '.join(str(e).split())[:100]}) "
                        f"[{self._failures}/{BREAKER_FAILURES}]")
            if self._failures >= BREAKER_FAILURES:
                self.benched = True
                devlog.warn(f"eventbus[{self.name}]: BENCHED after "
                            f"{self._failures} failures — the bus and other "
                            "subscribers keep running.")
                if self._on_bench:
                    try:
                        self._on_bench(self.name)
                    except Exception:
                        pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(1.0)


class EventBus:
    def __init__(self, *, eventlog=None, scorer=None, queue_max=QUEUE_MAX,
                 coalesce_ms=COALESCE_MS, max_hold_ms=MAX_HOLD_MS,
                 clock=time.monotonic):
        self._eventlog = eventlog
        self._scorer = scorer            # callable(event) -> (score, rule); optional
        self._queue_max = queue_max
        self._coalesce_ms = coalesce_ms
        self._max_hold_ms = max_hold_ms
        self._clock = clock
        self._subs = []
        self._lock = threading.Lock()
        self.benched = []                # names, for --doctor

    def subscribe(self, name, callback, *, coalesce_ms=None, queue_max=None):
        sub = _Subscriber(
            name, callback,
            window_ms=self._coalesce_ms if coalesce_ms is None else coalesce_ms,
            max_hold_ms=self._max_hold_ms,
            capacity=queue_max or self._queue_max,
            clock=self._clock, on_bench=self._note_bench)
        with self._lock:
            self._subs.append(sub)
        sub.start()
        return sub

    def publish(self, type, source="", payload=None, *, key="", salience=None):
        """Non-blocking, thread-safe. Tees to the event log, then offers to
        every subscriber (each coalesces + back-pressures on its own)."""
        ev = Event(type=type, source=source, payload=dict(payload or {}),
                   key=str(key or ""), salience=salience)
        # Score BEFORE the tee so the score+rule land in the audit log and the
        # feed (a scorer that throws never blocks the event).
        if self._scorer is not None:
            try:
                score, rule = self._scorer(ev)
                ev.salience = score
                ev.payload["score"] = score
                ev.payload["rule"] = rule
            except Exception:
                pass
        self._tee(ev)
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.offer(ev)
        return ev

    def _tee(self, ev) -> None:
        if self._eventlog is None:
            return
        try:
            # The event log's field allowlist is the privacy boundary; unknown
            # types get an empty payload (fail-closed) until their fields are
            # declared with the watcher that emits them.
            self._eventlog.emit(ev.type, source=ev.source, **ev.payload)
        except Exception:
            pass

    def _note_bench(self, name) -> None:
        if name not in self.benched:
            self.benched.append(name)

    def stats(self) -> dict:
        with self._lock:
            subs = list(self._subs)
        return {"subscribers": len(subs),
                "benched": list(self.benched),
                "delivered": {s.name: s.delivered for s in subs}}

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.stop()
