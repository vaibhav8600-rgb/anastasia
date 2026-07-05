"""Wake word behavior (spec sec 14): never initialized when disabled,
one clean warning per session when the optional package is missing."""

import sys

from app.main import Controller
from tests.fakes import FakeHistory, FakeMainUI, make_config


def make_controller(**config_overrides):
    ui = FakeMainUI()
    controller = Controller(ui=ui, autostart=False, config=make_config(**config_overrides),
                            memory=object(), history=FakeHistory())
    controller.speech.shutdown()
    return controller, ui


def test_wakeword_not_initialized_when_disabled(monkeypatch):
    sys.modules.pop("app.voice.wake_word", None)
    controller, ui = make_controller(wake_word_enabled=False)
    monkeypatch.setattr(Controller, "_register_hotkey", lambda self: None)
    monkeypatch.setattr(Controller, "_startup_checks", lambda self: None)

    # replicate the autostart guard: wake word only starts when enabled
    if controller.config.wake_word_enabled:
        controller.toggle_wake_word_on()

    assert controller.wake_listener is None
    assert "app.voice.wake_word" not in sys.modules   # module never imported
    assert "openwakeword" not in sys.modules          # package never imported
    assert not any("wake" in m.lower()
                   for m in ui.messages["info"] + ui.messages["error"])


def test_wakeword_warning_only_once():
    # openwakeword is not installed in the test env -> WakeWordUnavailable
    controller, ui = make_controller(wake_word_enabled=True)

    controller.toggle_wake_word_on()
    controller.toggle_wake_word_on()  # user (or a retry) flips it again

    warnings = [m for m in ui.messages["info"] + ui.messages["error"]
                if "wake word" in m.lower()]
    assert len(warnings) == 1, warnings
    assert controller.wake_listener is None
    assert controller.config.wake_word_enabled is False  # toggle flipped off
    assert ui.wake_switch_on is False
