"""Phase 10B: Gemini Live tool calls route through the LOCAL safety
validator. The cloud model never executes anything; blocked tools are never
declared; confirmations fail closed; tool responses carry no data payloads."""

import json
import time
from types import SimpleNamespace as NS

from app.agent.live_tools import (CLIPBOARD_EXPORTING, LiveToolBridge,
                                  live_tool_declarations)
from app.agent.safety import BLOCKED_TOOLS, CONFIRM_TOOLS, SAFE_TOOLS
from app.tools import ToolResult
from app.voice.gemini_live import GeminiLiveSession
from tests.fakes import FakeAgent, FakeHistory, make_config
from tests.test_phase10a import FakeSession


def make_bridge(config=None, confirm=None, execute_result=None):
    config = config or make_config()
    agent = FakeAgent(config, execute_result=execute_result)
    history = FakeHistory()
    responses = []
    bridge = LiveToolBridge(
        config, agent, history, run_async=False,
        respond=lambda cid, name, result: responses.append((cid, name, result)),
        ask_confirmation=confirm)
    return bridge, agent, history, responses


# ---- THE requirement: local validator between the cloud model and execution --

def test_gemini_live_tool_call_passes_local_safety_validator():
    """A Live tool call that understates its risk (run_terminal, no
    confirmation asked for) is still escalated by the LOCAL validator."""
    asked = []

    def confirm(plan, safety):
        asked.append((plan, safety))
        return False                        # the user declines

    bridge, agent, history, responses = make_bridge(confirm=confirm)
    bridge.handle_tool_call("run_terminal", {"command": "echo hi"}, "call-1")

    # The local policy demanded confirmation even though the cloud model
    # sent the call with no risk framing at all.
    assert asked, "local validator never asked for confirmation"
    plan, safety = asked[0]
    assert safety.requires_confirmation
    assert safety.risk_level in ("medium", "high")
    assert agent.executed == []             # declined -> nothing ran
    cid, name, result = responses[0]
    assert cid == "call-1" and name == "run_terminal"
    assert result["success"] is False
    assert "not" in result["message"].lower()


def test_confirmed_live_tool_call_executes_after_local_approval():
    bridge, agent, _, responses = make_bridge(
        confirm=lambda plan, safety: True,
        execute_result=ToolResult(True, "Done — ran it."))
    bridge.handle_tool_call("run_terminal", {"command": "echo hi"}, "call-2")
    assert len(agent.executed) == 1
    assert agent.executed[0].tool_name == "run_terminal"
    assert responses[0][2] == {"success": True, "message": "Done — ran it."}


def test_dangerous_terminal_from_live_blocked_before_confirmation():
    asked = []
    bridge, agent, history, responses = make_bridge(
        confirm=lambda p, s: asked.append(1) or True)
    bridge.handle_tool_call("run_terminal", {"command": "rm -rf C:/Users"}, "c3")
    assert asked == []                      # blocked outright, never confirmable
    assert agent.executed == []
    result = responses[0][2]
    assert result["success"] is False and "safety" in result["message"].lower()
    (args, kwargs) = history.rows[0]
    assert kwargs.get("executed") is False and kwargs.get("error")


def test_blocked_and_unknown_tools_from_live_are_refused():
    bridge, agent, _, responses = make_bridge(confirm=lambda p, s: True)
    bridge.handle_tool_call("send_email", {"to": "x@y.z"}, "c4")     # BLOCKED
    bridge.handle_tool_call("fly_to_moon", {}, "c5")                 # unknown
    assert agent.executed == []
    assert all(r[2]["success"] is False for r in responses)
    assert len(responses) == 2              # the model still gets an answer


def test_no_confirmation_hook_means_deny_by_default():
    """A missing confirmation UI must fail closed, never open."""
    bridge, agent, _, responses = make_bridge(confirm=None)
    bridge.handle_tool_call("window_control",
                            {"action": "close", "app": "chrome"}, "c6")
    assert agent.executed == []
    assert responses[0][2]["success"] is False


# ---- declarations: registry ∩ whitelist, blocked tools never declared --------

def _declared_names(config):
    decls = live_tool_declarations(config)
    return {d["name"] for d in decls[0]["function_declarations"]}


def test_blocked_tools_are_never_declared():
    from app.tools import TOOL_REGISTRY
    names = _declared_names(make_config())
    assert names, "no tools declared at all"
    assert not (names & BLOCKED_TOOLS)
    assert names <= (SAFE_TOOLS | CONFIRM_TOOLS)   # whitelist is the ceiling
    assert names <= set(TOOL_REGISTRY)             # registry = source of truth
    assert "delete_files" not in names             # destructive stub: undeclared
    assert "run_terminal" in names and "open_app" in names


