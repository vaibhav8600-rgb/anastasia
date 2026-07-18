"""Phase 1A commit 3: the filesystem watcher — basenames only, exclusions,
dir-level coalescing, and a real watchdog round-trip."""

import time

from app.watchers.filesystem import FilesystemWatcher
from tests.fakes import make_config


class FakeBus:
    def __init__(self): self.published = []
    def publish(self, type, source="", payload=None, key=""):
        self.published.append({"type": type, "payload": dict(payload or {}), "key": key})


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def _fs(bus, **cfg):
    return FilesystemWatcher(make_config(**cfg), bus)


# ---- payload audit: basenames only, never a full path ------------------------

def test_payload_is_basenames_only():
    bus = FakeBus()
    _fs(bus)._on_fs("file_added", r"C:\Users\vaibhav\Downloads\invoice-2026.pdf")
    ev = bus.published[-1]
    assert ev["type"] == "watch_fs"
    assert ev["payload"] == {"kind": "file_added", "name": "invoice-2026.pdf",
                             "where": "Downloads"}
    assert "Users" not in str(ev["payload"]) and "vaibhav" not in str(ev["payload"])


def test_coalesce_key_is_the_parent_dir():
    bus = FakeBus()
    w = _fs(bus)
    w._on_fs("file_added", r"C:\d\Downloads\a.pdf")
    w._on_fs("file_added", r"C:\d\Downloads\b.pdf")
    keys = [e["key"] for e in bus.published]
    assert keys[0] == keys[1]                       # same dir → one coalesce bucket
    assert keys[0].endswith("Downloads")


def test_exclusions_git_node_modules_and_temp():
    bus = FakeBus()
    w = _fs(bus, watch_fs_exclude=[".git", "node_modules", "__pycache__"])
    for p in (r"C:\p\.git\HEAD", r"C:\p\node_modules\x.js",
              r"C:\p\__pycache__\m.pyc", r"C:\d\report.tmp",
              r"C:\d\~$budget.xlsx", r"C:\d\movie.crdownload"):
        w._on_fs("file_changed", p)
    assert bus.published == [], "excluded/temp paths must not emit"


def test_a_real_file_still_emits():
    bus = FakeBus()
    _fs(bus, watch_fs_exclude=[".git"])._on_fs("file_changed", r"C:\work\proj\main.py")
    assert bus.published and bus.published[-1]["payload"]["name"] == "main.py"


def test_test_emit_marks_simulated_and_rejects_unknown():
    bus = FakeBus()
    w = _fs(bus)
    assert w.test_emit("file_added", simulated=True) is True
    assert bus.published[-1]["payload"]["simulated"] is True
    assert w.test_emit("bogus") is False


# ---- real watchdog: an actual file change reaches the bus --------------------

def test_real_watchdog_fires_on_a_created_file(tmp_path):
    from app.core.eventbus import EventBus
    bus = EventBus(coalesce_ms=30)
    got = []
    bus.subscribe("s", lambda ev: got.append(ev))
    # tmp_path lives under AppData\Temp, which the default exclusions skip —
    # override so this specific watched root is honoured.
    w = FilesystemWatcher(make_config(watch_paths=[str(tmp_path)],
                                      watch_fs_exclude=[".git"]), bus)
    assert w.start()
    try:
        time.sleep(0.6)                             # observer spin-up
        (tmp_path / "downloaded.pdf").write_text("x")
        assert _wait(lambda: got, timeout=5), "watchdog never delivered the event"
        assert got[0].type == "watch_fs"
        assert got[0].payload["name"] == "downloaded.pdf"
    finally:
        w.stop()
        bus.close()
