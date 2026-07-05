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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.conversation import Conversation
from app.agent.devlog import devlog
from app.agent.history import History
from app.agent.memory import Memory
from app.agent.pipeline import CommandPipeline
from app.agent.router import Agent
from app.config import AppConfig
from app.voice.recorder import MicrophoneError, Recorder, microphone_available
from app.voice.speech_output import SpeechOutput

APPROVE_WORDS = {"approve", "approved", "yes", "yeah", "yep", "confirm", "go ahead", "do it"}
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

        self.wake_listener = None
        self._wake_warned = False          # one clean warning per session
        self._mic_lock = threading.Lock()
        self._stt_generation = 0           # bumped to drop stale STT results
        self._record_source = "voice"

        if ui is None:
            from app.gui.main_window import MainWindow
            ui = MainWindow(self)
        self.ui = ui

        self.pipeline = CommandPipeline(
            config=self.config, agent=self.agent, history=self.history,
            ui=self, speech=self.speech, cancel_recording=self.cancel_recording)

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
        self._ui(self.ui.transcript.add_assistant, text, self.config.assistant_nickname)
        data = action.get("data")
        if action.get("intent") == "take_screenshot" and data:
            self._ui(self.ui.transcript.add_info, f"Saved to {data}")

    def show_error(self, text: str) -> None:
        self.conversation.add("error", text)
        self._ui(self.ui.transcript.add_error, text)

    def show_info(self, text: str) -> None:
        self.conversation.add("info", text)
        self._ui(self.ui.transcript.add_info, text)

    def set_state(self, state: str, detail: str = "") -> None:
        self._ui(self.ui.set_state, state, detail)

    def ask_confirmation(self, transcript: str, plan, safety) -> None:
        self._ui(self.ui.confirm_panel.show, transcript, plan, safety,
                 self.approve_pending, self.cancel_pending, self.voice_confirm)

    def hide_confirmation(self) -> None:
        self._ui(self.ui.confirm_panel.hide)

    def _on_speaking_changed(self, active: bool) -> None:
        if active:
            self.set_state("speaking")
        elif not self.pipeline.is_processing_command and self.pipeline.pending is None:
            self.set_state("ready")

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
        """Run/re-run dependency checks. Also wired to the setup card's
        Recheck button."""
        issues = []
        if self.agent.llm.is_available():
            models = self.agent.llm.list_models()
            if any(self.config.ollama_model in m for m in models):
                devlog.log(f"Ollama running (model: {self.config.ollama_model}). "
                           "Warming up so the first command is fast...")
                from app.llm.prompt_builder import build_intent_messages
                ms = self.agent.llm.warm_up(
                    build_intent_messages("hello", self.config, self.memory))
                devlog.log(f"Model warm-up done in {ms:.0f}ms." if ms is not None
                           else "Model warm-up failed (will load on first use).")
            else:
                issues.append(f"Ollama is running but '{self.config.ollama_model}' "
                              f"isn't installed. Run: ollama pull {self.config.ollama_model}")
        else:
            issues.append("Ollama is not running. Simple local commands still work, "
                          "but chat/reasoning needs Ollama.")

        from app.voice.stt_whisper import backend_ready
        ok, msg = backend_ready(self.config)
        if ok:
            devlog.log(msg)
        else:
            devlog.warn(msg)
            issues.append("Voice input isn't ready yet — typing still works. "
                          "(Details in Developer Tools.)")
        if not microphone_available():
            devlog.warn("No microphone detected.")
            issues.append("No microphone found — voice input is disabled, but typing works.")
        from app.voice.tts_piper import piper_available
        if not piper_available(self.config):
            devlog.warn("Piper voice not configured — using the built-in Windows voice.")

        if issues:
            self._ui(self.ui.show_setup_card, issues, self._on_recheck)
        else:
            self._ui(self.ui.hide_setup_card)

    def _on_recheck(self) -> None:
        devlog.log("Recheck requested from the setup card.")
        self._ui(self.ui.hide_setup_card)
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
                try:
                    self.recorder.start(on_auto_stop=lambda: self._ui(self.toggle_mic))
                except MicrophoneError as e:
                    self.show_error(str(e))
                    return
                self._record_source = source
                self.set_state("listening")
                self._ui(self.ui.set_mic_active, True)
            else:
                self._ui(self.ui.set_mic_active, False)
                self.set_state("transcribing")
                generation = self._stt_generation
                threading.Thread(target=self._finish_recording,
                                 args=(generation,), daemon=True).start()

    def cancel_recording(self) -> None:
        """Discard any live recording (typed input supersedes voice)."""
        if self.recorder.recording:
            self._stt_generation += 1     # drop the in-flight result
            self.recorder.cancel()
            self._ui(self.ui.set_mic_active, False)
            devlog.log("Recording cancelled (typed input took over).")

    def _finish_recording(self, generation: int) -> None:
        try:
            text = self._transcribe_recording()
        except Exception as e:
            devlog.exception(e, context="STT")
            self.show_error("I couldn't transcribe that — try once more?")
            self.set_state("ready")
            return
        if generation != self._stt_generation:
            devlog.log(f"Stale STT result dropped: {text!r}")
            return
        if not text:
            # Empty STT must never set the busy state (sec 8/9).
            self.show_info("I didn't catch that clearly. Try once more?")
            self.set_state("ready")
            return
        self.pipeline.submit(text, source=self._record_source)

    def _transcribe_recording(self) -> str:
        """Stop the recorder, run STT on a temp WAV, always delete the audio."""
        data = self.recorder.stop()
        if data is None or len(data) < self.config.sample_rate // 4:
            return ""
        wav_path = Path(tempfile.gettempdir()) / "anna_input.wav"
        try:
            self.recorder.save_wav(data, wav_path)
            from app.voice.stt_whisper import transcribe_wav
            return transcribe_wav(str(wav_path), self.config).strip()
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
                text = self._transcribe_recording() or ""
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
    def toggle_voice(self) -> None:
        self.config.voice_enabled = bool(self.ui.voice_switch.get())
        self.config.save()

    def open_settings(self) -> None:
        from app.gui.settings_window import SettingsWindow
        SettingsWindow(self.ui, self.config, self.memory, on_saved=self._on_settings_saved)

    def _on_settings_saved(self) -> None:
        self.ui.title(f"{self.config.assistant_name} "
                      f"({self.config.assistant_nickname}) — local voice assistant")
        self.ui.hotkey_lbl.configure(text=f"Push-to-talk: {self.config.push_to_talk_hotkey}")
        self.show_info("Settings saved. Some changes (hotkey) apply after restart.")

    def show_history(self) -> None:
        self.ui.show_history_window(self.history.recent(50))

    def clear_history(self) -> None:
        self.history.clear()
        self.conversation.clear()
        self.ui.transcript.clear()
        self.show_info("History cleared.")

    def on_close(self) -> None:
        try:
            if self.wake_listener:
                self.wake_listener.stop()
            if self.recorder.recording:
                self.recorder.cancel()
            self.speech.shutdown()
            self.history.close()
        finally:
            self.ui.destroy()


def main() -> None:
    controller = Controller()
    controller.ui.mainloop()


if __name__ == "__main__":
    main()
