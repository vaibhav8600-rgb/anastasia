"""Non-blocking, cancellable TTS worker.

speak_async() queues text and returns immediately; a daemon thread does the
Piper/SAPI work. While audio is playing (plus a 400 ms tail) the half-duplex
gate `audio_gate.speaking` is set so the mic and wake word drop all input.
cancel() supports barge-in: it stops playback, drains the queue and reopens
the mic gate immediately.
"""

import queue
import subprocess
import threading
import time
import wave
from pathlib import Path

from app.agent.devlog import devlog
from app.voice import audio_gate, split_sentences

_STOP = object()


def sapi_rate(rate: float) -> int:
    """Map a 0.5..2.0 speed multiplier onto SAPI's -10..10 scale."""
    return max(-10, min(10, round((float(rate) - 1.0) * 10)))


def piper_length_scale(rate: float) -> float:
    """Piper slows down as length_scale grows; invert the multiplier."""
    return round(1.0 / max(0.25, min(4.0, float(rate) or 1.0)), 2)


class SpeechOutput:
    def __init__(self, config, on_speaking_changed=None):
        self.config = config
        self.on_speaking_changed = on_speaking_changed  # callback(bool)
        self._queue = queue.Queue()
        self._cancel = threading.Event()
        self._proc = None            # active SAPI PowerShell process, if any
        self._proc_lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="anna-tts")
        self._thread.start()

    # ------------------------------------------------------------- API
    @property
    def speaking(self) -> bool:
        return audio_gate.speaking.is_set()

    def speak_async(self, text: str) -> None:
        """Queue text for speech and return immediately. Never raises."""
        if not text or not self.config.voice_enabled or self.config.tts_backend == "off":
            return
        for sentence in split_sentences(text):
            self._queue.put(sentence)

    def cancel(self) -> None:
        """Barge-in: stop current playback, drop queued speech, reopen mic."""
        self._cancel.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        audio_gate.speaking.clear()
        self._notify(False)

    def shutdown(self) -> None:
        self.cancel()
        self._queue.put(_STOP)

    # ---------------------------------------------------------- worker
    def _notify(self, active: bool) -> None:
        if self.on_speaking_changed:
            try:
                self.on_speaking_changed(active)
            except Exception:
                pass

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is _STOP:
                return
            self._cancel.clear()
            audio_gate.speaking.set()   # gate closes BEFORE any audio plays
            self._notify(True)
            try:
                self._speak(text)
            except Exception as e:
                devlog.exception(e, context="TTS")
            finally:
                if not self._cancel.is_set() and self._queue.empty():
                    # let room echo die before the mic reopens
                    self._cancel.wait(audio_gate.TAIL_SECONDS)
                if self._queue.empty():
                    audio_gate.speaking.clear()
                    self._notify(False)

    # --------------------------------------------------------- backends
    def _speak(self, text: str) -> None:
        backend = self.config.tts_backend
        if backend == "auto":
            from app.voice.tts_piper import piper_available
            if piper_available(self.config):
                try:
                    self._speak_piper(text)
                    return
                except Exception as e:
                    devlog.warn(f"Piper failed ({e}); falling back to Windows voice.")
            self._speak_windows(text)
            return
        if backend == "piper":
            from app.voice.tts_piper import piper_available
            if not piper_available(self.config):
                devlog.warn("Piper selected but setup is incomplete — staying silent.")
                return
            try:
                self._speak_piper(text)
                return
            except Exception as e:
                devlog.warn(f"Piper failed ({e}); falling back to Windows voice.")
                self._speak_windows(text)
                return
        if backend == "kokoro":
            from app.voice.tts_kokoro import kokoro_available
            if not kokoro_available(self.config):
                devlog.warn("Kokoro selected but setup is incomplete — staying silent.")
                return
            try:
                self._speak_kokoro(text)
            except Exception as e:
                devlog.warn(f"Kokoro failed ({e}); check Voice settings.")
            return
        self._speak_windows(text)

    def _speak_piper(self, text: str) -> None:
        from app.voice.tts_piper import synthesize_piper
        import tempfile
        wav_path = Path(tempfile.gettempdir()) / \
            f"anna_piper_{threading.get_ident()}.wav"
        synthesize_piper(text, self.config, wav_path)
        if self._cancel.is_set():
            wav_path.unlink(missing_ok=True)
            return
        try:
            self._play_wav_cancellable(wav_path)
        finally:
            wav_path.unlink(missing_ok=True)

    def _speak_kokoro(self, text: str) -> None:
        from app.voice.tts_kokoro import synthesize_kokoro
        import tempfile
        wav_path = Path(tempfile.gettempdir()) / \
            f"anna_kokoro_{threading.get_ident()}.wav"
        synthesize_kokoro(text, self.config, wav_path)
        if self._cancel.is_set():
            wav_path.unlink(missing_ok=True)
            return
        try:
            self._play_wav_cancellable(wav_path)
        finally:
            wav_path.unlink(missing_ok=True)

    def _play_wav_cancellable(self, wav_path: Path) -> None:
        import winsound
        with wave.open(str(wav_path), "rb") as wf:
            duration = wf.getnframes() / float(wf.getframerate() or 1)
        winsound.PlaySound(str(wav_path),
                           winsound.SND_FILENAME | winsound.SND_ASYNC)
        cancelled = self._cancel.wait(duration + 0.1)
        if cancelled:
            winsound.PlaySound(None, winsound.SND_PURGE)

    def _speak_windows(self, text: str) -> None:
        escaped = text.replace("'", "''")
        rate = sapi_rate(getattr(self.config, "tts_rate", 1.0))
        volume = max(0, min(100, int(getattr(self.config, "tts_volume", 100))))
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Gender -eq 'Female' } "
            "| Select-Object -First 1; "
            "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; "
            f"$s.Rate = {rate}; $s.Volume = {volume}; "
            f"$s.Speak('{escaped}')"
        )
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self._proc_lock:
            if self._cancel.is_set():
                return
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation)
        proc = self._proc
        deadline = time.monotonic() + 90
        while proc.poll() is None and time.monotonic() < deadline:
            if self._cancel.wait(0.05):
                try:
                    proc.kill()
                except OSError:
                    pass
                break
        with self._proc_lock:
            self._proc = None