def test_declarations_validate_against_sdk_schema():
    """The dict declarations must actually construct a LiveConnectConfig —
    catches SDK schema-shape drift at test time, not at connect time."""
    from google.genai import types
    config_obj = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        tools=live_tool_declarations(make_config()))
    declared = config_obj.tools[0].function_declarations
    assert {d.name for d in declared} == _declared_names(make_config())


def test_clipboard_tools_gated_by_cloud_optin():
    # Off (default): not declared, and refused if called anyway.
    config = make_config(allow_clipboard_to_cloud=False)
    assert not (_declared_names(config) & CLIPBOARD_EXPORTING)
    bridge, agent, _, responses = make_bridge(
        config=config, execute_result=ToolResult(True, "Your clipboard says: hunter2"))
    bridge.handle_tool_call("clipboard_read", {}, "c7")
    assert agent.executed == []
    assert responses[0][2]["success"] is False
    assert "hunter2" not in json.dumps(responses[0][2])
    # On: declared, and the call executes.
    config_on = make_config(allow_clipboard_to_cloud=True)
    assert CLIPBOARD_EXPORTING <= _declared_names(config_on)
    bridge, agent, _, responses = make_bridge(
        config=config_on, execute_result=ToolResult(True, "Your clipboard says: hi"))
    bridge.handle_tool_call("clipboard_read", {}, "c8")
    assert len(agent.executed) == 1
    # clipboard_write never exports local data -> declared regardless
    assert "clipboard_write" in _declared_names(config)


# ---- privacy: tool responses carry no data payloads ---------------------------

def test_tool_response_carries_no_data_payload():
    """SCREENSHOT is never-cloud: the thumbnail/data on ToolResult must not
    ride the tool response to Gemini — success + message only."""
    thumb = "data:image/jpeg;base64,AAAABBBB"
    bridge, agent, _, responses = make_bridge(execute_result=ToolResult(
        True, "Screenshot captured.",
        data={"thumb_data_url": thumb, "full_path": "C:/x.png"}))
    bridge.handle_tool_call("take_screenshot", {}, "c9")
    assert len(agent.executed) == 1
    result = responses[0][2]
    assert set(result) == {"success", "message"}
    assert "base64" not in json.dumps(result)


# ---- provenance ---------------------------------------------------------------

def test_live_tool_calls_logged_with_gemini_live_source():
    bridge, agent, history, _ = make_bridge(
        execute_result=ToolResult(True, "Opening Notepad."))
    bridge.handle_tool_call("open_app", {"app_name": "notepad"}, "c10")
    assert len(agent.executed) == 1
    (args, kwargs) = history.rows[0]
    assert args[0].startswith("[gemini_live]")
    assert kwargs.get("executed") is True


# ---- end-to-end through the real session ---------------------------------------

def tool_call_msg(name, args, call_id):
    fc = NS(name=name, args=args, id=call_id)
    return NS(server_content=None, session_resumption_update=None,
              go_away=None, tool_call=NS(function_calls=[fc]))


def test_live_session_tool_roundtrip_through_validator(monkeypatch):
    """Server tool_call -> on_tool_call -> LOCAL validator -> whitelisted
    executor -> send_tool_response arrives back at the (fake) socket."""
    config = make_config(gemini_api_key="AIzaTESTKEY123456789",
                         engine_mode="gemini_live", live_audio_consent=True)
    fake = FakeSession(script=[tool_call_msg("open_app",
                                             {"app_name": "notepad"}, "fc-9")])
    monkeypatch.setattr(GeminiLiveSession, "_connect",
                        lambda self, cfg: fake.cm())
    agent = FakeAgent(config, execute_result=ToolResult(True, "Opening Notepad."))
    bridge = LiveToolBridge(config, agent, FakeHistory(), run_async=False)
    session = GeminiLiveSession(config, on_audio_out=lambda b: None,
                                on_tool_call=bridge.handle_tool_call,
                                tools=live_tool_declarations(config))
    bridge.attach_session(session)
    session.start()
    deadline = time.time() + 3
    while not fake.sent_tool_responses and time.time() < deadline:
        time.sleep(0.02)
    assert fake.sent_tool_responses, "tool response never reached the socket"
    fr = fake.sent_tool_responses[0][0]
    assert fr.id == "fc-9" and fr.name == "open_app"
    assert fr.response == {"success": True, "message": "Opening Notepad."}
    assert agent.executed and agent.executed[0].tool_name == "open_app"
    session.close()
