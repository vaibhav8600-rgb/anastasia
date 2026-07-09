"""Regression tests for safe window control behavior."""

import sys
import types

from app.tools import ToolContext
from app.tools.window_control import window_control
from tests.fakes import make_config


def _fake_pyautogui(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        FAILSAFE=False,
        hotkey=lambda *keys: calls.append(keys),
    )
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return calls


def test_bare_close_asks_for_target_without_hotkey(monkeypatch):
    calls = _fake_pyautogui(monkeypatch)
    ctx = ToolContext(config=make_config())

    result = window_control({"action": "close"}, ctx)

    assert not result.success
    assert "Which window" in result.message
    assert calls == []


def test_unknown_close_target_is_refused_without_hotkey(monkeypatch):
    calls = _fake_pyautogui(monkeypatch)
    ctx = ToolContext(config=make_config())

    result = window_control({"action": "close", "target": "mystery app"}, ctx)

    assert not result.success
    assert "registered" in result.message
    assert calls == []