"""Wake word: name-based Whisper backend (default) matches Anna/Anastasia
with no training; openwakeword backend warns once when its package is missing;
never initialized when disabled."""

import sys

from app.main import Controller
from app.voice.wake_word import DEFAULT_PHRASES, _match_phrase, make_wake_word
from tests.fakes import FakeHistory, FakeMainUI, make_config


def make_controller(**config_overrides):
    ui = FakeMainUI()
    controller = Controller(ui=ui, autostart=False, config=make_config(**config_overrides),
                            memory=object(), history=FakeHistory())
    controller.speech.shutdown()
    return controller, ui


# ---- phrase matching (the heart of the name-based wake word) ----------------

def test_wake_phrases_match_the_name():
    p = DEFAULT_PHRASES
    assert _match_phrase("Anna", p)
    assert _match_phrase("hey Anna", p)
    assert _match_phrase("Anastasia", p)
    assert _match_phrase("hey Anastasia!", p)
    assert _match_phrase("anna, open notepad", p)   # name at the start of a command


def test_wake_phrases_do_not_false_fire():
    p = DEFAULT_PHRASES
    assert not _match_phrase("banana", p)           # not a substring hit
    assert not _match_phrase("i wanna go", p)
    assert not _match_phrase("open notepad", p)
    assert not _match_phrase("", p)


def test_fuzzy_match_tolerates_mishears():
    # real mishears observed from base/small Whisper on synthesized names
    assert _match_phrase("annah", DEFAULT_PHRASES)
    assert _match_phrase("anastasiya", DEFAULT_PHRASES)
    assert _match_phrase("Hey, Anna.", DEFAULT_PHRASES)
    assert _match_phrase("and Stasia.", DEFAULT_PHRASES)
    assert _match_phrase("Hey, I'm Stasia.", DEFAULT_PHRASES)
    # ultra-short mistranscripts must NOT false-fire
    assert not _match_phrase("N", DEFAULT_PHRASES)
    assert not _match_phrase("And", DEFAULT_PHRASES)


def test_default_backend_is_whisper():
    listener = make_wake_word(make_config(), on_wake=lambda: None)
    assert type(listener).__name__ == "WhisperWakeWord"
    assert listener.phrases == DEFAULT_PHRASES


# ---- controller wiring -------------------------------------------------------

def test_whisper_wake_word_starts_and_reports_name(monkeypatch):
    started = {"n": 0}

    class FakeListener:
        def __init__(self, config, on_wake): self.on_wake = on_wake
        def start(self): started["n"] += 1
        def stop(self): pass

    monkeypatch.setattr("app.voice.wake_word.make_wake_word",
                        lambda c, cb: FakeListener(c, cb))
    controller, ui = make_controller(wake_word_enabled=False)
    controller.toggle_wake_word_on()
    assert started["n"] == 1
    assert controller.config.wake_word_enabled is True
    # the greeting names Anna, not Jarvis
    msg = " ".join(ui.messages["info"]).lower()
    assert "anna" in msg and "jarvis" not in msg


def test_wakeword_not_initialized_when_disabled(monkeypatch):
    controller, ui = make_controller(wake_word_enabled=False)
    if controller.config.wake_word_enabled:      # autostart guard
        controller.toggle_wake_word_on()
    assert controller.wake_listener is None


def test_openwakeword_backend_warns_once_when_missing(monkeypatch):
    # openwakeword isn't installed in CI -> WakeWordUnavailable, warned once
    controller, ui = make_controller(wake_word_enabled=True,
                                     wake_word_backend="openwakeword")
    controller.toggle_wake_word_on()
    controller.toggle_wake_word_on()
    warnings = [m for m in ui.messages["info"] + ui.messages["error"]
                if "wake word" in m.lower()]
    assert len(warnings) == 1
    assert controller.wake_listener is None
    assert controller.config.wake_word_enabled is False
