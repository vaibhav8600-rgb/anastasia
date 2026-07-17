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
import time
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.confirmation_manager import Outcome
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

# Approval/cancel phrase parsing lives in the confirmation manager (11A) so
# every entry point — typed, voice turn, and the voice-confirm button —
# shares one set of rules, including the strong phrase for destructive tiers.


def _confidence_is_low(confidence, thresholds=None) -> bool:
    """Type-safe low-confidence check for the hands-free garble guard.
    Deepgram gives a float (0..1); local Whisper gives a SpeechConfidence
    object — never compare the object directly to a float (that crashed the
    _finish_recording thread)."""
    if confidence is None:
        return False
    if isinstance(confidence, (int, float)):
        return confidence < 0.4
    # SpeechConfidence: mirror the pipeline's signals (very unlikely speech,
    # or a runaway repetitive/low-logprob transcript).
    if thresholds is None:
        from app.config import GarbleConfig
        thresholds = GarbleConfig()
    no_speech = getattr(confidence, "no_speech_prob", None)
    logprob = getattr(confidence, "avg_logprob", None)
    if no_speech is not None and no_speech > thresholds.no_speech_prob:
        return True
    if logprob is not None and logprob < thresholds.avg_logprob:
        return True
    return False


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
        from app.agent.engine import EngineSelector
        self.engine_selector = EngineSelector(self.config)   # 10C three-tier
        self._live = None                  # active Gemini Live conversation
        self._live_fallback_noted = False  # one honest info line per session

        # Vision (11B). Constructed but dormant: nothing captures until an
        # explicit trigger phrase, and the camera opener is only wired once
        # the webview exists.
        from app.vision.service import VisionService
        self.vision = VisionService(
            self.config,
            dispatch=self._vision_dispatch,
            stop_live=self._stop_live_for_privacy)
        self.vision.camera.opener = self._open_browser_camera
        self.agent.vision = self.vision
        self._camera_frames = {}           # request_id -> [Event, data_url]
        self._camera_seq = 0

        self.wake_listener = None
        self._wake_warned = False          # one clean warning per session
        self._mic_lock = threading.Lock()
        self._stt_generation = 0           # bumped to drop stale STT results
        self._stream_generation = 0
        self._record_source = "voice"
        self._low_mic_warned = False
        self.last_state = "ready"          # for full_state re-hydration
        self.chips = {}                    # top-bar status chips
        self._followup_armed = False       # hands-free follow-up (8D, legacy)
        self.last_tts_first_audio_ms = 0.0
        self._hands_free_active = False    # continuous conversation loop (9C)
        self._garble_streak = 0
        self._idle_timer = None
        self._turn_t0 = None               # turn_latency_ms anchor (9D)
        self._last_turn_stt_ms = 0.0
        self._last_turn_stt_provider = "local"   # real STT path for telemetry
        self._streaming_warned = False           # warn once per session (9.1B)
        self._listening_paused = False           # tray "Pause listening" (Phase 0)
        self._wake_was_on = False                # restore wake state on resume

        if ui is None:
            raise ValueError("Controller requires a ui (UIBridge or test fake).")
        self.ui = ui
        self._background_checks = autostart  # tests: no health-check threads

        self.pipeline = CommandPipeline(
            config=self.config, agent=self.agent, history=self.history,
            ui=self, speech=self.speech, cancel_recording=self.cancel_recording,
            confirm_timeout_seconds=float(
                getattr(self.config, "confirmation_timeout_s", 30.0)))
        if hasattr(self.agent, "brain"):
            # live Brain chip updates on failover / circuit transitions
            self.agent.brain.on_state_change = self._on_brain_state
        # live Voice chip update when the TTS circuit benches/restores Piper
        self.speech.on_tts_health_change = self._on_brain_state
        # reply-ready -> first audible sample telemetry (8D)
        self.speech.on_first_audio = self._on_first_audio
        # audio-reactive orb (9D): envelope levels, gated to the High tier
        self.speech.on_audio_level = self._on_audio_level
        self.speech.emit_levels = (self.config.animation_quality == "high")
        # recent chat turns for conversational continuity (8D)
        self.agent.recent_chat_turns = self._recent_chat_turns

        if autostart:
            self._register_hotkey()
            threading.Thread(target=self._startup_checks, daemon=True).start()
            threading.Thread(target=self._warm_imports, daemon=True).start()
            if self.config.wake_word_enabled:
                self.ui.after(500, self.toggle_wake_word_on)
            if self.config.hands_free:   # continuous mode persisted across restart
                self.ui.after(1500, lambda: self.start_hands_free(persist=False))

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
        # While a Gemini Live conversation is up the mic is always open, so
        # the resting state is "listening (Live)", never "ready" — translate
        # here so every path (incl. confirmation resolution) lands correctly
        # instead of stranding the UI. _end_live_conversation clears _live
        # first, so the final "ready" after a session ends passes through.
        if state == "ready" and self._live is not None:
            state, detail = "listening", "Live — just talk"
        self.last_state = state
        self._ui(self.ui.set_state, state, detail)

    def ask_confirmation(self, action_id: int, transcript: str, plan, safety,
                         kind: str = "safety", message: str = "") -> None:
        self._ui(self.ui.confirm_panel.show, action_id, transcript, plan, safety,
                 self.approve_pending, self.cancel_pending, self.voice_confirm,
                 kind, message)

    def hide_confirmation(self) -> None:
        self._ui(self.ui.confirm_panel.hide)

    def show_confirmation_details(self, payload: dict) -> None:
        """11A.2 'show details' — expand the pending card in place."""
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("confirm_details", payload)

    def _on_speaking_changed(self, active: bool) -> None:
        if active:
            self.set_state("speaking")
            return
        if self.pipeline.is_processing_command:
            return
        if self.pipeline.pending is not None:
            # 11A.3: a card is up. Don't start a NEW turn — but in hands-free
            # reopen the mic so "approve"/"cancel" can actually be spoken
            # (listening_for_confirmation). PTT users press the hotkey.
            self._listen_for_confirmation()
            return
        if self._hands_free_active:
            # Continuous loop: the instant Anna finishes, reopen the mic.
            self._hands_free_continue()
        else:
            self.set_state("ready")

    def _listen_for_confirmation(self) -> None:
        """Reopen the mic to hear the approve/cancel phrase while a
        confirmation card is pending. Opt-in (`confirmation_voice_listen`),
        because it opens the microphone on its own. Hands-free only: in PTT
        mode the user presses the hotkey, and in a Gemini Live session the
        mic is already open (tapping it there would end the conversation)."""
        if not getattr(self.config, "confirmation_voice_listen", False):
            return
        if not self._hands_free_active or self._live is not None:
            return

        def worker():
            import time as _t
            from app.voice import audio_gate
            _t.sleep(audio_gate.TAIL_SECONDS + 0.1)   # let the echo tail clear
            if not self._hands_free_active or self._live is not None:
                return
            if not self.pipeline.confirm.has_pending():
                return          # already approved/cancelled/expired
            if self.recorder.recording or self.speech.speaking \
                    or self.pipeline.is_processing_command:
                return
            self._ui(lambda: self.toggle_mic("voice"))
        threading.Thread(target=worker, daemon=True,
                         name="anna-confirm-listen").start()

    def arm_followup(self) -> None:
        """Pipeline hook (kept for compatibility). Continuous hands-free (9C)
        reopens the mic from _on_speaking_changed, so this is a no-op now."""
        return

    # ----------------------------------------------- continuous hands-free (9C)
    STOP_PHRASES = ("stop listening", "stop the conversation", "that's all",
                    "thats all", "that is all", "goodbye anna", "goodbye",
                    "bye anna", "bye", "go to sleep", "nevermind")

    def _is_stop_phrase(self, text: str) -> bool:
        t = (text or "").lower().strip(" .!?,")
        return t in self.STOP_PHRASES

    def start_hands_free(self, persist: bool = True) -> None:
        """Enter the continuous conversation loop: mic reopens after every
        turn until a stop phrase / mic tap / idle timeout."""
        if persist and not self.config.hands_free:
            self.config.hands_free = True
            self.config.save()
        if self._hands_free_active:
            return
        self._hands_free_active = True
        self._garble_streak = 0
        devlog.log("Hands-free conversation started.")
        self._emit_hands_free(True)
        self._reset_idle_timer()
        self._hands_free_continue()

    def stop_hands_free(self, reason: str = "", signoff: bool = True,
                        persist: bool = True) -> None:
        if not self._hands_free_active:
            if persist and self.config.hands_free:
                self.config.hands_free = False
                self.config.save()
            return
        self._hands_free_active = False
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if persist and self.config.hands_free:
            self.config.hands_free = False
            self.config.save()
        devlog.log(f"Hands-free conversation ended ({reason}).")
        self._emit_hands_free(False)
        if self.recorder.recording or self._active_stream is not None:
            self.cancel_recording()
        if signoff:
            self.speech.speak_async("I'll be right here when you need me.")
        self.set_state("ready")

    def _hands_free_continue(self) -> None:
        """Reopen the mic for the next turn if the loop is still active."""
        if not self._hands_free_active:
            return

        def worker():
            import time as _t
            from app.voice import audio_gate
            _t.sleep(audio_gate.TAIL_SECONDS + 0.1)  # let echo tail clear
            if not self._hands_free_active:
                return
            if self.recorder.recording or self.pipeline.is_processing_command \
                    or self.speech.speaking or self.pipeline.pending is not None:
                return
            self._ui(lambda: self.toggle_mic("voice"))
        threading.Thread(target=worker, daemon=True).start()

    def _reset_idle_timer(self) -> None:
        """(Re)start the total-silence timeout; a real turn resets it."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        timeout = float(getattr(self.config, "hands_free_idle_timeout_s", 45.0))
        self._idle_timer = threading.Timer(timeout, self._on_idle_timeout)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _on_idle_timeout(self) -> None:
        if self._hands_free_active:
            self.stop_hands_free("idle timeout", signoff=True)

    def _hands_free_handle_final(self, text: str, confidence=1.0) -> bool:
        """Loop-specific handling of a transcript before routing. Returns True
        if the pipeline should NOT process it (handled here: stop phrase,
        empty/garble). Only meaningful while the loop is active."""
        if not self._hands_free_active:
            return False
        from app.agent.normalizer import normalize_command
        cleaned = normalize_command(text, self.config).cleaned
        if cleaned and self._is_stop_phrase(cleaned):
            self.show_user(cleaned)
            self.stop_hands_free("you asked me to stop", signoff=True)
            return True
        is_garble = (not cleaned) or _confidence_is_low(
            confidence, getattr(self.config.stt, "garble", None))
        if is_garble:
            # Don't count as a turn, don't nag every silence — just keep
            # listening. After 3 in a row, check in once.
            self._garble_streak += 1
            devlog.log(f"Hands-free: ignored empty/low-confidence final "
                       f"(streak {self._garble_streak}).")
            speaks = self.config.voice_enabled and self.config.tts_backend != "off"
            if self._garble_streak == 3 and speaks:
                # check in once; the speech-finished hook reopens the mic
                self.speech.speak_async(
                    "I'm having trouble hearing you — still there?")
            else:
                self._hands_free_continue()
            return True
        self._garble_streak = 0
        self._reset_idle_timer()
        return False

    def _emit_hands_free(self, active: bool) -> None:
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("hands_free", {"active": bool(active)})

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

        # Honest STT status: one clear warning if the user chose streaming but
        # it can't run, plus a persistent amber chip (never a per-turn warning).
        stt_state, stt_reason = self.stt_router.streaming_status()
        if stt_state in ("unavailable", "degraded") and not self._streaming_warned:
            self._streaming_warned = True
            devlog.warn(f"Streaming STT not active: {stt_reason}")
            issues.append(f"Streaming speech isn't running — {stt_reason} "
                          "Anna is using local Whisper meanwhile.")

        self._push_chips(model_state)

        # One-line capability summary so the active paths are unambiguous.
        stt_cap = ("streaming (deepgram)" if stt_state == "streaming"
                   else f"local whisper ({self.config.faster_whisper_model})")
        brain_cap = (f"groq+ollama" if self.agent.brain.mode() == "hybrid"
                     else "ollama (local)")
        tts_cap = self.config.tts_backend
        devlog.log(f"Capabilities — STT: {stt_cap} | brain: {brain_cap} | "
                   f"TTS: {tts_cap}")

        if issues:
            self._ui(self.ui.show_setup_card, issues, self._on_recheck)
        else:
            self._ui(self.ui.hide_setup_card)

    def _on_brain_state(self) -> None:
        self._push_chips(self.chips.get("model", {}).get("state", "offline"))

    def _on_first_audio(self, ms: float) -> None:
        self.last_tts_first_audio_ms = ms
        devlog.log(f"tts_first_audio_ms: {ms:.0f}ms (reply ready -> first sound)")
        # Consolidated conversational metric (9D): "I stop speaking" -> "Anna's
        # first audible word" = the number that proves the Mira feel.
        if self._turn_t0 is not None:
            turn_ms = (time.perf_counter() - self._turn_t0) * 1000
            self._turn_t0 = None
            brain = getattr(self.agent, "brain", None)
            first_tok = getattr(getattr(brain, "last", None), "first_token_ms", 0) or 0
            # REAL measured stt_ms, labeled by the provider that actually ran —
            # never an aspirational estimate (9.1B).
            stt = self._last_turn_stt_ms
            stt_label = "deepgram" if self._last_turn_stt_provider == "deepgram" else "local"
            devlog.log(f"turn_latency_ms: {turn_ms:.0f}ms  "
                       f"(stt({stt_label})~{stt:.0f} + route+llm_first_token~{first_tok:.0f} "
                       f"+ tts_first_audio~{ms:.0f})")
            if hasattr(self.ui, "dispatch"):
                self.ui.dispatch("turn_latency", {"ms": round(turn_ms)})

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
        if backend == "deepgram":
            from app.voice.tts_deepgram import deepgram_tts_available
            if not deepgram_tts_available(self.config):
                voice_label, voice_state = "Voice: Piper (Deepgram: no key)", "warn"
            elif getattr(self.speech, "deepgram_tts_unhealthy", False):
                voice_label, voice_state = "Voice: Piper (Deepgram error)", "warn"
            else:
                short = self.config.tts_deepgram_model.replace("aura-2-", "").replace("aura-", "").replace("-en", "")
                voice_label, voice_state = f"Voice: Deepgram · {short}", "ok"
        elif backend == "kokoro" and getattr(self, "_kokoro_ok", False):
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
        # Honest STT chip: only shown when the user opted into streaming, so
        # they can see at a glance whether it's actually running (9.1B).
        stt_state, _reason = self.stt_router.streaming_status()
        if stt_state == "streaming":
            self.chips["stt"] = {"label": "STT: Streaming (Deepgram)", "state": "ok"}
        elif stt_state == "degraded":
            self.chips["stt"] = {"label": "STT: Local (Deepgram error)", "state": "warn"}
        elif stt_state == "unavailable":
            self.chips["stt"] = {"label": "STT: Local (streaming unavailable)",
                                 "state": "warn"}
        # stt_state == "local" (streaming off): no chip, mic chip covers it
        # Engine chip (10C): which conversation engine handles voice turns.
        engine, _reason = self.engine_selector.choose()
        if engine == "gemini_live":
            self.chips["engine"] = {"label": "Engine: Gemini Live",
                                    "state": "live" if self._live is not None
                                             else "ok"}
        elif getattr(self.config, "engine_mode", "pipeline") == "gemini_live":
            # asked for Live, fell back — honest amber per 10C.4
            self.chips["engine"] = {"label": "Engine: Pipeline (Live offline)",
                                    "state": "warn"}
        elif engine == "local":
            self.chips["engine"] = {"label": "Engine: Local", "state": "local"}
        else:
            self.chips["engine"] = {"label": "Engine: Pipeline", "state": "ok"}
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
        """js_api: begin push-to-talk recording (no-op if already on).
        A tap while the continuous loop runs ends the loop (mic-tap stop)."""
        if self._hands_free_active:
            self.stop_hands_free("you tapped the mic", signoff=False)
            return
        if not self.recorder.recording:
            self.toggle_mic()

    def stop_ptt(self) -> None:
        """js_api: stop recording and transcribe (no-op if not recording)."""
        if self._hands_free_active:
            self.stop_hands_free("you tapped the mic", signoff=False)
            return
        if self.recorder.recording:
            self.toggle_mic()

    def toggle_mic(self, source: str = "voice") -> None:
        with self._mic_lock:
            if self._live is not None:
                # One action ends the whole Live conversation (10A.3/10C).
                self._end_live_conversation("you tapped the mic")
                return
            if not self.recorder.recording:
                if self.speech.speaking:
                    # Barge-in: pressing PTT while Anna talks cuts her off
                    # and starts listening immediately (sec 13a). Also abort
                    # any in-flight chat stream so Groq stops generating (9B).
                    devlog.log("Barge-in: cancelling TTS + aborting stream.")
                    self.pipeline.abort_stream()
                    self.speech.cancel()
                if self.pipeline.is_processing_command:
                    self.show_info("One moment — I'm finishing the last command.")
                    return
                # 10C engine selection. Rules still short-circuit locally in
                # every engine; typed input never comes through here at all.
                engine, live_reason = self.engine_selector.choose()
                if engine == "gemini_live":
                    self._begin_live_conversation(source)
                    return
                if live_reason and not self._live_fallback_noted:
                    self._live_fallback_noted = True
                    self.show_info(f"Gemini Live isn't available right now "
                                   f"({live_reason}) — using the regular pipeline.")
                streaming = self.stt_router.use_streaming()
                # Start recording IMMEDIATELY — the local buffer is the safety
                # net and captures audio with no delay. Deepgram (if on) then
                # connects in the background and replays the buffered frames, so
                # a slow/failed cloud connection never delays the mic or loses
                # your first words.
                try:
                    self.recorder.start(on_auto_stop=lambda: self._ui(self.toggle_mic))
                except MicrophoneError as e:
                    self.show_error(str(e))
                    return
                self._record_source = source
                self.set_state("listening")
                self._ui(self.ui.set_mic_active, True)
                if streaming:
                    self._begin_streaming_stt_async()
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

    # ------------------------------------------- Gemini Live engine (10C)
    def _begin_live_conversation(self, source: str = "voice") -> None:
        """Open the mic and a Gemini Live session: one continuous
        conversation (the model handles turn-taking) until the mic is tapped
        again or a failure falls back to the pipeline. The recorder keeps a
        rolling local buffer as the same-turn fallback safety net."""
        from app.voice.live_engine import LiveEngine
        if self.speech.speaking:
            self.pipeline.abort_stream()
            self.speech.cancel()
        try:
            self.recorder.start(
                on_auto_stop=None,   # continuous: Gemini VADs the turns
                rolling_seconds=max(15.0, float(self.config.max_record_seconds)))
        except MicrophoneError as e:
            self.show_error(str(e))
            return
        self._record_source = source
        self.set_state("thinking", "Connecting to Gemini Live…")
        live = LiveEngine(self.config, self.agent, self.history,
                          self.pipeline, self.engine_selector,
                          memory=self.memory,
                          show_user=self.show_user,
                          show_anna=self.show_anna,
                          show_result=self.show_result,
                          on_failure=self._on_live_failure,
                          on_cost=self._on_live_cost,
                          on_idle=self._on_live_idle,
                          notify=self.show_info)
        self._live = live   # set before connect so a tap can abort it

        def worker():   # session connect blocks up to ~10s — never the GUI
            if not live.begin(self.recorder):
                self._live = None
                self.show_info("Couldn't reach Gemini Live — this turn uses "
                               "the regular pipeline.")
                self._begin_pipeline_recording_after_live_failure()
                return
            self._ui(self.ui.set_mic_active, True)
            self._emit_live_indicator(True)
            self.set_state("listening", "Live — just talk")
            self._refresh_chips()
        threading.Thread(target=worker, daemon=True,
                         name="anna-live-begin").start()

    def _begin_pipeline_recording_after_live_failure(self) -> None:
        """The user's mic tap still works: restart as a normal pipeline
        recording (nothing was spoken yet, so nothing is lost)."""
        self.recorder.cancel()
        try:
            self.recorder.start(on_auto_stop=lambda: self._ui(self.toggle_mic))
        except MicrophoneError as e:
            self.show_error(str(e))
            self.set_state("ready")
            return
        self.set_state("listening")
        self._ui(self.ui.set_mic_active, True)
        if self.stt_router.use_streaming():
            self._begin_streaming_stt_async()
        self._refresh_chips()

    def _end_live_conversation(self, reason: str) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.end(reason)
            devlog.log(f"Live conversation ended ({reason}) — {live.stats()}")
        if self.recorder.recording:
            self.recorder.cancel()
        self._ui(self.ui.set_mic_active, False)
        self._emit_live_indicator(False)
        self.set_state("ready")
        self._refresh_chips()

    def _on_live_failure(self, detail: str) -> None:
        """Same-turn fallback (10C.2): tear down Live and run whatever the
        rolling mic buffer holds through local Whisper -> the pipeline, so
        the user's turn is not lost. The failure was already counted."""
        live, self._live = self._live, None
        if live is None:
            return   # a mic tap raced the failure — already ended
        live.end(f"failure: {detail}")
        self._ui(self.ui.set_mic_active, False)
        self._emit_live_indicator(False)
        self.show_info("Live hiccuped — I'm finishing that with my regular "
                       "pipeline.")
        self._refresh_chips()
        if self.recorder.recording:
            self.set_state("transcribing")
            generation = self._stt_generation
            threading.Thread(target=self._finish_recording,
                             args=(generation,), daemon=True).start()
        else:
            self.set_state("ready")

    # ----------------------------------------------------- vision (11B)
    def _vision_dispatch(self, event: str, payload: dict) -> None:
        """Screen-vision / camera indicators. These are the only signal the
        user needs that something is being captured, so they must always fire."""
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch(event, payload)

    def _stop_live_for_privacy(self) -> bool:
        """privacy_mode also kills any Gemini Live audio session (11B.5)."""
        if self._live is None:
            return False
        self._end_live_conversation("privacy mode")
        return True

    def _open_browser_camera(self):
        """Mira's pattern: the WebView opens getUserMedia, draws ONE frame,
        stops every track, and hands back a data URL."""
        from app.vision.camera import BrowserCameraStream
        if not hasattr(self.ui, "dispatch"):
            from app.vision import CameraUnavailable
            raise CameraUnavailable("No window to open the camera in.")
        self._camera_seq += 1
        request_id = self._camera_seq
        done = threading.Event()
        self._camera_frames[request_id] = [done, ""]

        def request(timeout_s: float) -> str:
            self.ui.dispatch("camera_capture", {
                "id": request_id,
                "device": getattr(self.config, "camera_device", "") or "",
                "preview": bool(getattr(self.config, "camera_preview", True))})
            if not done.wait(timeout_s):
                self._camera_frames.pop(request_id, None)
                from app.vision import CameraUnavailable
                raise CameraUnavailable(
                    "The camera didn't respond — check the window's camera "
                    "permission.")
            return self._camera_frames.pop(request_id, [None, ""])[1]

        return BrowserCameraStream(
            request, stop_fn=lambda: self.ui.dispatch("camera_stop", {}))

    def camera_frame(self, request_id, data_url: str) -> None:
        """js_api: the single frame the browser captured. Raw pixels are never
        logged — only that a frame of N bytes arrived."""
        entry = self._camera_frames.get(int(request_id))
        if entry is None:
            return
        entry[1] = str(data_url or "")
        devlog.log(f"Vision: camera frame received ({len(entry[1])} b64 chars)")
        entry[0].set()

    def privacy_mode(self) -> None:
        """js_api / voice: the one switch that stops screen watching, the
        camera, and any Live audio session."""
        stopped = self.vision.privacy_mode()
        parts = [name for name, was_on in stopped.items() if was_on]
        self.show_info("Privacy mode — " + (", ".join(parts) + " stopped."
                                            if parts else "nothing was running."))

    def _emit_live_indicator(self, active: bool) -> None:
        """Unmistakable 'audio is streaming to Google' badge (10C; 10D adds
        the running session-cost estimate to it)."""
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("live_streaming", {"active": bool(active)})

    def _on_live_cost(self, payload: dict) -> None:
        """~1/s from the live engine: session audio minutes + cost estimate
        for the badge and dev tools (10D.2)."""
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("live_cost", payload)

    def _on_live_idle(self) -> None:
        idle_s = float(getattr(self.config, "live_idle_close_s", 60.0) or 0.0)
        self._end_live_conversation("idle auto-close")
        self.show_info(f"Live session closed after {idle_s:.0f}s of quiet — "
                       "tap the mic when you want me again.")

    def live_consent(self, accepted) -> None:
        """js_api: resolve the first-run Gemini Live consent card (10D.1)."""
        if bool(accepted):
            self.config.live_audio_consent = True
            self.config.save()
            devlog.log("Gemini Live consent granted (billing + continuous audio).")
            self.show_info("Gemini Live enabled — tap the mic to start a "
                           "live conversation.")
        else:
            self.config.engine_mode = "pipeline"
            self.config.save()
            devlog.log("Gemini Live consent declined — engine back to pipeline.")
            self.show_info("No problem — staying on the pipeline. Audio keeps "
                           "working exactly as before.")
        self._refresh_chips()

    def _refresh_chips(self) -> None:
        self._push_chips(self.chips.get("model", {}).get("state", "offline"))

    # ------------------------------------------------ streaming STT (9A)
    def _begin_streaming_stt_async(self) -> None:
        """Connect Deepgram off the mic path. On success, replay the frames
        captured so far and attach the live observer; on failure, count it for
        the circuit breaker and let local Whisper handle the turn (the buffer
        is already recording). Never blocks the mic."""
        generation = self._stt_generation

        def worker():
            try:
                stream = self.stt_router.deepgram.start_stream(
                    on_partial=self._on_stt_partial,
                    on_final=self._on_stt_final,
                    on_error=self._on_stt_error)
            except Exception as e:
                # connection/gate failure -> circuit + clean local fallback
                self.stt_router.record_failure(" ".join(str(e).split())[:120])
                return
            # user may have stopped (or a newer turn started) while connecting
            if generation != self._stt_generation or not self.recorder.recording:
                stream.close()
                return
            self._active_stream = stream
            self._stream_generation = generation
            buffered = self.recorder.buffered_pcm()
            if buffered:
                stream.send_audio(buffered)   # replay early frames
            self.recorder.set_frame_observer(stream.send_audio)
            devlog.log("Streaming STT: Deepgram connected (live audio).")
            self._emit_streaming_indicator(True)
        threading.Thread(target=worker, daemon=True, name="anna-dg-connect").start()

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
        if self._hands_free_handle_final(result.text, result.confidence):
            return   # stop phrase / garble handled by the loop
        if not result.text:
            self.show_info("I didn't catch that clearly. Try once more?")
            self.set_state("ready")
            return
        self._start_turn_clock(result.stt_ms, provider=result.provider or "deepgram")
        self.pipeline.submit(result.text, source=self._record_source,
                             confidence=result.confidence, stt_ms=result.stt_ms)

    def _start_turn_clock(self, stt_ms: float, provider: str = "local") -> None:
        """Anchor turn_latency_ms at transcript-ready for voice turns (9D).
        Records the REAL STT provider so the breakdown never lies (9.1B)."""
        self._turn_t0 = time.perf_counter()
        self._last_turn_stt_ms = stt_ms or 0.0
        self._last_turn_stt_provider = provider

    def _on_audio_level(self, level: float) -> None:
        """Forward the TTS amplitude envelope to the orb (High tier only)."""
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("speaking_level", {"level": round(level, 3)})

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
        if self._hands_free_handle_final(result.text, result.confidence):
            return   # stop phrase / garble handled by the loop
        if not result.text:
            # Empty STT must never set the busy state (sec 8/9).
            self.show_info("I didn't catch that clearly. Try once more?")
            self.set_state("ready")
            return
        self._start_turn_clock(result.stt_ms)
        self.pipeline.submit(result.text, source=self._record_source,
                             confidence=result.confidence, stt_ms=result.stt_ms)

    def _transcribe_recording(self):
        """Stop recording and return text plus STT confidence metadata."""
        from app.voice.stt_whisper import TranscriptionResult
        source_rate = getattr(self.recorder, "capture_sample_rate",
                              self.config.sample_rate)
        data = self.recorder.stop()
        if data is None or len(data) < source_rate // 4:
            return TranscriptionResult(text="")
        import numpy as np
        peak = float(np.max(np.abs(data.astype(np.float32)))) / 32768.0
        if peak < 0.05 and not self._low_mic_warned:
            self._low_mic_warned = True
            devlog.warn(f"Low microphone level detected (peak: {peak * 100:.1f}%).")
            self.show_info("Your mic level seems very low — check Windows input volume.")
        from app.voice.recorder import normalize_audio_for_stt
        data = normalize_audio_for_stt(data, source_rate, 16000)
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
        """Record ~3s and route the raw utterance through the confirmation
        manager (11A). It — not this method — decides approve/cancel/repeat/
        details, and enforces the strong phrase on destructive-tier actions,
        so this button can never become a softer approval path."""
        def worker():
            if not self.pipeline.confirm.has_pending():
                self.show_info("There's nothing waiting for your approval.")
                return
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
            devlog.log(f"Voice confirm heard: {text!r}")
            outcome = self.pipeline.handle_confirmation_utterance(text)
            if outcome in (Outcome.APPROVED, Outcome.CANCELLED):
                return          # the pipeline already resolved + reset state
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
        from app.voice.wake_word import make_wake_word, WakeWordUnavailable
        try:
            self.wake_listener = make_wake_word(self.config, self._on_wake)
            self.wake_listener.start()
            if not self.config.wake_word_enabled:
                self.config.wake_word_enabled = True
                self.config.save()
            self._ui(self.ui.set_wake_switch, True)
            if getattr(self.config, "wake_word_backend", "whisper") == "openwakeword":
                self.show_info('Wake word on — say "Hey Jarvis" to wake me.')
            else:
                names = " or ".join(f'"{p.title()}"' for p in
                                    (self.config.wake_word_phrases or ["Anna"])[:2])
                self.show_info(f"Wake word on — just say {names} to wake me.")
        except WakeWordUnavailable as e:
            self.wake_listener = None
            self._ui(self.ui.set_wake_switch, False)
            if self.config.wake_word_enabled:
                self.config.wake_word_enabled = False
                self.config.save()
            devlog.warn(str(e))
            if not self._wake_warned:
                self._wake_warned = True
                self.show_info("Wake word couldn't start — details are in "
                               "Developer Tools. I turned the toggle off for now.")

    def _on_wake(self) -> None:
        # Provably deaf while paused: even if a wake stream is mid-teardown and
        # fires once more, it does NOTHING here — no mic, no turn, no row.
        if self._listening_paused:
            return
        if not self.pipeline.is_processing_command and not self.recorder.recording:
            self._ui(self.toggle_mic, "wake_word")

    def set_listening_paused(self, paused: bool) -> None:
        """Tray 'Pause listening'. Makes Anna deaf to the wake word: the wake
        listener is STOPPED (its mic released), and `_on_wake` is gated as a
        belt-and-suspenders. Resume restarts the wake listener iff it had been
        on. Push-to-talk from the window is a deliberate user act and is left
        alone — pausing is about ambient listening, not the button."""
        paused = bool(paused)
        if paused == self._listening_paused:
            self._emit_paused_indicator(paused)   # idempotent echo (re-hydrate)
            return
        self._listening_paused = paused
        if paused:
            self._wake_was_on = self.wake_listener is not None
            if self.wake_listener is not None:
                self.wake_listener.stop()
                self.wake_listener = None
                self._sync_toggle("wake_word", False)
            devlog.log("Listening paused (tray) — wake word off, mic released.")
        else:
            if self._wake_was_on and self.wake_listener is None:
                self.toggle_wake_word_on()        # restarts + re-syncs the switch
            devlog.log("Listening resumed (tray).")
        self._emit_paused_indicator(paused)

    def _emit_paused_indicator(self, paused: bool) -> None:
        if hasattr(self.ui, "dispatch"):
            self.ui.dispatch("listening_paused", {"paused": bool(paused)})

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
        elif name == "hands_free":
            if value:
                self.start_hands_free()
            else:
                self.stop_hands_free("you turned it off", signoff=False)
            self._sync_toggle("hands_free", self._hands_free_active)
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
                        "voice": self.config.voice_enabled,
                        "hands_free": self._hands_free_active},
            "conversation": self.conversation.snapshot(),
            "devlog": devlog.entries(150),
            "pending": self.pipeline.pending_payload(),
            "hotkey": self.config.push_to_talk_hotkey,
            "assistant": self.config.assistant_nickname,
            "prefs": {"animation_quality": self.config.animation_quality},
            "listening_paused": self._listening_paused,
        })

    def _allowed_path(self, path: str):
        """Return a resolved Path if it's inside the screenshot dir or a safe
        folder, else None (the whitelist for all file actions)."""
        from pathlib import Path as P
        target = P(path or "")
        if not path or not target.exists():
            return None
        roots = [P(self.config.screenshot_dir)] + \
                [P(f) for f in self.config.safe_folders]
        for root in roots:
            try:
                if target.resolve().is_relative_to(root.resolve()):
                    return target
            except (OSError, ValueError):
                continue
        return None

    def open_path(self, path: str) -> None:
        """js_api: open a file Anna produced (View button)."""
        import os
        target = self._allowed_path(path)
        if target is None:
            devlog.warn(f"open_path refused (outside allowed roots): {path}")
            return
        os.startfile(str(target))  # noqa: S606 — whitelisted roots

    def reveal_path(self, path: str) -> None:
        """js_api: reveal a file in Explorer (Open folder button)."""
        import subprocess
        target = self._allowed_path(path)
        if target is None:
            devlog.warn(f"reveal_path refused: {path}")
            return
        subprocess.Popen(["explorer", "/select,", str(target)])

    def copy_image(self, path: str) -> None:
        """js_api: copy an image file to the Windows clipboard (Copy button)."""
        target = self._allowed_path(path)
        if target is None:
            devlog.warn(f"copy_image refused: {path}")
            return
        def worker():
            try:
                import io
                from PIL import Image
                with Image.open(str(target)) as img:
                    out = io.BytesIO()
                    img.convert("RGB").save(out, "BMP")
                    dib = out.getvalue()[14:]   # strip 14-byte BMP header -> DIB
                import ctypes
                CF_DIB = 8
                u = ctypes.windll.user32; k = ctypes.windll.kernel32
                if not u.OpenClipboard(0):
                    return
                try:
                    u.EmptyClipboard()
                    h = k.GlobalAlloc(0x2000, len(dib))   # GMEM_DDESHARE
                    p = k.GlobalLock(h)
                    ctypes.memmove(p, dib, len(dib))
                    k.GlobalUnlock(h)
                    u.SetClipboardData(CF_DIB, h)
                finally:
                    u.CloseClipboard()
                self.show_info("Screenshot copied to clipboard.")
            except Exception as e:
                devlog.warn(f"copy_image failed: {' '.join(str(e).split())[:150]}")
        threading.Thread(target=worker, daemon=True).start()

    def save_image_as(self, path: str) -> None:
        """js_api: native Save-As dialog to copy the screenshot elsewhere."""
        target = self._allowed_path(path)
        if target is None:
            devlog.warn(f"save_image_as refused: {path}")
            return
        def worker():
            try:
                dest = self._native_save_dialog(target.name)
                if dest:
                    import shutil
                    shutil.copy2(str(target), dest)
                    devlog.log(f"Screenshot saved to {dest}")
            except Exception as e:
                devlog.warn(f"save_image_as failed: {' '.join(str(e).split())[:150]}")
        threading.Thread(target=worker, daemon=True).start()

    def _native_save_dialog(self, default_name: str) -> str:
        """Windows Save-As dialog via the webview if available, else None."""
        try:
            import webview
            windows = getattr(webview, "windows", None)
            if windows:
                result = windows[0].create_file_dialog(
                    webview.SAVE_DIALOG, save_filename=default_name)
                if result:
                    return result if isinstance(result, str) else result[0]
        except Exception as e:
            devlog.warn(f"save dialog unavailable: {e}")
        return ""

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
                "tts_deepgram_model": c.tts_deepgram_model,
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
                "stt_stream_state": self.stt_router.streaming_status()[0],
                "stt_stream_reason": self.stt_router.streaming_status()[1],
                # Conversation engine (10C) — the raw key NEVER goes to
                # the frontend.
                "engine_mode": c.engine_mode,
                "engine_rules_first": c.engine_rules_first,
                "live_audio_consent": c.live_audio_consent,
                "gemini_key_masked": self._masked_gemini_key(),
                "gemini_key_set": self._gemini_key_set(),
                "gemini_live_model": c.gemini_live_model,
                "live_state_reason": self.engine_selector.last_reason,
                # 10D: voice, affect, cost transparency
                "gemini_live_voice": c.gemini_live_voice,
                "live_affective_dialog": c.live_affective_dialog,
                "live_price_in_per_min": c.live_price_in_per_min,
                "live_price_out_per_min": c.live_price_out_per_min,
                "live_idle_close_s": c.live_idle_close_s,
                "live_monthly_cap_usd": c.live_monthly_cap_usd,
                "live_month_spend": self._live_month_spend(),
                # Vision (11B) — capture is trigger-only; cloud is opt-in.
                "cloud_vision_consent": c.cloud_vision_consent,
                "vision_cloud_model": c.vision_cloud_model,
                "screen_watch_interval_s": c.screen_watch_interval_s,
                "screen_watch_idle_timeout_s": c.screen_watch_idle_timeout_s,
                "vision_save_captures": c.vision_save_captures,
                "camera_device": c.camera_device,
                "camera_preview": c.camera_preview,
                "ocr_ready": self._ocr_ready(),
            })

    def _ocr_ready(self) -> bool:
        from app.vision.ocr import ocr_status
        return ocr_status(self.config)[0]

    def _masked_groq_key(self) -> str:
        from app.llm.providers import mask_key
        import os
        return mask_key(os.environ.get("GROQ_API_KEY")
                        or self.config.groq_api_key)

    def _masked_gemini_key(self) -> str:
        from app.llm.providers import mask_key
        from app.voice.gemini_live import gemini_key
        return mask_key(gemini_key(self.config))

    def _gemini_key_set(self) -> bool:
        from app.voice.gemini_live import gemini_key
        return bool(gemini_key(self.config))

    @staticmethod
    def _live_month_spend() -> float:
        try:
            from app.voice.live_cost import month_spend
            return round(month_spend(), 2)
        except Exception:
            return 0.0

    _SETTINGS_FIELDS = {
        "ollama_url": str, "ollama_model": str, "chat_model": str,
        "ollama_timeout": int,
        "animation_quality": str,
        "tts_backend": str, "piper_exe": str, "piper_voice": str,
        "piper_length_scale": float,
        "kokoro_model": str, "kokoro_voices": str, "kokoro_voice": str,
        "tts_deepgram_model": str,
        "tts_rate": float, "tts_volume": int,
        "faster_whisper_model": str, "microphone_device": str,
        "stt_language": str,
        "silence_seconds": float, "max_record_seconds": int,
        "brain_mode": str, "cloud_model": str, "cloud_timeout_s": float,
        "allow_clipboard_to_cloud": bool,
        "hands_free_followup": bool,
        "stt_mode": str, "deepgram_model": str,
        "wake_word_backend": str, "wake_word_model": str,
        "engine_mode": str, "engine_rules_first": bool,
        "live_audio_consent": bool, "gemini_live_model": str,
        "gemini_live_voice": str, "live_affective_dialog": bool,
        "live_price_in_per_min": float, "live_price_out_per_min": float,
        "live_idle_close_s": float, "live_monthly_cap_usd": float,
        "cloud_vision_consent": bool, "vision_cloud_model": str,
        "screen_watch_interval_s": float, "screen_watch_idle_timeout_s": float,
        "vision_save_captures": bool, "confirmation_voice_listen": bool,
        "confirmation_timeout_s": float,
        "camera_device": str, "camera_preview": bool, "live_native_camera": bool,
    }
    _SETTINGS_CHOICES = {
        "animation_quality": {"low", "medium", "high"},
        "tts_backend": {"auto", "piper", "kokoro", "deepgram", "windows", "off"},
        "kokoro_voice": {"af_heart", "af_bella"},
        "tts_deepgram_model": {"aura-2-delia-en", "aura-2-luna-en",
                               "aura-asteria-en", "aura-luna-en"},
        "faster_whisper_model": {"tiny", "base", "base.en", "small", "small.en"},
        "stt_language": {"auto", "en", "hi", "mr"},
        "brain_mode": {"hybrid", "local_only"},
        "stt_mode": {"streaming", "local"},
        "wake_word_backend": {"whisper", "openwakeword"},
        "wake_word_model": {"tiny", "base", "small"},
        "engine_mode": {"gemini_live", "pipeline", "local"},
        # Warm/female-leaning HD voices verified 2026-07 (full 30-voice TTS
        # set works on native-audio Live models; this is a curated subset).
        "gemini_live_voice": {"Sulafat", "Aoede", "Leda", "Vindemiatrix",
                              "Achernar", "Autonoe", "Zephyr", "Kore",
                              "Callirrhoe", "Despina"},
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
        if "gemini_api_key" in settings:
            key = str(settings["gemini_api_key"] or "").strip()
            if "..." not in key and "•" not in key \
                    and key != self.config.gemini_api_key:
                self.config.gemini_api_key = key
                changed.append("gemini_api_key (hidden)")
        if changed:
            self.config.save()
            devlog.log(f"Settings changed: {', '.join(changed)}")
            self.show_info("Settings saved.")
            # First-time Live enablement (10D.1): picking the engine is not
            # consent — show the plain-language card and require an explicit
            # yes before any audio can stream (the session hard-gates too).
            if "engine_mode" in changed \
                    and self.config.engine_mode == "gemini_live" \
                    and not self.config.live_audio_consent \
                    and hasattr(self.ui, "dispatch"):
                self.ui.dispatch("live_consent", {
                    "model": self.config.gemini_live_model,
                    "price_in": self.config.live_price_in_per_min,
                    "price_out": self.config.live_price_out_per_min,
                    "idle_s": self.config.live_idle_close_s,
                })
            self.speech.emit_levels = (self.config.animation_quality == "high")
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

    def validate_deepgram_tts(self) -> None:
        """Settings button: probe Aura; success restores a benched backend."""
        def worker():
            from app.voice.tts_deepgram import validate_deepgram_tts
            ok, message = validate_deepgram_tts(self.config, play=True)
            self._test_result("deepgram_tts", ok, message)
            if ok:
                self.speech.reset_aura_circuit()
            else:
                self.speech.deepgram_tts_unhealthy = True
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
            if self._live is not None:
                # never leave a billed Live session behind (10A.3)
                self._end_live_conversation("app closing")
            try:
                self.vision.stop_watching("app closing")   # never watch on past exit
                self.vision.camera.stop()
            except Exception:
                pass
            try:
                # Release the browser driver. Detaches only — your own Chrome
                # keeps running, with its tabs intact.
                from app.control.playwright_backend import shutdown_browser
                shutdown_browser()
            except Exception:
                pass
            self._hands_free_active = False
            if self._idle_timer is not None:
                self._idle_timer.cancel()
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

    # Phase 0: let a human actually read (and audit) the event log.
    #   --dump-events [N]   show the last N events
    #   --scan-secrets      grep EVERY byte the log wrote for secret shapes
    if "--dump-events" in sys.argv or "--scan-secrets" in sys.argv:
        from app.core.inspect_events import run_cli
        sys.exit(run_cli(sys.argv))

    # Phase 0: headless daemon — the core without a window (commit 4).
    if "--core" in sys.argv:
        from app.core.daemon import run_daemon
        sys.exit(run_daemon(sys.argv))

    # Phase 0: thin window client that talks to a running --core (commit 5).
    if "--ui" in sys.argv:
        from app.anna_ui import run_ui
        sys.exit(run_ui(sys.argv))

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
