"""Auto-start supervision via Windows Task Scheduler (Phase 0, commit 7, D-0.3).

A **user-session** ONLOGON task — never a Windows Service (D-0.3: a service runs
in Session 0, with no microphone, no UIA, no desktop). Task Scheduler starts
anna-core when the user logs in, and restarts it a few times if it crashes.

**This edits the user's machine startup, so it is strictly OPT-IN and reversible
(Protocol §4 consent doctrine):**

  * `enable()` is called ONLY by an explicit user action — the installer
    checkbox (default off) or the Settings toggle. Nothing on boot calls it.
  * `disable()` cleanly REMOVES the task. `is_enabled()` reads the real state
    from Task Scheduler, so the toggle never drifts from reality.
  * Nothing here ever silently re-adds the task. Disable means gone.

The `schtasks` invocations go through an injected `run` callable, so the exact
commands are asserted in tests without touching the real scheduler.
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME = "AnastasiaAnnaCore"


def _run_schtasks(args: list) -> tuple:
    """(returncode, stdout, stderr). Never raises — a scheduler that isn't
    there (non-Windows, locked-down box) is a clean 'not enabled', not a crash."""
    try:
        proc = subprocess.run(["schtasks", *args], capture_output=True,
                              text=True, timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def core_command() -> str:
    """The command Task Scheduler should run at logon to launch anna-core.

    Frozen (installed) build: the packaged exe with --core. Dev: pythonw.exe
    running the module, so no console window flashes at logon."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --core'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    launcher = str(pyw if pyw.exists() else exe)
    main = str(Path(__file__).resolve().parents[2] / "app" / "main.py")
    return f'"{launcher}" "{main}" --core'


def enable(command: str = None, run=None) -> bool:
    """Register (or replace) the ONLOGON task. EXPLICIT user action only.
    Returns True on success. `/RL LIMITED` — no elevation; it runs as the
    logged-in user, which is exactly the session Anna needs."""
    run = run or _run_schtasks           # resolved at call time (test-swappable)
    command = command or core_command()
    code, _out, _err = run(["/Create", "/TN", TASK_NAME, "/SC", "ONLOGON",
                            "/TR", command, "/RL", "LIMITED", "/F"])
    return code == 0


def disable(run=None) -> bool:
    """Remove the task. Idempotent: deleting a task that isn't there still
    leaves us in the desired state (disabled), so that counts as success."""
    run = run or _run_schtasks
    code, _out, err = run(["/Delete", "/TN", TASK_NAME, "/F"])
    if code == 0:
        return True
    # "ERROR: The system cannot find the ... specified" — already gone = success.
    lowered = (err or "").lower()
    if "cannot find" in lowered or "does not exist" in lowered:
        return True
    return False


def is_enabled(run=None) -> bool:
    """The REAL state, straight from Task Scheduler — the source of truth the
    Settings toggle reflects."""
    run = run or _run_schtasks
    code, _out, _err = run(["/Query", "/TN", TASK_NAME])
    return code == 0
