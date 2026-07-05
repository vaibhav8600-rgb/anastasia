"""Command pipeline — the one place a command flows through.

raw input -> normalize -> rule router -> (LLM only if no rule matched)
-> safety validator -> (confirmation) -> tool executor -> result
-> non-blocking TTS -> state reset.

Guarantees (spec sections 8, 9, 22):
  * one central busy flag, ALWAYS reset in `finally`
  * a watchdog force-clears the busy flag if it's ever held too long
  * empty input never sets busy; typed commands cancel any live recording
  * rule-routed commands never touch Ollama; LLM timeouts reset cleanly
  * pending confirmations auto-cancel after a timeout
The `ui` dependency is a narrow interface (see PipelineUI) so the pipeline
is fully testable without any GUI.
"""

import threading
import time
from typing import Optional, Protocol

from app.agent.devlog import CommandTrace, devlog
from app.agent.normalizer import looks_garbled, normalize_command
from app.agent.safety import validate_action
from app.llm.ollama_client import OllamaError, OllamaModelMissing, OllamaNotRunning

BUSY_MESSAGE = "One moment — I'm finishing the last command."
PENDING_MESSAGE = "I'm still waiting for your approval on the last one — Run it or Cancel first."
EMPTY_MESSAGE = "I didn't catch that clearly. Try once more?"
GARBLE_MESSAGE = "I heard something like a command but didn't recognize it — say it once more for me?"
TIMEOUT_MESSAGE = ("The local model is taking too long, so I skipped that one. "
                   "Simple commands still work instantly.")
CONFIRM_TIMEOUT_MESSAGE = "That approval timed out, so I cancelled it to be safe."


class PipelineUI(Protocol):
    """What the pipeline needs from a frontend. Implemented by the GUI
    adapter (and by fakes in tests)."""

    def show_user(self, text: str) -> None: ...
    def show_anna(self, text: str) -> None: ...
    def show_result(self, text: str, action: dict) -> None: ...
    def show_error(self, text: str) -> None: ...
    def show_info(self, text: str) -> None: ...
    def set_state(self, state: str, detail: str = "") -> None: ...
    def ask_confirmation(self, action_id: int, transcript: str, plan, safety) -> None: ...
    def hide_confirmation(self) -> None: ...


