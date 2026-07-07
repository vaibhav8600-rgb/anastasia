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
from dataclasses import dataclass
from typing import Optional, Protocol

from app.agent.devlog import CommandTrace, devlog
from app.agent.normalizer import normalize_command
from app.agent.router import classify_input_mode, match_fuzzy_command
from app.agent.responses import ROTATED_ACTION_INTENTS, warm_action_responses
from app.agent.safety import validate_action
from app.llm.ollama_client import OllamaError, OllamaModelMissing, OllamaNotRunning
from app.llm.providers import BrainUnavailable
from app.voice.stt_whisper import SpeechConfidence

BUSY_MESSAGE = "One moment — I'm finishing the last command."
PENDING_MESSAGE = "I'm still waiting for your approval on the last one — Run it or Cancel first."
EMPTY_MESSAGE = "I didn't catch that clearly. Try once more?"
GARBLE_MESSAGE = "I heard something but didn't catch it — say it once more?"
FUZZY_TIMEOUT_MESSAGE = "No worries — I let that suggestion go."
TIMEOUT_MESSAGE = ("The local model is taking too long, so I skipped that one. "
                   "Simple commands still work instantly.")
CONFIRM_TIMEOUT_MESSAGE = "That approval timed out, so I cancelled it to be safe."

FUZZY_YES = {"yes", "yeah", "yep", "haan", "confirm", "go ahead", "do it"}
FUZZY_NO = {"no", "nope", "cancel", "stop", "never mind", "nevermind"}


@dataclass
class PendingAction:
    id: int
    plan: object
    safety: object
    transcript: str
    kind: str = "safety"
    message: str = ""


class PipelineUI(Protocol):
    """What the pipeline needs from a frontend. Implemented by the GUI
    adapter (and by fakes in tests)."""

    def show_user(self, text: str) -> None: ...
    def show_anna(self, text: str) -> None: ...
    def show_result(self, text: str, action: dict) -> None: ...
    def show_error(self, text: str) -> None: ...
    def show_info(self, text: str) -> None: ...
    def set_state(self, state: str, detail: str = "") -> None: ...
    def ask_confirmation(self, action_id: int, transcript: str, plan, safety,
                         kind: str = "safety", message: str = "") -> None: ...
    def hide_confirmation(self) -> None: ...


