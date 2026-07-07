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

# Consecutive Piper synth failures before the backend is benched for the
# session (spec 8B.2 — stops per-sentence retry spam).
TTS_CIRCUIT_FAILURES = 2
TTS_ERROR_LOG = Path(__file__).resolve().parents[1] / "data" / "tts_errors.log"


def _short_error(exc) -> str:
    """One line, max 200 chars — no multi-line tracebacks in the dev log."""
    return " ".join(str(exc).split())[:200]


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
        self.piper_unhealthy = False        # TTS circuit breaker (session)
        self._piper_failures = 0
        self._setup_warned = set()          # one unconfigured-warning per backend
        self.on_tts_health_change = None    # callback() — chip refresh
        self.on_first_audio = None          # callback(ms) — reply-ready -> audible
        self._utterance_start = None        # perf_counter when a reply was queued
        self._first_audio_pending = False
        self.on_audio_level = None          # callback(0..1) — orb reactivity (9D)
        self.emit_levels = False            # gated to the High animation tier
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
        sentences = split_sentences(text)
        if not sentences:
            return
        # Mark the start of a fresh utterance (idle queue + not speaking) so we
        # can measure reply-ready -> first audible sample (tts_first_audio_ms).
        if self._queue.empty() and not self.speaking:
            self._utterance_start = time.perf_counter()
            self._first_audio_pending = True
        for sentence in sentences:
            self._queue.put(sentence)

    def _mark_first_audio(self) -> None:
        """Called at the instant playback of the first sentence begins."""
        if self._first_audio_pending and self._utterance_start is not None:
            self._first_audio_pending = False
            ms = (time.perf_counter() - self._utterance_start) * 1000
            if self.on_first_audio:
                try:
                    self.on_first_audio(ms)
                except Exception:
                    pass

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
                self._log_tts_error(e, context="TTS")
            finally:
                if not self._cancel.is_set() and self._queue.empty():
                    # let room echo die before the mic reopens
                    self._cancel.wait(audio_gate.TAIL_SECONDS)
                if self._queue.empty():
                    audio_gate.speaking.clear()
                    self._notify(False)

    # ------------------------------------------------ TTS circuit breaker
    def _log_tts_error(self, exc, context: str = "TTS") -> None:
        """Truncated one-liner to the dev log; full traceback to a file."""
        devlog.error(f"{context}: {_short_error(exc)}")
        try:
            import traceback
            TTS_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(TTS_ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {context}\n")
                f.write("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
        except Exception:
            pass

    def _piper_failed(self, exc) -> None:
        self._piper_failures += 1
        if self._piper_failures >= TTS_CIRCUIT_FAILURES and not self.piper_unhealthy:
            self.piper_unhealthy = True
            devlog.warn(f"Piper disabled for this session after "
                        f"{self._piper_failures} failures ({_short_error(exc)}) "
                        "— using the Windows voice. Fix the paths in Settings "
                        "and press Validate Piper to restore it.")
            self._log_tts_error(exc, context="Piper (final failure)")
            self._notify_tts_health()
        else:
            devlog.warn(f"Piper failed ({_short_error(exc)}); "
                        "falling back to Windows voice.")

    def _piper_succeeded(self) -> None:
        self._piper_failures = 0

    def reset_piper_circuit(self) -> None:
        """Called after a successful re-probe (Validate Piper button)."""
        self._setup_warned.discard("piper")
        if self.piper_unhealthy or self._piper_failures:
            self._piper_failures = 0
            self.piper_unhealthy = False
            devlog.log("Piper circuit reset — Piper is back.")
            self._notify_tts_health()

    def _notify_tts_health(self) -> None:
        if self.on_tts_health_change:
            try:
                self.on_tts_health_change()
            except Exception:
                pass

    # --------------------------------------------------------- backends
    def _speak(self, text: str) -> None:
        backend = self.config.tts_backend
        if backend == "auto":
            from app.voice.tts_piper import piper_available
            if piper_available(self.config) and not self.piper_unhealthy:
                try:
                    self._speak_piper(text)
                    self._piper_succeeded()
                    return
                except Exception as e:
                    self._piper_failed(e)
            self._speak_windows(text)
            return
        if backend == "piper":
            from app.voice.tts_piper import piper_available
            if not piper_available(self.config):
                if "piper" not in self._setup_warned:
                    self._setup_warned.add("piper")
                    devlog.warn("Piper selected but setup is incomplete — "
                                "using the Windows voice until it validates "
                                "(Settings → Voice output).")
                self._speak_windows(text)
                return
            if self.piper_unhealthy:
                self._speak_windows(text)
                return
            try:
                self._speak_piper(text)
                self._piper_succeeded()
                return
            except Exception as e:
                self._piper_failed(e)
                self._speak_windows(text)
                return
        if backend == "kokoro":
            from app.voice.tts_kokoro import kokoro_available
            if not kokoro_available(self.config):
                if "kokoro" not in self._setup_warned:
                    self._setup_warned.add("kokoro")
                    devlog.warn("Kokoro selected but setup is incomplete — "
                                "using the Windows voice until it validates "
                                "(Settings → Voice output).")
                self._speak_windows(text)
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
        self._mark_first_audio()
        if self.emit_levels and self.on_audio_level:
            self._play_with_levels(wav_path, duration)
            return
        cancelled = self._cancel.wait(duration + 0.1)
        if cancelled:
            winsound.PlaySound(None, winsound.SND_PURGE)

    def _play_with_levels(self, wav_path: Path, duration: float) -> None:
        """Tick a coarse RMS envelope in sync with playback so the orb pulses
        with Anna's actual voice amplitude (9D, High tier only)."""
        import winsound
        env = self._wav_envelope(wav_path)
        start = time.monotonic()
        tick = 0.08
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break
            if self._cancel.wait(tick):
                winsound.PlaySound(None, winsound.SND_PURGE)
                break
            idx = min(len(env) - 1, int(elapsed / max(duration, 1e-3) * len(env)))
            self._safe_level(env[idx] if env else 0.0)
        self._safe_level(0.0)

    def _wav_envelope(self, wav_path: Path, windows: int = None):
        """RMS per ~80ms window, normalized to 0..1. Cheap: short sentences."""
        try:
            import numpy as np
            with wave.open(str(wav_path), "rb") as wf:
                rate = wf.getframerate() or 22050
                frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            if samples.size == 0:
                return []
            win = max(1, int(rate * 0.08))
            n = int(np.ceil(samples.size / win))
            env = []
            for i in range(n):
                chunk = samples[i * win:(i + 1) * win]
                env.append(float(np.sqrt(np.mean(chunk ** 2))) if chunk.size else 0.0)
            peak = max(env) or 1.0
            return [min(1.0, v / peak) for v in env]
        except Exception:
            return []

    def _safe_level(self, level: float) -> None:
        if self.on_audio_level:
            try:
                self.on_audio_level(level)
            except Exception:
                pass

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
        self._mark_first_audio()   # SAPI begins speaking as the process runs
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
