"""Watchers — Anna's senses (Phase 1A). Each is a supervised task that publishes
events onto the EventBus; the salience engine and feed consume them.

Nothing here runs unless `proactive_enabled` is on (default OFF).
"""


def _registry():
    """kind -> the watcher class that owns it (for --simulate-event)."""
    from app.watchers.system import SystemWatcher
    return {
        SystemWatcher: ("disk_low", "ram_high", "battery_low", "battery_full"),
    }


def simulate_event(kind: str) -> bool:
    """Fire ONE representative event of `kind` through the real
    watcher→bus→event-log pipeline, tagged `simulated` so a soak's labeled
    dataset never mixes test and real events. Returns False on an unknown kind.

    Writes to the standard event log (viewable via --dump-events). If the daemon
    is running it appends to the same log; live routing to the running feed
    arrives with the feed (a later commit)."""
    from app.config import AppConfig
    from app.core.eventbus import EventBus
    from app.core.eventlog import EventLog

    cfg = AppConfig.load()
    log = EventLog()
    bus = EventBus(eventlog=log, coalesce_ms=0)
    try:
        for cls, kinds in _registry().items():
            if kind in kinds:
                watcher = cls(cfg, bus)
                ok = watcher.test_emit(kind, simulated=True)
                log.flush(timeout=5)
                return bool(ok)
        return False
    finally:
        log.close()


def simulatable_kinds() -> list:
    kinds = []
    for _cls, ks in _registry().items():
        kinds.extend(ks)
    return sorted(kinds)