class CommandPipeline:
    def __init__(self, config, agent, history, ui: PipelineUI, speech,
                 cancel_recording=None, run_async: bool = True,
                 watchdog_seconds: float = 45.0,
                 confirm_timeout_seconds: float = 30.0,
                 fuzzy_timeout_seconds: float = 15.0):
        self.config = config
        self.agent = agent
        self.history = history
        self.ui = ui
        self.speech = speech
        self.cancel_recording = cancel_recording
        self.run_async = run_async
        self.watchdog_seconds = watchdog_seconds
        self.confirm_timeout_seconds = confirm_timeout_seconds
        self.fuzzy_timeout_seconds = fuzzy_timeout_seconds

        self.is_processing_command = False
        self.pending: Optional[PendingAction] = None
        self._action_counter = 0
        self._busy_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._watchdog: Optional[threading.Timer] = None
        self._watchdog_token = None
        self._confirm_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------ entry
    def submit(self, text: str, source: str = "typed", confidence=None,
               stt_ms: float = 0.0) -> None:
        """Entry point for every command, typed or voice. Returns fast;
        the actual work runs on a worker thread (unless run_async=False)."""
        if source == "typed" and self.cancel_recording:
            # Typing while the mic is open cancels the recording (sec 9).
            try:
                self.cancel_recording()
            except Exception as e:
                devlog.exception(e, context="cancel_recording")

        if isinstance(confidence, dict):
            confidence = SpeechConfidence(**confidence)
        elif confidence is not None and not isinstance(confidence, SpeechConfidence):
            confidence = SpeechConfidence(
                avg_logprob=getattr(confidence, "avg_logprob", None),
                no_speech_prob=getattr(confidence, "no_speech_prob", None),
                compression_ratio=getattr(confidence, "compression_ratio", None))

        voice_source = source in ("voice", "wake_word")
        thresholds = self.config.stt.garble
        if (voice_source and confidence is not None
                and confidence.no_speech_prob is not None
                and confidence.no_speech_prob > thresholds.no_speech_prob):
            devlog.log(f"Silence/noise dropped (no_speech_prob="
                       f"{confidence.no_speech_prob:.2f})")
            self.ui.show_info(EMPTY_MESSAGE)
            return

        norm = normalize_command(text, self.config)
        if norm.empty or len(norm.cleaned) <= 1:
            # Empty/hallucinated input must never set the busy flag.
            devlog.log(f"Empty/hallucinated input dropped (source={source}, raw={text!r})")
            if (text or "").strip():
                self.ui.show_info(EMPTY_MESSAGE)
            elif source in ("voice", "wake_word"):
                self.ui.show_info(EMPTY_MESSAGE)
            return
        if self.pending is not None:
            if voice_source and self.pending.kind == "fuzzy":
                answer = norm.cleaned.lower().strip(" .!?")
                if answer in FUZZY_YES:
                    self.approve_pending(action_id=self.pending.id)
                    return
                if answer in FUZZY_NO:
                    self.cancel_pending(action_id=self.pending.id)
                    return
            self.ui.show_info(PENDING_MESSAGE)
            return
        if self.is_processing_command:
            self.ui.show_info(BUSY_MESSAGE)
            return

        if self.run_async:
            threading.Thread(target=self._process,
                             args=(norm, source, confidence, stt_ms),
                             daemon=True, name="anna-command").start()
        else:
            self._process(norm, source, confidence, stt_ms)

    def _low_confidence(self, source: str, confidence) -> bool:
        if source not in ("voice", "wake_word") or confidence is None:
            return False
        thresholds = self.config.stt.garble
        low_probability = (confidence.avg_logprob is not None
                           and confidence.avg_logprob < thresholds.avg_logprob)
        repetitive = (confidence.compression_ratio is not None
                      and confidence.compression_ratio > thresholds.compression_ratio)
        return low_probability or repetitive

    def _start_chat_thinking(self):
        stop = threading.Event()

        def worker():
            phrases = ("Thinking…", "One sec…")
            if stop.wait(2.0):
                return
            index = 0
            while not stop.is_set():
                self.ui.set_state("thinking", phrases[index % len(phrases)])
                index += 1
                stop.wait(2.0)

        threading.Thread(target=worker, daemon=True,
                         name="anna-chat-status").start()
        return stop

    # ---------------------------------------------------------- worker
    def _process(self, norm, source: str, confidence=None,
                 stt_ms: float = 0.0) -> None:
        with self._busy_lock:
            if self.is_processing_command:
                self.ui.show_info(BUSY_MESSAGE)
                return
            self.is_processing_command = True
        self._start_watchdog()

        trace = CommandTrace(source=source, raw=norm.raw, normalized=norm.cleaned)
        trace.stt_ms = float(stt_ms or 0.0)
        started = time.perf_counter()
        transcript = norm.cleaned
        chat_indicator = None
        self._streamed_reply = False
        try:
            self.ui.set_state("thinking")

            # 1) fast rule router — first sentence with a clear command wins
            t0 = time.perf_counter()
            plan = None
            fuzzy = None
            for sentence in norm.sentences:
                plan = self.agent.plan_rule(sentence)
                if plan is not None:
                    transcript = sentence
                    break
            if plan is None:
                fuzzy = match_fuzzy_command(norm.cleaned, self.config)
                if fuzzy is not None:
                    plan = fuzzy.plan
            trace.routing_ms = (time.perf_counter() - t0) * 1000
            trace.normalized = transcript
            self.ui.show_user(transcript)

            # 2) exact rules, then fuzzy recovery, then confidence gate, then LLM
            if plan is not None and fuzzy is None:
                trace.route = "rule"
            elif fuzzy is not None:
                trace.route = "fuzzy_confirm" if fuzzy.needs_confirmation else "fuzzy"
                devlog.log(f"Fuzzy correction: {fuzzy.heard_target!r} -> "
                           f"{fuzzy.matched_target!r} ({fuzzy.score:.0f})")
                if fuzzy.needs_confirmation:
                    trace.intent = plan.intent
                    trace.args = plan.arguments
                    t0 = time.perf_counter()
                    safety = validate_action(plan, self.config)
                    trace.safety_ms = (time.perf_counter() - t0) * 1000
                    if not safety.allowed:
                        self._respond(f"I won't do that one. {safety.reason}",
                                      transcript, None, trace)
                        return
                    message = f"Did you mean {fuzzy.matched_target}?"
                    action_id = self._set_pending(plan, safety, transcript,
                                                  kind="fuzzy", message=message)
                    self.ui.set_state("waiting_clarification")
                    self.ui.ask_confirmation(action_id, transcript, plan, safety,
                                             kind="fuzzy", message=message)
                    self.speech.speak_async(message)
                    return
            elif self._low_confidence(source, confidence):
                trace.route = "garble"
                devlog.warn(f"Low-confidence STT not sent to LLM: {norm.raw!r}")
                self._respond(GARBLE_MESSAGE, transcript, None, trace)
                return
            else:
                trace.llm_used = True
                t0 = time.perf_counter()
                mode = classify_input_mode(norm.cleaned, self.config)
                if mode == "chat" and hasattr(self.agent, "plan_chat_stream") \
                        and self._streaming_chat_ok():
                    trace.route = "chat"
                    chat_indicator = self._start_chat_thinking()
                    plan, handed_off = self._run_streaming_chat(norm.cleaned, trace)
                    if handed_off:
                        trace.route = "chat_handoff_command"
                    else:
                        self._streamed_reply = True   # already spoken sentence-by-sentence
                elif mode == "chat" and hasattr(self.agent, "plan_chat"):
                    trace.route = "chat"
                    chat_indicator = self._start_chat_thinking()
                    plan, handed_off = self.agent.plan_chat(norm.cleaned)
                    if handed_off:
                        trace.route = "chat_handoff_command"
                else:
                    trace.route = "command_llm"
                    plan = self.agent.plan_llm(norm.cleaned)
                trace.llm_ms = (time.perf_counter() - t0) * 1000
                brain = getattr(self.agent, "brain", None)
                if brain is not None:
                    trace.provider = brain.last.provider
                    trace.failover = brain.last.failover
                    trace.data_classes = sorted(
                        c.value for c in
                        (getattr(brain, "last_data_classes", None) or ()))

            trace.intent = plan.intent
            trace.args = plan.arguments
            devlog.log(f"Plan: intent={plan.intent} args={plan.arguments} "
                       f"risk={plan.risk_level} confirm={plan.requires_confirmation}")

            if plan.intent in ("ask_clarification", "no_action"):
                # After a spoken conversational reply, optionally reopen the
                # mic for a hands-free follow-up (8D; controller decides).
                if (trace.route == "chat" and plan.intent == "no_action"
                        and source in ("voice", "wake_word")
                        and hasattr(self.ui, "arm_followup")):
                    self.ui.arm_followup()
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
                self._safe_log(transcript, plan, safety, executed=False,
                               error=safety.reason)
                self._respond(msg, transcript, None, trace, log_history=False)
                return

            # 4) confirmation gate — hand over to the user, release busy
            if safety.requires_confirmation:
                action_id = self._set_pending(plan, safety, transcript)
                self.ui.set_state("waiting_confirmation")
                self.ui.ask_confirmation(action_id, transcript, plan, safety,
                                         kind="safety", message="")
                warn = plan.confirmation_message or \
                    "This one needs your OK — check the screen for me?"
                self.speech.speak_async(warn)
                return

            # 5) execute
            self._run_tool(plan, safety, transcript, trace)

        except BrainUnavailable as e:
            self._fail(transcript, str(e), trace)
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
            if chat_indicator is not None:
                chat_indicator.set()
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
            self._safe_log(transcript, plan, None, executed=False, result=message)
        if self._streamed_reply:
            return   # already spoken sentence-by-sentence during the stream
        t0 = time.perf_counter()
        self.speech.speak_async(message)
        trace.tts_ms = (time.perf_counter() - t0) * 1000

    # ---------------------------------------------------- streamed chat (9B)
    def _streaming_chat_ok(self) -> bool:
        """Stream only when speech will actually play (else no benefit)."""
        return (self.config.voice_enabled
                and getattr(self.config, "tts_backend", "off") != "off")

    def _run_streaming_chat(self, text: str, trace):
        """Feed streamed sentences straight to the TTS queue so Anna starts
        speaking sentence 1 while the brain generates sentence 2. Barge-in
        (a new recording / cancelled speech) aborts the stream."""
        self._stream_epoch = getattr(self, "_stream_epoch", 0) + 1
        epoch = self._stream_epoch

        def should_abort():
            return epoch != self._stream_epoch

        def on_sentence(sentence: str):
            if should_abort():
                return
            self.speech.speak_async(sentence)   # warm Piper, ~0.2s/sentence

        t0 = time.perf_counter()
        plan, handed_off = self.agent.plan_chat_stream(
            text, on_sentence=on_sentence, should_abort=should_abort)
        trace.tts_ms = (time.perf_counter() - t0) * 1000
        brain = getattr(self.agent, "brain", None)
        if brain is not None and getattr(brain.last, "first_token_ms", 0):
            devlog.log(f"llm_first_token_ms: {brain.last.first_token_ms:.0f}ms")
        return plan, handed_off

    def abort_stream(self) -> None:
        """Barge-in hook: invalidate the in-flight chat stream."""
        self._stream_epoch = getattr(self, "_stream_epoch", 0) + 1

    def _safe_log(self, *args, **kwargs) -> None:
        """History logging is best-effort — it must NEVER affect the reported
        outcome or crash the caller (9.1A). History.log is already non-fatal;
        this is belt-and-suspenders for the error path."""
        try:
            self.history.log(*args, **kwargs)
        except Exception as e:
            devlog.warn(f"history log skipped: {' '.join(str(e).split())[:150]}")

    def _fail(self, transcript: str, message: str, trace) -> None:
        self.ui.set_state("error")
        self.ui.show_error(message)
        self._safe_log(transcript, None, None, executed=False, error=message)
        self.speech.speak_async(message)

    def _run_tool(self, plan, safety, transcript: str, trace) -> None:
        self.ui.set_state("executing")
        t0 = time.perf_counter()
        result = self.agent.execute(plan)   # ONLY this determines success
        trace.tool_ms = (time.perf_counter() - t0) * 1000

        # 1) Report to the user based purely on the execution result — never
        #    on whether logging works (9.1A.4: the close-paint crash reported
        #    success as failure because the exception came from logging).
        msg = result.message or plan.assistant_message or "Done."
        if result.success:
            if plan.intent in ROTATED_ACTION_INTENTS:
                msg = warm_action_responses.next(plan)
            self.ui.show_result(msg, {"intent": plan.intent,
                                      "success": True,
                                      "data": result.data})
        else:
            technical = ("\n" in msg or any(marker in msg.lower() for marker in
                         ("exit code", "traceback", "exception", "winerror", "error:")))
            if technical:
                devlog.warn(f"Tool error hidden from conversation: {msg}")
                msg = "Hmm, my local brain tripped on that one. Try me again?"
            self.ui.show_error(msg)
        t0 = time.perf_counter()
        self.speech.speak_async(msg.split("\n")[0])   # short spoken form
        trace.tts_ms = (time.perf_counter() - t0) * 1000

        # 2) Logging is best-effort and fully decoupled from the outcome above.
        self._safe_log(transcript, plan, safety, executed=result.success,
                       result=result.message if result.success else "",
                       error="" if result.success else result.message)

    # ----------------------------------------------------- confirmation
    def _set_pending(self, plan, safety, transcript: str,
                     kind: str = "safety", message: str = "") -> int:
        with self._pending_lock:
            self._action_counter += 1
            action_id = self._action_counter
            self.pending = PendingAction(action_id, plan, safety, transcript,
                                         kind=kind, message=message)
            timeout = (self.fuzzy_timeout_seconds if kind == "fuzzy"
                       else self.confirm_timeout_seconds)
            self._confirm_timer = threading.Timer(
                timeout,
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
            if action_id is not None and self.pending.id != action_id:
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
            pending = self.pending
            return {"id": pending.id, "transcript": pending.transcript,
                    "tool": pending.plan.tool_name,
                    "arguments": pending.plan.arguments,
                    "risk": pending.safety.risk_level,
                    "kind": pending.kind,
                    "message": pending.message or pending.plan.confirmation_message
                               or pending.safety.reason}

    def approve_pending(self, action_id=None) -> None:
        pending = self._take_pending(action_id)
        if not pending:
            return
        plan, safety, transcript = pending.plan, pending.safety, pending.transcript
        self.ui.hide_confirmation()

        if pending.kind == "fuzzy" and safety.requires_confirmation:
            next_id = self._set_pending(plan, safety, transcript)
            self.ui.set_state("waiting_confirmation")
            self.ui.ask_confirmation(next_id, transcript, plan, safety,
                                     kind="safety", message="")
            warn = plan.confirmation_message or \
                "This one needs your OK — check the screen for me?"
            self.speech.speak_async(warn)
            return

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
        plan, safety, transcript = pending.plan, pending.safety, pending.transcript
        self.ui.hide_confirmation()
        self._safe_log(transcript, plan, safety, executed=False,
                       result=f"cancelled ({reason})")
        if reason == "timeout":
            timeout = (self.fuzzy_timeout_seconds if pending.kind == "fuzzy"
                       else self.confirm_timeout_seconds)
            devlog.warn(f"{pending.kind.title()} confirmation timed out after "
                        f"{timeout:.0f}s: {transcript!r}")
            self.ui.show_info(FUZZY_TIMEOUT_MESSAGE if pending.kind == "fuzzy"
                              else CONFIRM_TIMEOUT_MESSAGE)
        elif pending.kind == "fuzzy":
            self.ui.show_info("No problem — I won't do that.")
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
