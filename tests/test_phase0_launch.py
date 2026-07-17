"""Phase 0 commit 8: the split is the DEFAULT, `--legacy` is the escape hatch.

Pins the routing in `main()` (no window is ever opened in these tests — the
launch targets are patched) and the frozen-vs-dev UI launch command, so a
packaged build actually launches the window.
"""

import sys

import pytest

import app.main as m
from app.core import daemon


def route(argv, monkeypatch):
    """Call main() with argv, recording which launcher it chose."""
    calls = {}
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    monkeypatch.setattr(daemon, "run_daemon",
                        lambda a=None, *, open_ui=False: calls.setdefault("daemon_open_ui", open_ui) or 0)
    monkeypatch.setattr(m, "run_legacy", lambda: calls.setdefault("legacy", True))
    import app.anna_ui
    monkeypatch.setattr(app.anna_ui, "run_ui", lambda a=None: calls.setdefault("ui", True) or 0)
    try:
        m.main()
    except SystemExit:
        pass
    return calls


def test_default_launches_the_split_with_the_window():
    """No flags → this process is anna-core AND it opens the window once."""
    import _pytest.monkeypatch
    mp = _pytest.monkeypatch.MonkeyPatch()
    try:
        calls = route([], mp)
    finally:
        mp.undo()
    assert calls.get("daemon_open_ui") is True     # split + auto-open the window
    assert "legacy" not in calls


def test_core_flag_is_headless_no_window(monkeypatch):
    calls = route(["--core"], monkeypatch)
    assert calls.get("daemon_open_ui") is False    # daemon, but NO auto-window
    assert "legacy" not in calls and "ui" not in calls


def test_ui_flag_launches_only_the_window(monkeypatch):
    calls = route(["--ui"], monkeypatch)
    assert calls.get("ui") is True
    assert "daemon_open_ui" not in calls


def test_legacy_flag_runs_the_single_process_app(monkeypatch):
    calls = route(["--legacy"], monkeypatch)
    assert calls.get("legacy") is True             # the escape hatch...
    assert "daemon_open_ui" not in calls           # ...never the daemon


# ---- the UI launch command works in a FROZEN build ---------------------------

def test_ui_launch_command_dev_routes_through_main_py():
    cmd = daemon.ui_launch_command(8765)
    assert cmd[-3:] == ["--ui", "--port", "8765"]
    assert cmd[1].endswith("main.py")              # dev: python main.py --ui


def test_ui_launch_command_frozen_uses_the_exe_directly(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Anastasia\Anastasia.exe",
                        raising=False)
    cmd = daemon.ui_launch_command(8790)
    # A frozen exe has no `-m` and no main.py path — it IS the entrypoint.
    assert cmd == [r"C:\Program Files\Anastasia\Anastasia.exe", "--ui",
                   "--port", "8790"]


def test_open_ui_skips_when_a_window_is_already_connected(monkeypatch):
    launched = []
    monkeypatch.setattr("subprocess.Popen", lambda cmd, *a, **k: launched.append(cmd))

    class Srv:
        _clients = [type("C", (), {"ready": True})()]
    daemon._open_ui(Srv(), 8765)
    assert launched == [], "must not stack a second window on an attached one"

    class Empty:
        _clients = []
    daemon._open_ui(Empty(), 8765)
    assert len(launched) == 1 and launched[0][-3:] == ["--ui", "--port", "8765"]