class CommandPipeline:
    def __init__(self, config, agent, history, ui: PipelineUI, speech,
                 cancel_recording=None, run_async: bool = True,
                 watchdog_seconds: float = 45.0,
                 confirm_timeout_seconds: float = 30.0):
        self.config = config
        self.agent = agent
        self.history = history
        self.ui = ui
        self.speech = speech
        self.cancel_recording = cancel_recording
        self.run_async = run_async
        self.watchdog_seconds = watchdog_seconds
        self.confirm_timeout_seconds = confirm_timeout_seconds

        self.is_processing_command = False
        self.pending = None            # (action_id, plan, safety, transcript)
        self._action_counter = 0
        self._busy_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._watchdog: Optional[threading.Timer] = None
        self._watchdog_token = None
        self._confirm_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------ entry
    def submit(self, text: str, source: str = "typed") -> None:
        """Entry point for every command, typed or voice. Returns fast;
        the actual work runs on a worker thread (unless run_async=False)."""
        if source == "typed" and self.cancel_recording:
            # Typing while the mic is open cancels the recording (sec 9).
            try:
                self.cancel_recording()
            except Exception as e:
                devlog.exception(e, context="cancel_recording")

        norm = normalize_command(text, self.config)
        if norm.empty:
            # Empty/hallucinated input must never set the busy flag.
            devlog.log(f"Empty/hallucinated input dropped (source={source}, raw={text!r})")
            if (text or "").strip():
                self.ui.show_info(EMPTY_MESSAGE)
            elif source in ("voice", "wake_word"):
                self.ui.show_info(EMPTY_MESSAGE)
            return
        if self.pending is not None:
            self.ui.show_info(PENDING_MESSAGE)
            return
        if self.is_processing_command:
            self.ui.show_info(BUSY_MESSAGE)
            return

        if self.run_async:
            threading.Thread(target=self._process, args=(norm, source),
                             daemon=True, name="anna-command").start()
        else:
            self._process(norm, source)

    # ---------------------------------------------------------- worker
    def _process(self, norm, source: str) -> None:
        with self._busy_lock:
            if self.is_processing_command:
                self.ui.show_info(BUSY_MESSAGE)
                return
            self.is_processing_command = True
        self._start_watchdog()

        trace = CommandTrace(source=source, raw=norm.raw, normalized=norm.cleaned)
        started = time.perf_counter()
        transcript = norm.cleaned
        try:
            self.ui.set_state("thinking")

            # 1) fast rule router — first sentence with a clear command wins
            t0 = time.perf_counter()
            plan = None
            for sentence in norm.sentences:
                plan = self.agent.plan_rule(sentence)
                if plan is not None:
                    transcript = sentence
                    break
            trace.routing_ms = (time.perf_counter() - t0) * 1000
            trace.normalized = transcript
            self.ui.show_user(transcript)

            # 2) LLM only when no rule matched (and the input isn't STT garble)
            if plan is not None:
                trace.route = "rule"
            elif source in ("voice", "wake_word") and looks_garbled(norm.cleaned):
                trace.route = "garble"
                devlog.warn(f"Probable STT garble, not sent to LLM: {norm.raw!r}")
                self._respond(GARBLE_MESSAGE, transcript, None, trace)
                return
            else:
                trace.route = "llm"
                trace.llm_used = True
                t0 = time.perf_counter()
                plan = self.agent.plan_llm(norm.cleaned)
                trace.llm_ms = (time.perf_counter() - t0) * 1000

            trace.intent = plan.intent
            trace.args = plan.arguments
            devlog.log(f"Plan: intent={plan.intent} args={plan.arguments} "
                       f"risk={plan.risk_level} confirm={plan.requires_confirmation}")

            if plan.intent in ("ask_clarification", "no_action"):
                self._respond(plan.assistant_message or "Okay.", transcript, plan, trace)
                return

            # 3) safety validator — every plan, no exceptions
            t0 = time.perf_counter()
            safety = validate_action(plan, self.config)
            trace.safety_ms = (time.perf_counter() - t0) * 1000
            devlog.log(f"Safety: allowed={safety.allowed} "
                       f"confirm={safety.requires_confirmation} risk={safety.risk_level}")

            if not safety.allowed:
                msg = (plan.assistant_message if plan.risk_level == "blocked" else "") \
                    or f"I won't do that one. {safety.reason}"
                self.history.log(transcript, plan, safety, executed=False,
                                 error=safety.reason)
                self._respond(msg, transcript, None, trace, log_history=False)
                return

            # 4) confirmation gate — hand over to the user, release busy
            if safety.requires_confirmation:
                action_id = self._set_pending(plan, safety, transcript)
                self.ui.set_state("waiting_confirmation")
                self.ui.ask_confirmation(action_id, transcript, plan, safety)
                warn = plan.confirmation_message or \
                    "This one needs your OK — check the screen for me?"
                self.speech.speak_async(warn)
                return

            # 5) execute
            self._run_tool(plan, safety, transcript, trace)

        except OllamaNotRunning:
            self._fail(transcript, "My local brain (Ollama) isn't running, so I can't "
                                   "do the thinking part. Simple commands still work.", trace)
        except OllamaModelMissing as e:
            self._fail(transcript, str(e), trace)
        except OllamaError as e:
            devlog.error(f"Ollama error: {e}")
            self._fail(transcript, TIMEOUT_MESSAGE, trace)
        except Exception as e:
            devlog.exception(e, context="pipeline")
            self._fail(transcript, "Hmm, something went sideways on that one. "
                                   "I'm back and ready now.", trace)
        finally:
            self._stop_watchdog()
            self.is_processing_command = False
            trace.total_ms = (time.perf_counter() - started) * 1000
            devlog.timing(trace)
            if self.pending is None:
                self.ui.set_state("ready")

    def _respond(self, message: str, transcript: str, plan, trace,
                 log_history: bool = True) -> None:
        """Anna answers with words only (no tool executed)."""
        self.ui.show_anna(message)
        if log_history:
            self.history.log(transcript, plan, None, executed=False, result=message)
        t0 = time.perf_counter()
        self.speech.speak_async(message)
        trace.tts_ms = (time.perf_counter() - t0) * 1000

    def _fail(self, transcript: str, message: str, trace) -> None:
        self.ui.set_state("error")
        self.ui.show_error(message)
        self.history.log(transcript, None, None, executed=False, error=message)
        self.speech.speak_async(message)

    def _run_tool(self, plan, safety, transcript: str, trace) -> None:
        self.ui.set_state("executing")
        t0 = time.perf_counter()
        result = self.agent.execute(plan)
        trace.tool_ms = (time.perf_counter() - t0) * 1000

        msg = result.message or plan.assistant_message or "Done."
        if result.success:
            # Structured payload so the frontend can render a result card
            # (e.g. screenshot preview with View/Copy/Save).
            self.ui.show_result(msg, {"intent": plan.intent,
                                      "success": True,
                                      "data": result.data})
        else:
            self.ui.show_error(msg)
        self.history.log(transcript, plan, safety, executed=result.success,
                         result=result.message if result.success else "",
                         error="" if result.success else result.message)
        t0 = time.perf_counter()
        self.speech.speak_async(msg.split("\n")[0])   # short spoken form
        trace.tts_ms = (time.perf_counter() - t0) * 1000

    # ----------------------------------------------------- confirmation
    def _set_pending(self, plan, safety, transcript: str) -> int:
        with self._pending_lock:
            self._action_counter += 1
            action_id = self._action_counter
            self.pending = (action_id, plan, safety, transcript)
            self._confirm_timer = threading.Timer(
                self.confirm_timeout_seconds,
                lambda: self.cancel_pending(reason="timeout", action_id=action_id))
            self._confirm_timer.daemon = True
            self._confirm_timer.start()
        return action_id

    def _take_pending(self, action_id=None):
        """Pop the pending action; None if there is none or the id is stale
        (e.g. a click on a card that already timed out)."""
        with self._pending_lock:
            if self.pending is None:
                return None
            if action_id is not None and self.pending[0] != action_id:
                return None
            pending, self.pending = self.pending, None
            if self._confirm_timer is not None:
                self._confirm_timer.cancel()
                self._confirm_timer = None
        return pending

    def pending_payload(self):
        """Serializable view of the pending confirmation (full_state)."""
        with self._pending_lock:
            if self.pending is None:
                return None
            action_id, plan, safety, transcript = self.pending
            return {"id": action_id, "transcript": transcript,
                    "tool": plan.tool_name, "arguments": plan.arguments,
                    "risk": safety.risk_level,
                    "message": plan.confirmation_message or safety.reason}

    def approve_pending(self, action_id=None) -> None:
        pending = self._take_pending(action_id)
        if not pending:
            return
        _, plan, safety, transcript = pending
        self.ui.hide_confirmation()

        def worker():
            with self._busy_lock:
                if self.is_processing_command:
                    self.ui.show_info(BUSY_MESSAGE)
                    return
                self.is_processing_command = True
            self._start_watchdog()
            trace = CommandTrace(source="confirmation", raw=transcript,
                                 normalized=transcript, route="approved",
                                 intent=plan.intent, args=plan.arguments)
            started = time.perf_counter()
            try:
                self._run_tool(plan, safety, transcript, trace)
            except Exception as e:
                devlog.exception(e, context="approve_pending")
                self._fail(transcript, "That didn't go through cleanly — "
                                       "I'm back and ready now.", trace)
            finally:
                self._stop_watchdog()
                self.is_processing_command = False
                trace.total_ms = (time.perf_counter() - started) * 1000
                devlog.timing(trace)
                self.ui.set_state("ready")

        if self.run_async:
            threading.Thread(target=worker, daemon=True,
                             name="anna-approved").start()
        else:
            worker()

    def cancel_pending(self, reason: str = "user", action_id=None) -> None:
        pending = self._take_pending(action_id)
        if not pending:
            return
        _, plan, safety, transcript = pending
        self.ui.hide_confirmation()
        self.history.log(transcript, plan, safety, executed=False,
                         result=f"cancelled ({reason})")
        if reason == "timeout":
            devlog.warn(f"Confirmation timed out after "
                        f"{self.confirm_timeout_seconds:.0f}s: {transcript!r}")
            self.ui.show_info(CONFIRM_TIMEOUT_MESSAGE)
        else:
            self.ui.show_info("Cancelled — nothing was executed.")
            self.speech.speak_async("Okay, cancelled.")
        self.ui.set_state("ready")

    # --------------------------------------------------------- watchdog
    def _start_watchdog(self) -> None:
        token = object()
        self._watchdog_token = token
        timer = threading.Timer(self.watchdog_seconds,
                                self._watchdog_fire, args=(token,))
        timer.daemon = True
        timer.start()
        self._watchdog = timer

    def _stop_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self._watchdog_token = None

    def _watchdog_fire(self, token) -> None:
        if token is not self._watchdog_token or not self.is_processing_command:
            return
        devlog.warn(f"Watchdog: busy flag held > {self.watchdog_seconds:.0f}s — "
                    "force-clearing so new commands can run.")
        self.is_processing_command = False
        self.ui.set_state("ready")
