"""Filesystem watcher (Phase 1A commit 3): watchdog over ReadDirectoryChangesW.

Emits `watch_fs` events with **basenames only** — never a full path in the
payload (Protocol §4: no full path where a basename suffices). The full path is
used only as the in-memory coalescing key, which is never logged or sent.

Coalescing is by PARENT DIRECTORY, so both an editor save-storm (same file, many
writes) and an unzip (many files, one directory) collapse to one event per
active directory per bus window — the feed sees "activity in Downloads", not 200
rows. Excludes .git/node_modules/temp; deletions are ignored (a drive clean-up
shouldn't spam). watchdog is imported lazily so a box without it benches this one
watcher, not core.
"""

from pathlib import Path

from app.agent.devlog import devlog
from app.watchers.base import Watcher

DEFAULT_EXCLUDE = (".git", "node_modules", "__pycache__", ".venv", "venv",
                   "AppData", "$RECYCLE.BIN", ".cache", ".idea", ".vs")
TEMP_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".swp", "~")
TEMP_PREFIXES = ("~$", ".~", "~")


class FilesystemWatcher(Watcher):
    name = "filesystem"

    def __init__(self, config, bus, *, clock=None):
        import time as _t
        super().__init__(config, bus, clock=clock or _t.monotonic)
        self._observer = None
        self.paths = self._resolve_paths()

    def _resolve_paths(self) -> list:
        out = []
        for p in (getattr(self.config, "watch_paths", None) or []):
            try:
                pp = Path(p).expanduser()
                if pp.exists():
                    out.append(pp)
            except Exception:
                pass
        if not out:
            dl = Path.home() / "Downloads"
            if dl.exists():
                out.append(dl)
        return out

    def excluded(self, path) -> bool:
        excl = set(getattr(self.config, "watch_fs_exclude", None) or DEFAULT_EXCLUDE)
        if any(part in excl for part in Path(path).parts):
            return True
        name = Path(path).name
        return name.startswith(TEMP_PREFIXES) or name.endswith(TEMP_SUFFIXES)

    def run(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception as e:
            self._bench(f"watchdog unavailable: {' '.join(str(e).split())[:80]}")
            return
        if not self.paths:
            devlog.log("filesystem watcher: no existing paths to watch — idle.")
            return

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    watcher._on_fs("file_added", event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    watcher._on_fs("file_changed", event.src_path)

            def on_moved(self, event):
                dest = getattr(event, "dest_path", None) or event.src_path
                if not event.is_directory:
                    watcher._on_fs("file_added", dest)

        obs = Observer()
        for p in self.paths:
            try:
                obs.schedule(_Handler(), str(p), recursive=True)
            except Exception as e:
                devlog.warn(f"filesystem watcher: can't watch {p} ({e})")
        obs.start()
        self._observer = obs
        devlog.log(f"filesystem watcher: watching {[str(p) for p in self.paths]}")
        while not self.sleep(1.0):
            pass
        try:
            obs.stop(); obs.join(2.0)
        except Exception:
            pass

    def _on_fs(self, kind, path) -> None:
        if self.excluded(path):
            return
        p = Path(path)
        # Basenames only in the payload; coalesce by the PARENT DIR so storms
        # collapse. The dir path is the in-memory key (never logged/sent).
        self.emit("watch_fs", {"kind": kind, "name": p.name, "where": p.parent.name},
                  key=str(p.parent))

    _SIM = {
        "file_added": {"kind": "file_added", "name": "invoice.pdf", "where": "Downloads"},
        "file_changed": {"kind": "file_changed", "name": "report.docx", "where": "Documents"},
    }

    def test_emit(self, kind, *, simulated=True) -> bool:
        payload = self._SIM.get(kind)
        if payload is None:
            return False
        return self.emit("watch_fs", dict(payload), key="", simulated=simulated)
