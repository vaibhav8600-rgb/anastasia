"""Python <-> JS bridge for the pywebview frontend (spec section 3).

UIBridge implements the same duck-typed UI surface the Controller already
talks to (transcript, confirm_panel, set_state, ...) and translates every
call into a single JS dispatcher: `ui.dispatch({type, payload})`.
Events raised before the page signals readiness are buffered and flushed,
followed by a `full_state` snapshot — the frontend is a dumb renderer that
can be re-hydrated at any time.

JsApi is the JS -> Python surface. It never receives raw commands to
execute — everything funnels through the controller/pipeline/safety stack.
"""

import json
import threading
import time

from app.agent.devlog import devlog


def _now() -> str:
    return time.strftime("%I:%M %p").lstrip("0")


class UIBridge:
    """Owns the webview window handle and all Python -> JS traffic."""

    def __init__(self):
        self.window = None            # set by main() after create_window
        self.controller = None        # set by main() after Controller()
        self._ready = False
        self._buffer = []
        self._lock = threading.Lock()
        self._on_recheck = None
        # The controller talks to ui.transcript.* and ui.confirm_panel.*
        self.transcript = self
        self.confirm_panel = self
        devlog.subscribe(self._on_devlog)

    # ------------------------------------------------------ dispatcher
    def dispatch(self, type_: str, payload: dict = None) -> None:
        event = {"type": type_, "payload": payload or {}}
        with self._lock:
            if not self._ready:
                self._buffer.append(event)
                return
        self._eval(event)

    def _eval(self, event: dict) -> None:
        if self.window is None:
            return
        try:
            self.window.evaluate_js(f"ui.dispatch({json.dumps(event)})")
        except Exception:
            pass  # window closing

    def mark_ready(self) -> None:
        """Called (via JsApi.ready) once the DOM dispatcher exists."""
        with self._lock:
            self._ready = True
            buffered, self._buffer = self._buffer, []
        for event in buffered:
            self._eval(event)

    def _on_devlog(self, entry: dict) -> None:
        self.dispatch("devlog", entry)
        if entry.get("category") == "timing":
            self.dispatch("latency", {"summary": entry.get("message", "")})

    # --------------------------------------- controller-facing surface
    def after(self, _ms, fn) -> None:
        fn()  # evaluate_js is thread-safe; no GUI thread to marshal onto

    def add_user(self, text: str) -> None:
        self.dispatch("user_message", {"text": text, "ts": _now()})

    def add_assistant(self, text: str, name: str = "Anna") -> None:
        self.dispatch("anna_message", {"text": text, "ts": _now(), "name": name})

    def add_result(self, text: str, action: dict) -> None:
        self.dispatch("action_result", {"text": text, "ts": _now(),
                                        "action": action})

    def add_info(self, text: str) -> None:
        self.dispatch("anna_message", {"text": text, "ts": _now(), "info": True})

    def add_error(self, text: str) -> None:
        self.dispatch("anna_message", {"text": text, "ts": _now(), "error": True})

    def clear(self) -> None:
        self.dispatch("clear_conversation")

    def set_state(self, state: str, detail: str = "") -> None:
        self.dispatch("state_change", {"state": state, "detail": detail})

    def set_mic_active(self, active: bool) -> None:
        self.dispatch("mic", {"active": bool(active)})

    def set_wake_switch(self, on: bool) -> None:
        self.dispatch("toggle_sync", {"name": "wake_word", "value": bool(on)})

    def show(self, action_id, transcript, plan, safety,
             on_approve=None, on_cancel=None, on_voice=None) -> None:
        """confirm_panel.show — renders the amber approval card in JS."""
        self.dispatch("confirm_request", {
            "id": action_id, "transcript": transcript,
            "tool": plan.tool_name, "arguments": plan.arguments,
            "risk": safety.risk_level,
            "message": plan.confirmation_message or safety.reason
                       or "Do you want me to go ahead?",
        })

    def hide(self) -> None:
        self.dispatch("confirm_resolved")

    def show_setup_card(self, issues: list, on_recheck) -> None:
        self._on_recheck = on_recheck
        self.dispatch("setup_card", {"issues": list(issues)})

    def hide_setup_card(self) -> None:
        self.dispatch("setup_card", {"issues": []})

    def show_history_window(self, rows: list) -> None:
        self.dispatch("history", {"rows": rows})

    def destroy(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass


class JsApi:
    """Exposed to JS as `pywebview.api`. Thin: validates, then delegates."""

    def __init__(self, bridge: UIBridge):
        self._bridge = bridge

    @property
    def _controller(self):
        return self._bridge.controller

    # -- lifecycle ------------------------------------------------------
    def ready(self) -> None:
        self._bridge.mark_ready()
        if self._controller is not None:
            self._controller.send_full_state()

    # -- input ----------------------------------------------------------
    def send_text(self, text) -> None:
        text = str(text or "").strip()
        if text and self._controller is not None:
            self._controller.submit_text(text)

    def start_ptt(self) -> None:
        self._controller.start_ptt()

    def stop_ptt(self) -> None:
        self._controller.stop_ptt()

    # -- confirmation -----------------------------------------------------
    def confirm(self, action_id, approved) -> None:
        pipeline = self._controller.pipeline
        if approved:
            pipeline.approve_pending(action_id=action_id)
        else:
            pipeline.cancel_pending(action_id=action_id)

    # -- toggles / settings ------------------------------------------------
    def set_toggle(self, name, value) -> None:
        self._controller.set_toggle(str(name), bool(value))

    def open_settings(self) -> None:
        self._controller.open_settings()

    def save_settings(self, settings) -> None:
        if isinstance(settings, dict):
            self._controller.save_settings(settings)

    def recheck(self) -> None:
        if self._bridge._on_recheck:
            self._bridge._on_recheck()

    def test_voice(self) -> None:
        self._controller.test_voice()

    def test_microphone(self) -> None:
        self._controller.test_microphone()

    def test_model(self) -> None:
        self._controller.test_model()

    # -- data ---------------------------------------------------------------
    def get_history(self, page=0) -> list:
        return self._controller.history.recent(50)

    def open_path(self, path) -> None:
        self._controller.open_path(str(path or ""))

    def clear_history(self) -> None:
        self._controller.clear_history()
