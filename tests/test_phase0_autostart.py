"""Phase 0 commit 7: opt-in logon auto-start (Task Scheduler ONLOGON, D-0.3).

Consent doctrine (Protocol §4): editing machine startup is EXPLICIT and
REVERSIBLE. Pinned here: enable issues an ONLOGON create; the Settings toggle
OFF cleanly REMOVES the task (the removal path is the important one); the state
the toggle shows is the REAL scheduler state; and nothing ever silently
re-adds the task.
"""

from app.core import autostart
from app.main import Controller
from tests.fakes import FakeHistory, FakeMainUI, make_config


class FakeSchtasks:
    """Records every schtasks invocation and returns a staged result."""

    def __init__(self, *, exists=False, fail=False):
        self.calls = []
        self.exists = exists
        self.fail = fail

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[0]
        if verb == "/Create":
            if self.fail:
                return 1, "", "ERROR: Access is denied."
            self.exists = True
            return 0, "", ""
        if verb == "/Delete":
            if not self.exists:
                return 1, "", "ERROR: The system cannot find the file specified."
            self.exists = False
            return 0, "", ""
        if verb == "/Query":
            return (0, "task", "") if self.exists else (1, "", "ERROR: cannot find")
        return 1, "", "unknown verb"

    def verbs(self):
        return [c[0] for c in self.calls]


# ---- the module: enable / disable / query -------------------------------------

def test_enable_creates_an_onlogon_task():
    sch = FakeSchtasks()
    assert autostart.enable(command="X --core", run=sch) is True
    create = sch.calls[0]
    assert create[:2] == ["/Create", "/TN"] and autostart.TASK_NAME in create
    assert "ONLOGON" in create and "/RL" in create and "LIMITED" in create
    assert sch.exists is True


def test_disable_removes_the_task_the_removal_path():
    sch = FakeSchtasks(exists=True)
    assert autostart.disable(run=sch) is True
    assert sch.calls[0][:2] == ["/Delete", "/TN"]
    assert autostart.TASK_NAME in sch.calls[0]
    assert sch.exists is False                       # actually gone


def test_disable_is_idempotent_when_already_absent():
    sch = FakeSchtasks(exists=False)
    assert autostart.disable(run=sch) is True         # "cannot find" == success
    assert sch.exists is False


def test_is_enabled_reflects_real_scheduler_state():
    on = FakeSchtasks(exists=True)
    off = FakeSchtasks(exists=False)
    assert autostart.is_enabled(run=on) is True
    assert autostart.is_enabled(run=off) is False


def test_enable_failure_is_reported_not_swallowed():
    sch = FakeSchtasks(fail=True)
    assert autostart.enable(command="X", run=sch) is False


def test_core_command_targets_core():
    assert "--core" in autostart.core_command()


# ---- the Controller toggle ----------------------------------------------------

def _controller():
    class _Mem:
        def get(self, k, d=None): return d
        def set(self, k, v): pass
    c = Controller(ui=FakeMainUI(), autostart=False,
                   config=make_config(autostart_enabled=False),
                   memory=_Mem(), history=FakeHistory())
    c.speech.shutdown()
    return c


def test_toggle_on_registers_then_mirrors_real_state(monkeypatch):
    c = _controller()
    sch = FakeSchtasks(exists=False)
    monkeypatch.setattr(autostart, "_run_schtasks", sch)     # the whole surface

    c.set_toggle("autostart", True)
    assert "/Create" in sch.verbs()
    assert c.config.autostart_enabled is True         # mirrored from is_enabled()


def test_toggle_off_removes_the_task_and_never_recreates(monkeypatch):
    c = _controller()
    sch = FakeSchtasks(exists=True)                   # currently installed
    monkeypatch.setattr(autostart, "_run_schtasks", sch)

    c.set_toggle("autostart", False)
    assert "/Delete" in sch.verbs()
    assert "/Create" not in sch.verbs()               # OFF never re-adds
    assert sch.exists is False
    assert c.config.autostart_enabled is False


def test_nothing_on_boot_enables_autostart(monkeypatch):
    """The consent invariant: constructing the Controller (as --core does at
    boot) must NEVER touch the scheduler. Only an explicit toggle does."""
    touched = []
    monkeypatch.setattr(autostart, "_run_schtasks",
                        lambda args: touched.append(args) or (1, "", ""))
    _controller()                                     # full construction
    assert touched == [], "boot must not call schtasks — auto-start is opt-in"
