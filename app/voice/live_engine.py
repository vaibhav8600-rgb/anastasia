"""One Gemini Live conversation episode (Phase 10C).

Wires the 10A session + 10B tool bridge into Anna's world:
  mic frames (recorder tee)  -> session.send_audio
  session audio out          -> LivePcmPlayer (worker thread, barge-in aware)
  input/output transcripts   -> chat panel + conversation memory (display only)
  tool calls                 -> LiveToolBridge -> LOCAL validator -> executor
  confirmations              -> the pipeline's confirmation card (blocking)
  rule short-circuit         -> clearly-simple commands run through the LOCAL
                                rule router instantly; a duplicate tool call
                                from the model is answered "already done"
  any failure                -> selector.record_failure + on_failure so the
                                controller recovers THIS turn via the pipeline

The engine never parses the model's words for actions: tool calls are the
only execution channel, and they all pass through app/agent/safety.py.
"""

import queue
import threading
import time

from app.agent.devlog import devlog
from app.agent.live_tools import LiveToolBridge, live_tool_declarations
from app.agent.safety import validate_action

RULE_DEDUP_WINDOW_S = 15.0   # model tool call repeating a local rule -> skip


class LiveEngine:
    def __init__(self, config, agent, history, pipeline, selector, *,
                 memory=None, show_user=None, show_anna=None,
                 show_result=None, on_failure=None,
                 session_factory=None, player=None):
        self.config = config
        self.agent = agent
        self.history = history
        self.pipeline = pipeline
        self.selector = selector
        self.memory = memory
        self.show_user = show_user or (lambda t: None)
        self.show_anna = show_anna or (lambda t: None)
        self.show_result = show_result or (lambda t, a: None)
        self.on_failure = on_failure or (lambda d: None)
        self._session_factory = session_factory
        self._player = player

        self.active = False
        self.session = None
        self.recorder = None
        self._out_q = queue.Queue()
        self._player_thread = None
        self._user_buf = ""
        self._anna_buf = ""
        self._turn_rule_done = False
        self._recent_local = None      # (tool_name, monotonic) rule dedup
        self._healthy_reported = False
        self._ended = threading.Event()

    # -------------------------------------------------------------- begin
    def begin(self, recorder) -> bool:
        """Connect and start streaming. False (with the failure recorded)
        if the session can't be established — the caller falls back."""
        from app.llm.prompt_builder import persona_prompt
        try:
            factory = self._session_factory
            if factory is None:
                from app.voice.gemini_live import GeminiLiveSession
                factory = GeminiLiveSession
            bridge = LiveToolBridge(
                self.config, self.agent, self.history,
                ask_confirmation=self._ask_confirmation,
                skip_check=self._skip_check)
            self.session = factory(
                self.config,
                on_audio_out=self._on_audio_out,
                on_input_transcript=self._on_input_transcript,
                on_output_transcript=self._on_output_transcript,
                on_interrupted=self._on_interrupted,
                on_tool_call=bridge.handle_tool_call,
                on_closed=self._on_closed,
                on_error=self._on_error,
                system_instruction=persona_prompt(self.config, self.memory),
                tools=live_tool_declarations(self.config))
            self.session.start()
            bridge.attach_session(self.session)
        except Exception as e:
            self.selector.record_failure(f"connect: {e}")
            self.session = None
            return False
        self.recorder = recorder
        recorder.set_frame_observer(self._on_mic_frame)
        self._player_thread = threading.Thread(
            target=self._drain_player, daemon=True, name="anna-live-out")
        self._player_thread.start()
        self.active = True
        devlog.log("Live conversation started (Gemini Live).")
        return True

    # ------------------------------------------------------------ audio in
    def _on_mic_frame(self, pcm16k: bytes) -> None:
        # The recorder already drops frames while Anna speaks (half-duplex
        # gate in its callback), so everything arriving here is the user.
        if self.active and self.session is not None:
            self.session.send_audio(pcm16k)

    # ----------------------------------------------------------- audio out
    def _get_player(self):
        if self._player is None:
            from app.voice.gemini_live import LivePcmPlayer
            self._player = LivePcmPlayer(self.config)
        return self._player

    def _on_audio_out(self, pcm24k: bytes) -> None:
        if not self._healthy_reported:
            self._healthy_reported = True
            self.selector.record_success()   # first audio = session healthy
        self._out_q.put(pcm24k)

    def _drain_player(self) -> None:
        while not self._ended.is_set():
            try:
                chunk = self._out_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                return
            try:
                self._get_player().play_chunk(chunk)
            except Exception as e:
                devlog.warn(f"live playback: {' '.join(str(e).split())[:120]}")

    def _on_interrupted(self) -> None:
        """Barge-in: the user spoke over Anna — drop queued audio instantly."""
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass
        if self._player is not None:
            self._player.stop()
        self._flush_anna()

    # ---------------------------------------------------------- transcripts
    def _on_input_transcript(self, text: str) -> None:
        if self._anna_buf:
            self._flush_anna()               # previous reply is done
        if not self._user_buf:
            self._turn_rule_done = False     # a new user turn begins
        self._user_buf += text
        self._maybe_rule_short_circuit()

    def _on_output_transcript(self, text: str) -> None:
        self._flush_user()
        self._anna_buf += text
        if self._anna_buf.rstrip().endswith((".", "!", "?")) \
                and len(self._anna_buf.strip()) > 1:
            self._flush_anna()

    def _flush_user(self) -> None:
        text = self._user_buf.strip()
        self._user_buf = ""
        if text:
            self.show_user(text)

    def _flush_anna(self) -> None:
        text = self._anna_buf.strip()
        self._anna_buf = ""
        if text:
            self.show_anna(text)

    # ------------------------------------------------- rule short-circuit
    def _maybe_rule_short_circuit(self) -> None:
        """10C.3: clearly-simple commands run through the LOCAL rule router
        the moment the transcript recognizes them — instant, offline-capable,
        no cloud round-trip. Only no-confirmation rules qualify; anything
        needing confirmation stays on the tool-call path (one card, one flow).
        The model, having heard the audio too, may still emit a tool call —
        _skip_check answers it 'already done' instead of re-executing."""
        if self._turn_rule_done or not getattr(self.config,
                                               "engine_rules_first", True):
            return
        text = self._user_buf.strip()
        if len(text) < 4:
            return
        try:
            plan = self.agent.plan_rule(text)
        except Exception:
            return
        if plan is None or plan.intent in ("ask_clarification", "no_action"):
            return
        safety = validate_action(plan, self.config)   # LOCAL, always
        if not safety.allowed or safety.requires_confirmation:
            return
        self._turn_rule_done = True
        result = self.agent.execute(plan)             # whitelisted executor
        self._recent_local = (plan.tool_name, time.monotonic())
        devlog.log(f"[live_rule] short-circuit: {plan.tool_name} "
                   f"({text!r}) success={result.success}")
        try:
            self.history.log(f"[live_rule] {text}", plan, safety,
                             executed=result.success,
                             result=result.message if result.success else "",
                             error="" if result.success else result.message)
        except Exception:
            pass
        self.show_result(result.message or "Done.",
                         {"intent": plan.intent, "success": result.success,
                          "data": result.data})

    def _skip_check(self, name: str, args: dict):
        """Bridge hook: dedup a model tool call that repeats the local rule."""
        recent = self._recent_local
        if recent and recent[0] == name \
                and time.monotonic() - recent[1] < RULE_DEDUP_WINDOW_S:
            return ("Anna already did this locally an instant ago — done. "
                    "Don't repeat it; just confirm to the user.")
        return None

    # ------------------------------------------------------- confirmation
    def _ask_confirmation(self, plan, safety) -> bool:
        """Blocking (bridge worker thread): raise the pipeline's confirmation
        card and wait for approve / cancel / timeout. Fail closed."""
        done = threading.Event()
        outcome = {"approved": False}

        def callback(approved: bool) -> None:
            outcome["approved"] = bool(approved)
            done.set()

        accepted = self.pipeline.request_external_confirmation(
            plan, safety, f"Gemini Live: {plan.tool_name}", callback)
        if not accepted:
            return False   # another confirmation is already pending
        done.wait(self.pipeline.confirm_timeout_seconds + 5.0)
        return outcome["approved"]

    # ------------------------------------------------------------ failures
    def _on_error(self, detail: str) -> None:
        self.selector.record_failure(detail)
        if self.active:
            self.active = False
            self.on_failure(detail)

    def _on_closed(self, reason: str) -> None:
        devlog.log(f"Live session closed ({reason}).")
        self.active = False

    # ---------------------------------------------------------------- end
    def end(self, reason: str = "user") -> None:
        """Idempotent teardown: socket closed, playback stopped, mic un-teed
        (the recorder itself belongs to the controller)."""
        self.active = False
        self._ended.set()
        if self.recorder is not None:
            try:
                self.recorder.set_frame_observer(None)
            except Exception:
                pass
        if self.session is not None:
            self.session.close(reason)
        self._out_q.put(None)
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._flush_user()
        self._flush_anna()

    def stats(self) -> dict:
        return self.session.stats() if self.session is not None else {}
