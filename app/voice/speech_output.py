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
from app.voice import _clean_for_speech, audio_gate

_STOP = object()


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
        text = _clean_for_speech(text)
        if text:
            self._queue.put(text)

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
                if not self._cancel.is_set():
                    # let room echo die before the mic reopens
                    self._cancel.wait(audio_gate.TAIL_SECONDS)
                if self._queue.empty():
                    audio_gate.speaking.clear()
                    self._notify(False)

    # --------------------------------------------------------- backends
    def _speak(self, text: str) -> None:
        backend = self.config.tts_backend
        if backend in ("auto", "piper") and self.config.piper_exe:
            try:
                self._speak_piper(text)
                return
            except Exception as e:
                devlog.warn(f"Piper failed ({e}); falling back to Windows voice.")
        if backend == "piper":
            devlog.warn("Piper selected but not usable — staying silent.")
            return
        self._speak_windows(text)

    def _speak_piper(self, text: str) -> None:
        from app.voice.tts_piper import piper_available
        if not piper_available(self.config):
            raise FileNotFoundError("Piper exe or voice model not found.")
        import tempfile
        wav_path = Path(tempfile.gettempdir()) / "anna_tts.wav"
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [str(self.config.piper_exe), "--model", str(self.config.piper_voice),
             "--output_file", str(wav_path)],
            input=text.encode("utf-8"), capture_output=True,
            timeout=60, creationflags=creation)
        if proc.returncode != 0 or not wav_path.exists():
            raise RuntimeError(f"Piper failed: {(proc.stderr or b'')[:200]!r}")
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
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Gender -eq 'Female' } "
            "| Select-Object -First 1; "
            "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; "
            "$s.Rate = 0; "
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
