"""Anastasia (Anna) — entry point and controller.

Run with:  python app/main.py   (or:  python -m app.main)

The controller wires the pieces together:
  voice/text -> STT -> CommandPipeline (rule/LLM plan -> safety ->
  confirmation -> tool) -> non-blocking TTS (half-duplex mic gate)
All slow work runs on background threads; the GUI thread only paints.
Technical/diagnostic output goes to the devlog, not the chat.
"""

import sys
import tempfile
import threading
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.conversation import Conversation
from app.agent.devlog import devlog
from app.agent.history import History
from app.agent.memory import Memory
from app.agent.pipeline import CommandPipeline
from app.agent.router import Agent
from app.config import AppConfig
from app.voice.recorder import (MicrophoneError, Recorder,
                                microphone_available,
                                microphone_dropdown_state)
from app.voice.speech_output import SpeechOutput

APPROVE_WORDS = {"approve", "approved", "yes", "yeah", "yep", "haan", "confirm", "go ahead", "do it"}
CANCEL_WORDS = {"cancel", "no", "nope", "stop", "abort", "never mind", "nevermind"}


class Controller:
    """Owns config, agent, recorder, speech and the pipeline.
    Also implements the pipeline's UI interface by marshalling onto the
    GUI thread. `ui`/`autostart` are injectable for tests."""

    def __init__(self, ui=None, autostart: bool = True,
                 config=None, memory=None, history=None):
        self.config = config if config is not None else AppConfig.load()
        self.memory = memory if memory is not None else Memory()
        self.history = history if history is not None else History()
        self.agent = Agent(self.config, self.memory, self.history)
        self.conversation = Conversation()
        self.recorder = Recorder(self.config)
        self.speech = SpeechOutput(self.config,
                                   on_speaking_changed=self._on_speaking_changed)
        from app.voice.stt_providers import STTRouter
        self.stt_router = STTRouter(self.config)
        self._active_stream = None         # live Deepgram session, if any

        self.wake_listener = None
        self._wake_warned = False          # one clean warning per session
        self._mic_lock = threading.Lock()
        self._stt_generation = 0           # bumped to drop stale STT results
        self._stream_generation = 0
        self._record_source = "voice"
        self._low_mic_warned = False
        self.last_state = "ready"          # for full_state re-hydration
        self.chips = {}                    # top-bar status chips
        self._followup_armed = False       # hands-free follow-up (8D)
        self.last_tts_first_audio_ms = 0.0

        if ui is None:
            raise ValueError("Controller requires a ui (UIBridge or test fake).")
        self.ui = ui
        self._background_checks = autostart  # tests: no health-check threads

        self.pipeline = CommandPipeline(
            config=self.config, agent=self.agent, history=self.history,
            ui=self, speech=self.speech, cancel_recording=self.cancel_recording)
        if hasattr(self.agent, "brain"):
            # live Brain chip updates on failover / circuit transitions
            self.agent.brain.on_state_change = self._on_brain_state
        # live Voice chip update when the TTS circuit benches/restores Piper
        self.speech.on_tts_health_change = self._on_brain_state
        # reply-ready -> first audible sample telemetry (8D)
        self.speech.on_first_audio = self._on_first_audio
        # recent chat turns for conversational continuity (8D)
        self.agent.recent_chat_turns = self._recent_chat_turns

        if autostart:
            self._register_hotkey()
            threading.Thread(target=self._startup_checks, daemon=True).start()
            threading.Thread(target=self._warm_imports, daemon=True).start()
            if self.config.wake_word_enabled:
                self.ui.after(500, self.toggle_wake_word_on)

    # -------------------------------------------------- pipeline UI glue
    def _ui(self, fn, *args) -> None:
        """Marshal a call onto the GUI thread."""
        try:
            self.ui.after(0, lambda: fn(*args))
        except Exception:
            pass  # window closing

    def show_user(self, text: str) -> None:
        self.conversation.add("user", text)
        self._ui(self.ui.transcript.add_user, text)

    def show_anna(self, text: str) -> None:
        self.conversation.add("anna", text)
        self._ui(self.ui.transcript.add_assistant, text, self.config.assistant_nickname)

    def show_result(self, text: str, action: dict) -> None:
        """Successful action result — carries a payload for result cards."""
        self.conversation.add("anna", text, action=action)
        self._ui(self.ui.transcript.add_result, text, action)

    def show_error(self, text: str) -> None:
        self.conversation.add("error", text)
        self._ui(self.ui.transcript.add_error, text)

    def show_info(self, text: str) -> None:
        self.conversation.add("info", text)
        self._ui(self.ui.transcript.add_info, text)

    def set_state(self, state: str, detail: str = "") -> None:
        self.last_state = state
        self._ui(self.ui.set_state, state, detail)

    def ask_confirmation(self, action_id: int, transcript: str, plan, safety,
                         kind: str = "safety", message: str = "") -> None:
        self._ui(self.ui.confirm_panel.show, action_id, transcript, plan, safety,
                 self.approve_pending, self.cancel_pending, self.voice_confirm,
                 kind, message)

    def hide_confirmation(self) -> None:
        self._ui(self.ui.confirm_panel.hide)

    def _on_speaking_changed(self, active: bool) -> None:
        if active:
            self.set_state("speaking")
        elif not self.pipeline.is_processing_command and self.pipeline.pending is None:
            self.set_state("ready")
            if self._followup_armed:
                self._followup_armed = False
                self._start_followup_listen()

    def arm_followup(self) -> None:
        """Pipeline hook: after a spoken chat reply, arm a hands-free
        follow-up (only if enabled). Fires when speech finishes."""
        if getattr(self.config, "hands_free_followup", False):
            self._followup_armed = True

    def _start_followup_listen(self) -> None:
        """Reopen the mic without PTT for one follow-up (half-duplex still
        applies; silence auto-stop closes it)."""
        def worker():
            import time as _t
            _t.sleep(0.15)   # let the gate's echo tail clear first
            if self.recorder.recording or self.pipeline.is_processing_command:
                return
            devlog.log("Hands-free: listening for a follow-up.")
            self._ui(lambda: self.toggle_mic("voice"))
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------- startup
    def _startup_checks(self) -> None:
        """Clean welcome (sec 18): two friendly lines in chat, every technical
        detail in the devlog, critical problems in ONE setup card."""
        nick = self.config.assistant_nickname
        user = ""
        try:
            user = str(self.memory.get("user_name", "") or "").strip()
        except Exception:
            pass
        greeting = f"Hi {user}, I'm {nick} 💜" if user else f"Hi, I'm {nick} 💜"
        self.show_anna(f"{greeting}\nI'm ready when you are.")
        devlog.log(f"Push-to-talk hotkey: {self.config.push_to_talk_hotkey}")
        self.run_health_checks()

    def run_health_checks(self) -> None:
        """Run/re-run dependency checks. Builds the top-bar status chips and
        the setup card. Also wired to the setup card's Recheck button."""
        issues = []
        model_state = "offline"
        from app.llm.prompt_builder import (
            build_chat_messages, build_intent_messages, estimate_tokens,
        )
        command_messages = build_intent_messages("hello", self.config, self.memory)
        chat_messages = build_chat_messages("hello", self.config, self.memory)
        command_tokens = estimate_tokens(command_messages[0]["content"])
        chat_tokens = estimate_tokens(chat_messages[0]["content"])
        devlog.log(f"Prompt sizes: command≈{command_tokens} tokens | "
                   f"chat≈{chat_tokens} tokens")
        if self.agent.llm.is_available():
            models = self.agent.llm.list_models()
            if any(self.config.ollama_model in m for m in models):
                model_state = "warming"
                self._push_chips(model_state)
                devlog.log(f"Ollama running (model: {self.config.ollama_model}). "
                           "Warming up so the first command is fast...")
                ms = self.agent.llm.warm_up(command_messages)
                devlog.log(f"Model warm-up done in {ms:.0f}ms." if ms is not None
                           else "Model warm-up failed (will load on first use).")
                chat_model = self.config.chat_model or self.config.ollama_model
                if chat_model != self.config.ollama_model:
                    if any(chat_model in model for model in models):
                        chat_ms = self.agent.llm.warm_up(chat_messages, model=chat_model)
                        devlog.log(
                            f"Chat model {chat_model} warm-up: {chat_ms:.0f}ms."
                            if chat_ms is not None else
                            f"Chat model {chat_model} warm-up failed.")
                    else:
                        issues.append(f"Chat model '{chat_model}' is not installed. "
                                      f"Run: ollama pull {chat_model}")
                model_state = "connected"
            else:
                model_state = "missing"
                issues.append(f"Ollama is running but '{self.config.ollama_model}' "
                              f"isn't installed. Run: ollama pull {self.config.ollama_model}")
        else:
            issues.append("Ollama is not running. Simple local commands still work, "
                          "but chat/reasoning needs Ollama.")

        from app.voice.stt_whisper import backend_ready
        stt_ok, msg = backend_ready(self.config)
        if stt_ok:
            devlog.log(msg)
        else:
            devlog.warn(msg)
            issues.append("Voice input isn't ready yet — typing still works. "
                          "(Details in Developer Tools.)")
        self._mic_ok = microphone_available()
        if not self._mic_ok:
            devlog.warn("No microphone detected.")
            issues.append("No microphone found — voice input is disabled, but typing works.")
        from app.voice.tts_piper import (piper_available, piper_setup_status,
                                         validate_piper_config)
        self._piper_ok = piper_available(self.config)
        if not self._piper_ok:
            _, piper_reason = piper_setup_status(self.config)
            devlog.warn(f"Piper unavailable: {piper_reason}")
            if self.config.tts_backend == "piper":
                # Explicitly selected but broken -> surface it, don't go mute.
                issues.append(f"Your selected voice (Piper) isn't working — "
                              f"using the Windows voice meanwhile. {piper_reason}")
        elif self.config.tts_backend in ("auto", "piper"):
            # Startup probe (spec 8B.1): only a real synthesized WAV counts.
            probe_ok, probe_msg = validate_piper_config(self.config, play=False)
            devlog.log(f"Piper startup probe: {probe_msg}")
            if probe_ok:
                self.speech.reset_piper_circuit()
                # Preload the voice so the first spoken sentence is fast
                # (~0.3s) instead of paying a cold model load mid-reply.
                from app.voice.tts_piper import warm_piper
                warm_piper(self.config)
                devlog.log("Piper voice preloaded (warm, in-process).")
            else:
                self.speech.piper_unhealthy = True
                issues.append("Piper isn't working (details in Developer Tools) "
                              "— using the Windows voice until it validates.")
        from app.voice.tts_kokoro import kokoro_available
        self._kokoro_ok = kokoro_available(self.config)

        self._push_chips(model_state)
        if issues:
            self._ui(self.ui.show_setup_card, issues, self._on_recheck)
        else:
            self._ui(self.ui.hide_setup_card)

    def _on_brain_state(self) -> None:
        self._push_chips(self.chips.get("model", {}).get("state", "offline"))

    def _on_first_audio(self, ms: float) -> None:
        self.last_tts_first_audio_ms = ms
        devlog.log(f"tts_first_audio_ms: {ms:.0f}ms (reply ready -> first sound)")

    def _recent_chat_turns(self, max_turns: int) -> list:
        """Prior user/assistant turns for chat context (8D). Maps the
        conversation model's roles; drops info/error lines."""
        turns = []
        for entry in self.conversation.snapshot():
            role = entry.get("role")
            if role == "user":
                turns.append({"role": "user", "text": entry.get("text", "")})
            elif role == "anna" and not entry.get("action"):
                turns.append({"role": "assistant", "text": entry.get("text", "")})
        # exclude the just-added current user line; keep the last max_turns
        return turns[-(max_turns + 1):-1] if len(turns) > max_turns else turns[:-1]

    def brain_info(self) -> dict:
        """js_api: Brain chip popover data. Never contains the API key."""
        if hasattr(self.agent, "brain"):
            return self.agent.brain.info()
        return {"mode": "local_only", "circuit": "closed", "calls": []}

    def _push_chips(self, model_state: str) -> None:
        """Top-bar chips: Local model / Mic / Voice (spec sec 4/18)."""
        backend = self.config.tts_backend
        if backend == "kokoro" and getattr(self, "_kokoro_ok", False):
            voice_label, voice_state = \
                f"Voice: Kokoro · {self.config.kokoro_voice}", "ok"
        elif backend in ("auto", "piper") and getattr(self, "_piper_ok", False):
            if getattr(self.speech, "piper_unhealthy", False):
                voice_label, voice_state = \
                    "Voice: Windows fallback (Piper error)", "warn"
            else:
                stem = Path(self.config.piper_voice).stem
                stem = re.sub(r"^(?:en_[A-Z]{2}-)?|-(?:low|medium|high)$", "", stem)
                voice_label, voice_state = f"Voice: Piper · {stem}", "ok"
        elif backend == "off":
            voice_label, voice_state = "Voice: Off", "warn"
        elif backend in ("piper", "kokoro"):
            voice_label, voice_state = f"Voice: {backend.title()} setup needed", "warn"
        else:
            voice_label, voice_state = "Voice: Windows fallback", "warn"
        brain = getattr(self.agent, "brain", None)
        if brain is None or brain.mode() == "local_only":
            brain_chip = {"label": f"Brain: Local · {self.config.ollama_model}",
                          "state": "local"}
        elif brain.circuit_open():
            brain_chip = {"label": "Brain: Local (cloud offline)", "state": "warn"}
        else:
            short = self.config.cloud_model.split("-versatile")[0]
            brain_chip = {"label": f"Brain: Groq · {short}", "state": "ok"}
        self.chips = {
            "brain": brain_chip,
            "model": {"label": f"Local model: {self.config.ollama_model}",
                      "state": model_state},
            "mic": {"label": "Mic: Ready" if getattr(self, "_mic_ok", True)
                    else "Mic: Missing",
                    "state": "ok" if getattr(self, "_mic_ok", True) else "bad"},
            "voice": {"label": voice_label, "state": voice_state},
        }
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("status", {"chips": self.chips})

    def _on_recheck(self) -> None:
        devlog.log("Recheck requested from the setup card.")
        self._ui(self.ui.hide_setup_card)
        if self._background_checks:
            threading.Thread(target=self.run_health_checks, daemon=True).start()

    @staticmethod
    def _warm_imports() -> None:
        """Preload heavy tool dependencies so the first command doesn't pay
        multi-second import costs (pyautogui alone takes seconds)."""
        try:
            from PIL import ImageGrab  # noqa: F401 — screenshots
            import pyperclip           # noqa: F401 — clipboard
            import pyautogui           # noqa: F401 — hotkeys/typing
            devlog.log("Tool dependencies preloaded.")
        except Exception as e:
            devlog.warn(f"Warm import failed: {e}")

    def _register_hotkey(self) -> None:
        try:
            import keyboard
            keyboard.add_hotkey(self.config.push_to_talk_hotkey,
                                lambda: self._ui(self.toggle_mic))
        except Exception as e:
            devlog.warn(f"Global hotkey unavailable: {e}")

    # ------------------------------------------------------- text input
    def submit_text(self, text: str) -> None:
        """Typed commands never touch the microphone or STT (sec 9)."""
        self.pipeline.submit(text, source="typed")

    # ------------------------------------------------------- voice input
    def start_ptt(self) -> None:
        """js_api: begin push-to-talk recording (no-op if already on)."""
        if not self.recorder.recording:
            self.toggle_mic()

    def stop_ptt(self) -> None:
        """js_api: stop recording and transcribe (no-op if not recording)."""
        if self.recorder.recording:
            self.toggle_mic()

    def toggle_mic(self, source: str = "voice") -> None:
        with self._mic_lock:
            if not self.recorder.recording:
                if self.speech.speaking:
                    # Barge-in: pressing PTT while Anna talks cuts her off
                    # and starts listening immediately (sec 13a).
                    devlog.log("Barge-in: cancelling TTS to listen.")
                    self.speech.cancel()
                if self.pipeline.is_processing_command:
                    self.show_info("One moment — I'm finishing the last command.")
                    return
                streaming = self.stt_router.use_streaming()
                try:
                    if streaming:
                        self._begin_streaming_stt()
                    self.recorder.start(on_auto_stop=lambda: self._ui(self.toggle_mic))
                except MicrophoneError as e:
                    self._end_streaming_stt()
                    self.show_error(str(e))
                    return
                except Exception as e:
                    devlog.warn(f"Streaming STT failed to start ({e}); using local Whisper.")
                    self._end_streaming_stt()
                    self.recorder.start(on_auto_stop=lambda: self._ui(self.toggle_mic))
                self._record_source = source
                self.set_state("listening")
                self._ui(self.ui.set_mic_active, True)
                self._emit_streaming_indicator(streaming and self._active_stream is not None)
            else:
                self._ui(self.ui.set_mic_active, False)
                self._emit_streaming_indicator(False)
                if self._active_stream is not None:
                    # Streaming: ask Deepgram to flush a final; on_final routes.
                    # If nothing finalizes shortly, fall back to Whisper batch.
                    self.set_state("transcribing")
                    self._finish_streaming_stt()
                else:
                    self.set_state("transcribing")
                    generation = self._stt_generation
                    threading.Thread(target=self._finish_recording,
                                     args=(generation,), daemon=True).start()

    # ------------------------------------------------ streaming STT (9A)
    def _begin_streaming_stt(self) -> None:
        """Open a Deepgram session and feed it the recorder's live frames.
        Raises on failure so the caller falls back to local Whisper."""
        stream = self.stt_router.deepgram.start_stream(
            on_partial=self._on_stt_partial,
            on_final=self._on_stt_final,
            on_error=self._on_stt_error)
        self._active_stream = stream
        self._stream_generation = self._stt_generation
        self.recorder.set_frame_observer(stream.send_audio)
        devlog.log("Streaming STT: Deepgram socket open (live audio).")

    def _finish_streaming_stt(self) -> None:
        stream = self._active_stream
        if stream is None:
            return
        stream.finish()   # CloseStream -> Deepgram flushes a final result
        # Safety net: if no final arrives quickly, transcribe the buffer locally.
        gen = self._stt_generation

        def fallback():
            import time as _t
            _t.sleep(1.5)
            if self._active_stream is stream and not stream._final_sent \
                    and gen == self._stt_generation:
                devlog.warn("Deepgram sent no final in time — using local Whisper.")
                self.stt_router.record_failure("no final on close")
                self._end_streaming_stt()
                self._finish_recording(gen)
        threading.Thread(target=fallback, daemon=True).start()

    def _end_streaming_stt(self) -> None:
        self.recorder.set_frame_observer(None)
        if self._active_stream is not None:
            self._active_stream.close()
            self._active_stream = None

    def _on_stt_partial(self, text: str, is_endpoint: bool) -> None:
        # Live interim transcript (greyed) — the "she's hearing me" feedback.
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("stt_interim", {"text": text})

    def _on_stt_final(self, result) -> None:
        gen = self._stream_generation
        self.stt_router.record_success()
        self._end_streaming_stt()
        if self.recorder.recording:
            self.recorder.cancel()   # stop capture; we have the final
        self._ui(self.ui.set_mic_active, False)
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("stt_interim", {"text": ""})
        if gen != self._stt_generation:
            return
        if not result.text:
            self.show_info("I didn't catch that clearly. Try once more?")
            self.set_state("ready")
            return
        self.pipeline.submit(result.text, source=self._record_source,
                             confidence=result.confidence, stt_ms=result.stt_ms)

    def _on_stt_error(self, result) -> None:
        gen = self._stream_generation
        self.stt_router.record_failure(result.error_detail)
        self._end_streaming_stt()
        if gen == self._stt_generation:
            # Fall back to Whisper on the locally-buffered audio (safety net).
            self._finish_recording(gen)

    def _emit_streaming_indicator(self, active: bool) -> None:
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("stt_streaming", {"active": bool(active)})

    def cancel_recording(self) -> None:
        """Discard any live recording (typed input supersedes voice)."""
        if self.recorder.recording or self._active_stream is not None:
            self._stt_generation += 1     # drop the in-flight result
            self._end_streaming_stt()     # close the Deepgram socket if open
            self._emit_streaming_indicator(False)
            self.recorder.cancel()
            self._ui(self.ui.set_mic_active, False)
            devlog.log("Recording cancelled (typed input took over).")

    def _finish_recording(self, generation: int) -> None:
        try:
            result = self._transcribe_recording()
        except Exception as e:
            devlog.exception(e, context="STT")
            self.show_error("I couldn't transcribe that — try once more?")
            self.set_state("ready")
            return
        if generation != self._stt_generation:
            devlog.log(f"Stale STT result dropped: {result.text!r}")
            return
        if not result.text:
            # Empty STT must never set the busy state (sec 8/9).
            self.show_info("I didn't catch that clearly. Try once more?")
            self.set_state("ready")
            return
        self.pipeline.submit(result.text, source=self._record_source,
                             confidence=result.confidence, stt_ms=result.stt_ms)

    def _transcribe_recording(self):
        """Stop recording and return text plus STT confidence metadata."""
        from app.voice.stt_whisper import TranscriptionResult
        data = self.recorder.stop()
        if data is None or len(data) < self.config.sample_rate // 4:
            return TranscriptionResult(text="")
        import numpy as np
        peak = float(np.max(np.abs(data.astype(np.float32)))) / 32768.0
        if peak < 0.05 and not self._low_mic_warned:
            self._low_mic_warned = True
            devlog.warn(f"Low microphone level detected (peak: {peak * 100:.1f}%).")
            self.show_info("Your mic level seems very low — check Windows input volume.")
        from app.voice.recorder import normalize_audio_for_stt
        data = normalize_audio_for_stt(data, self.config.sample_rate, 16000)
        wav_path = Path(tempfile.gettempdir()) / "anna_input.wav"
        try:
            self.recorder.save_wav(data, wav_path, sample_rate=16000)
            from app.voice.stt_whisper import transcribe_wav
            return transcribe_wav(str(wav_path), self.config)
        finally:
            wav_path.unlink(missing_ok=True)  # never keep raw audio

    # ------------------------------------------------------- confirmation
    def approve_pending(self) -> None:
        self.pipeline.approve_pending()

    def cancel_pending(self) -> None:
        self.pipeline.cancel_pending()

    def voice_confirm(self) -> None:
        """Record ~3s, transcribe, look for approve/cancel keywords."""
        def worker():
            self.set_state("listening", "Say “approve” or “cancel”…")
            try:
                self.recorder.start()
                threading.Event().wait(3.0)
                result = self._transcribe_recording()
                text = result.text
            except Exception as e:
                devlog.exception(e, context="voice confirm")
                self.show_error("Voice confirm didn't work — use the buttons.")
                self.set_state("waiting_confirmation")
                return
            answer = text.lower().strip(" .,!?")
            devlog.log(f"Voice confirm heard: {text!r}")
            if any(w in answer for w in APPROVE_WORDS):
                self._ui(self.approve_pending)
            elif any(w in answer for w in CANCEL_WORDS):
                self._ui(self.cancel_pending)
            else:
                self.show_info("I didn't catch approve or cancel — use the buttons.")
                self.set_state("waiting_confirmation")
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------- wake word
    def toggle_wake_word(self) -> None:
        if self.wake_listener is None:
            self.toggle_wake_word_on()
        else:
            self.wake_listener.stop()
            self.wake_listener = None
            self._ui(self.ui.set_wake_switch, False)
            self.show_info("Wake word off.")

    def toggle_wake_word_on(self) -> None:
        # openwakeword is only imported here — never when the feature is off.
        from app.voice.wake_word import WakeWordListener, WakeWordUnavailable
        try:
            self.wake_listener = WakeWordListener(self.config, self._on_wake)
            self.wake_listener.start()
            if not self.config.wake_word_enabled:
                self.config.wake_word_enabled = True
                self.config.save()
            self._ui(self.ui.set_wake_switch, True)
            self.show_info('Wake word on — say "Hey Jarvis" (pre-trained model) to wake me.')
        except WakeWordUnavailable as e:
            self.wake_listener = None
            self._ui(self.ui.set_wake_switch, False)
            if self.config.wake_word_enabled:
                self.config.wake_word_enabled = False
                self.config.save()
            devlog.warn(f"{e} — install with: pip install openwakeword")
            if not self._wake_warned:
                self._wake_warned = True
                self.show_info("Wake word needs an optional package — I turned the "
                               "toggle off for now. Details are in Developer Tools.")

    def _on_wake(self) -> None:
        if not self.pipeline.is_processing_command and not self.recorder.recording:
            self._ui(self.toggle_mic, "wake_word")

    # ------------------------------------------------------- misc UI actions
    def set_toggle(self, name: str, value: bool) -> None:
        """js_api: wake_word / voice switches. Python owns the state; the
        frontend gets a toggle_sync echo so it can never drift."""
        if name == "voice":
            self.config.voice_enabled = bool(value)
            self.config.save()
            if not value:
                self.speech.cancel()
            self._sync_toggle("voice", self.config.voice_enabled)
        elif name == "wake_word":
            if value and self.wake_listener is None:
                self.toggle_wake_word_on()   # syncs the switch itself
            elif not value and self.wake_listener is not None:
                self.toggle_wake_word()
            else:
                self._sync_toggle("wake_word", self.wake_listener is not None)
        else:
            devlog.warn(f"Unknown toggle from frontend: {name!r}")

    def _sync_toggle(self, name: str, value: bool) -> None:
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("toggle_sync", {"name": name, "value": bool(value)})

    def send_full_state(self) -> None:
        """Re-hydrate the (dumb) frontend from Python-owned state."""
        if not hasattr(self.ui, "dispatch"):
            return
        self.ui.dispatch("full_state", {
            "state": self.last_state,
            "chips": self.chips,
            "toggles": {"wake_word": self.wake_listener is not None,
                        "voice": self.config.voice_enabled},
            "conversation": self.conversation.snapshot(),
            "devlog": devlog.entries(150),
            "pending": self.pipeline.pending_payload(),
            "hotkey": self.config.push_to_talk_hotkey,
            "assistant": self.config.assistant_nickname,
            "prefs": {"animation_quality": self.config.animation_quality},
        })

    def open_path(self, path: str) -> None:
        """js_api: open a file/folder Anna produced (View buttons). Only
        paths inside the screenshot dir or safe folders are allowed."""
        import os
        from pathlib import Path as P
        target = P(path)
        if not path or not target.exists():
            return
        roots = [P(self.config.screenshot_dir)] + \
                [P(f) for f in self.config.safe_folders]
        for root in roots:
            try:
                if target.resolve().is_relative_to(root.resolve()):
                    os.startfile(str(target))  # noqa: S606 — whitelisted roots
                    return
            except (OSError, ValueError):
                continue
        devlog.warn(f"open_path refused (outside allowed roots): {path}")

    def open_settings(self) -> None:
        """js_api: send current settings to the frontend modal."""
        if hasattr(self.ui, "dispatch"):
            c = self.config
            microphone_options, microphone_device, microphone_note = \
                microphone_dropdown_state(c)
            self.ui.dispatch("settings", {
                "user_name": str(self.memory.get("user_name", "") or ""),
                "ollama_url": c.ollama_url,
                "ollama_model": c.ollama_model,
                "chat_model": c.chat_model or c.ollama_model,
                "ollama_timeout": c.ollama_timeout,
                "animation_quality": c.animation_quality,
                "tts_backend": c.tts_backend,
                "piper_exe": c.piper_exe,
                "piper_voice": c.piper_voice,
                "piper_length_scale": c.piper_length_scale,
                "kokoro_model": c.kokoro_model,
                "kokoro_voices": c.kokoro_voices,
                "kokoro_voice": c.kokoro_voice,
                "tts_rate": c.tts_rate,
                "tts_volume": c.tts_volume,
                "faster_whisper_model": c.faster_whisper_model,
                "microphone_device": microphone_device,
                "microphone_options": microphone_options,
                "microphone_note": microphone_note,
                "stt_language": c.stt_language,
                "silence_seconds": c.silence_seconds,
                "max_record_seconds": c.max_record_seconds,
                # Cloud brain — the raw key NEVER goes to the frontend.
                "brain_mode": c.brain_mode,
                "cloud_model": c.cloud_model,
                "cloud_timeout_s": c.cloud_timeout_s,
                "allow_clipboard_to_cloud": c.allow_clipboard_to_cloud,
                "hands_free_followup": c.hands_free_followup,
                "groq_key_masked": self._masked_groq_key(),
                "groq_key_set": self.agent.brain.groq.configured()
                                if hasattr(self.agent, "brain") else False,
                # Streaming STT — the raw key NEVER goes to the frontend.
                "stt_mode": c.stt_mode,
                "deepgram_model": c.deepgram_model,
                "deepgram_key_masked": self.stt_router.masked_key(),
                "deepgram_key_set": self.stt_router.deepgram.configured(),
            })

    def _masked_groq_key(self) -> str:
        from app.llm.providers import mask_key
        import os
        return mask_key(os.environ.get("GROQ_API_KEY")
                        or self.config.groq_api_key)

    _SETTINGS_FIELDS = {
        "ollama_url": str, "ollama_model": str, "chat_model": str,
        "ollama_timeout": int,
        "animation_quality": str,
        "tts_backend": str, "piper_exe": str, "piper_voice": str,
        "piper_length_scale": float,
        "kokoro_model": str, "kokoro_voices": str, "kokoro_voice": str,
        "tts_rate": float, "tts_volume": int,
        "faster_whisper_model": str, "microphone_device": str,
        "stt_language": str,
        "silence_seconds": float, "max_record_seconds": int,
        "brain_mode": str, "cloud_model": str, "cloud_timeout_s": float,
        "allow_clipboard_to_cloud": bool,
        "hands_free_followup": bool,
        "stt_mode": str, "deepgram_model": str,
    }
    _SETTINGS_CHOICES = {
        "animation_quality": {"low", "medium", "high"},
        "tts_backend": {"auto", "piper", "kokoro", "windows", "off"},
        "kokoro_voice": {"af_heart", "af_bella"},
        "faster_whisper_model": {"tiny", "base", "base.en", "small", "small.en"},
        "stt_language": {"auto", "en", "hi", "mr"},
        "brain_mode": {"hybrid", "local_only"},
        "stt_mode": {"streaming", "local"},
    }

    def save_settings(self, settings: dict) -> None:
        """js_api: apply a whitelisted subset of settings, then recheck."""
        changed = []
        for key, cast in self._SETTINGS_FIELDS.items():
            if key in settings:
                try:
                    value = cast(settings[key])
                except (TypeError, ValueError):
                    continue
                choices = self._SETTINGS_CHOICES.get(key)
                if choices and value not in choices:
                    continue
                if getattr(self.config, key) != value:
                    setattr(self.config, key, value)
                    changed.append(key)
        if "user_name" in settings:
            try:
                self.memory.set("user_name", str(settings["user_name"]).strip())
                changed.append("user_name")
            except Exception:
                pass
        if "groq_api_key" in settings:
            # Never store the masked placeholder; empty string clears the key.
            key = str(settings["groq_api_key"] or "").strip()
            if "..." not in key and "•" not in key \
                    and key != self.config.groq_api_key:
                self.config.groq_api_key = key
                changed.append("groq_api_key (hidden)")
        if "deepgram_api_key" in settings:
            key = str(settings["deepgram_api_key"] or "").strip()
            if "..." not in key and "•" not in key \
                    and key != self.config.deepgram_api_key:
                self.config.deepgram_api_key = key
                changed.append("deepgram_api_key (hidden)")
        if changed:
            self.config.save()
            devlog.log(f"Settings changed: {', '.join(changed)}")
            self.show_info("Settings saved.")
            if hasattr(self.ui, "dispatch"):
                self.ui.dispatch("prefs", {
                    "animation_quality": self.config.animation_quality})
            from app.voice.tts_piper import piper_available
            if self.config.tts_backend == "piper" and not piper_available(self.config):
                self.show_info("Piper is selected but not fully configured — "
                               "I'll use the Windows voice until the runtime "
                               "and voice files are set.")
            from app.voice.tts_kokoro import kokoro_available
            if self.config.tts_backend == "kokoro" and not kokoro_available(self.config):
                self.show_info("Kokoro is selected but its package or model files "
                               "are missing — check the setup card.")
            if self._background_checks:
                threading.Thread(target=self.run_health_checks, daemon=True).start()

    # ------------------------------------------------------- settings tests
    def _test_result(self, kind: str, ok: bool, message: str) -> None:
        devlog.log(f"Test {kind}: ok={ok} — {message}")
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("test_result", {"kind": kind, "ok": ok,
                                             "message": message})

    def pick_voice_file(self, kind: str) -> str:
        """Open a native picker for a known TTS setup field."""
        file_types = {
            "piper_exe": ("Piper executable (*.exe)",),
            "piper_voice": ("Piper voice (*.onnx)",),
            "kokoro_model": ("Kokoro model (*.onnx)",),
            "kokoro_voices": ("Kokoro voices (*.bin)",),
        }
        if kind not in file_types or not getattr(self.ui, "window", None):
            return ""
        try:
            import webview
            selected = self.ui.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=file_types[kind])
            return str(selected[0]) if selected else ""
        except Exception as exc:
            devlog.exception(exc, context="voice file picker")
            return ""

    def validate_piper(self) -> None:
        """Settings button: re-probe Piper; success restores a benched
        backend (TTS circuit breaker reset)."""
        def worker():
            from app.voice.tts_piper import validate_piper_config
            ok, message = validate_piper_config(self.config, play=True)
            self._test_result("piper", ok, message)
            self._piper_ok = ok
            if ok:
                self.speech.reset_piper_circuit()
            else:
                self.speech.piper_unhealthy = True
            self._push_chips(self.chips.get("model", {}).get("state", "offline"))
        threading.Thread(target=worker, daemon=True).start()

    def validate_kokoro(self) -> None:
        def worker():
            from app.voice.tts_kokoro import validate_kokoro_config
            ok, message = validate_kokoro_config(self.config, play=True)
            self._test_result("kokoro", ok, message)
            self._kokoro_ok = ok
            self._push_chips(self.chips.get("model", {}).get("state", "offline"))
        threading.Thread(target=worker, daemon=True).start()

    def test_voice(self) -> None:
        """js_api: speak a sample line with the current voice settings."""
        from app.voice.tts_piper import piper_available
        from app.voice.tts_kokoro import kokoro_available
        selected = self.config.tts_backend
        if selected == "piper" and not piper_available(self.config):
            self._test_result("voice", False, "Piper setup is incomplete.")
            return
        if selected == "kokoro" and not kokoro_available(self.config):
            self._test_result("voice", False, "Kokoro setup is incomplete.")
            return
        backend = ("Kokoro" if selected == "kokoro" else
                   "Piper" if selected in ("auto", "piper")
                   and piper_available(self.config) else
                   "the built-in Windows voice")
        self.speech.speak_async("Hi, it's Anna — this is how I sound right now. "
                                "Do you like this voice?")
        self._test_result("voice", True, f"Speaking a sample with {backend}…")

    def test_model(self) -> None:
        """js_api: one tiny request to verify the model answers, with timing."""
        def worker():
            if not self.agent.llm.is_available():
                self._test_result("model", False, "Ollama is not reachable.")
                return
            from app.llm.prompt_builder import build_intent_messages
            ms = self.agent.llm.warm_up(
                build_intent_messages("hello", self.config, self.memory))
            if ms is None:
                self._test_result("model", False,
                                  f"'{self.config.ollama_model}' didn't answer — "
                                  "is it pulled?")
            else:
                self._test_result("model", True, f"Model answered in {ms:.0f}ms.")
        threading.Thread(target=worker, daemon=True).start()

    def test_microphone(self) -> None:
        """js_api: record ~3s and transcribe it as a mic/STT check."""
        def worker():
            if self.recorder.recording or self.pipeline.is_processing_command:
                self._test_result("mic", False, "Busy — try again in a moment.")
                return
            try:
                self.recorder.start()
                self._test_result("mic", True, "Recording 3 seconds — say something…")
                threading.Event().wait(3.0)
                result = self._transcribe_recording()
                text = result.text
            except Exception as e:
                devlog.exception(e, context="mic test")
                self._test_result("mic", False, "Microphone/STT failed — see Developer Tools.")
                return
            self._test_result("mic", bool(text),
                              f"I heard: “{text}”" if text
                              else "I didn't hear anything.")
        threading.Thread(target=worker, daemon=True).start()

    def show_history(self) -> None:
        self.ui.show_history_window(self.history.recent(50))

    def clear_history(self) -> None:
        self.history.clear()
        self.conversation.clear()
        self.ui.transcript.clear()
        self.show_info("History cleared.")

    def shutdown(self) -> None:
        """Release audio/db resources. Safe to call more than once."""
        try:
            if self.wake_listener:
                self.wake_listener.stop()
                self.wake_listener = None
            self._end_streaming_stt()
            if self.recorder.recording:
                self.recorder.cancel()
            self.speech.shutdown()
            self.history.close()
        except Exception as e:
            devlog.exception(e, context="shutdown")

    def on_close(self) -> None:
        self.shutdown()
        self.ui.destroy()


WEBVIEW2_LINK = "https://developer.microsoft.com/en-us/microsoft-edge/webview2/"


def _webview2_error_box(detail: str) -> None:
    """No WebView2 runtime -> a plain message box instead of a crash."""
    message = ("Anna's interface needs the Microsoft Edge WebView2 runtime.\n\n"
               f"Download it here:\n{WEBVIEW2_LINK}\n\n"
               f"Technical detail: {detail[:300]}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message,
                                         "Anastasia (Anna) — setup needed", 0x10)
    except Exception:
        print(message)


def main() -> None:
    if "--doctor" in sys.argv:
        from app.doctor import run_doctor
        sys.exit(run_doctor())

    import webview

    from app.web.bridge import JsApi, UIBridge

    bridge = UIBridge()
    api = JsApi(bridge)
    index = Path(__file__).resolve().parent / "web" / "index.html"
    window = webview.create_window(
        "Anastasia (Anna)", url=str(index), js_api=api,
        width=1280, height=820, min_size=(900, 650),
        background_color="#05060f")
    bridge.window = window

    controller = Controller(ui=bridge)
    bridge.controller = controller
    window.events.closing += controller.shutdown

    try:
        webview.start(gui="edgechromium", debug="--debug" in sys.argv)
    except Exception as e:
        controller.shutdown()
        _webview2_error_box(str(e))


if __name__ == "__main__":
    main()
