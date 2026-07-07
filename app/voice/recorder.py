"""Microphone recorder with optional silence auto-stop.
Runs off the GUI thread; sounddevice/numpy are lazy-imported."""

import threading
import time
import wave
from pathlib import Path


class MicrophoneError(Exception):
    pass


class Recorder:
    def __init__(self, config):
        self.config = config
        self._frames = []
        self._stream = None
        self._recording = False
        self._speech_seen = False
        self._silence_start = None
        self._start_time = 0.0
        self._on_auto_stop = None
        self._device_logged = False

    @property
    def recording(self) -> bool:
        return self._recording

    def start(self, on_auto_stop=None) -> None:
        """Begin recording. on_auto_stop fires (once, from a worker thread)
        when trailing silence or the max duration is reached."""
        if self._recording:
            return
        try:
            import sounddevice as sd
        except Exception as e:
            raise MicrophoneError(f"Audio library unavailable: {e}") from e

        self._frames = []
        self._speech_seen = False
        self._silence_start = None
        self._on_auto_stop = on_auto_stop
        self._start_time = time.time()
        try:
            self._stream = sd.InputStream(
                samplerate=self.config.sample_rate, channels=1,
                dtype="int16", callback=self._callback)
            self._stream.start()
        except Exception as e:
            self._stream = None
            raise MicrophoneError(f"Could not open the microphone: {e}") from e
        self._recording = True
        if not self._device_logged:
            try:
                from app.agent.devlog import devlog
                device = sd.query_devices(kind="input")
                name = str(device.get("name", "Default microphone"))
                default_rate = int(float(device.get("default_samplerate", 0) or 0))
                devlog.log(f"Microphone input: {name} | capture: "
                           f"{self.config.sample_rate}Hz mono | device: {default_rate}Hz")
                self._device_logged = True
            except Exception:
                pass

    def _callback(self, indata, frames, time_info, status) -> None:
        if not self._recording:
            return
        from app.voice import audio_gate
        if audio_gate.speaking.is_set():
            return  # half-duplex: never capture Anna's own voice (sec 13a)
        import numpy as np
        self._frames.append(indata.copy())
        now = time.time()
        cfg = self.config
        if cfg.silence_auto_stop and self._on_auto_stop is not None:
            rms = float(np.sqrt(np.mean((indata.astype(np.float32) / 32768.0) ** 2)))
            if rms > cfg.silence_threshold:
                self._speech_seen = True
                self._silence_start = None
            elif self._speech_seen:
                if self._silence_start is None:
                    self._silence_start = now
                elif now - self._silence_start >= cfg.silence_seconds:
                    self._fire_auto_stop()
        if now - self._start_time > cfg.max_record_seconds:
            self._fire_auto_stop()

    def _fire_auto_stop(self) -> None:
        cb, self._on_auto_stop = self._on_auto_stop, None
        if cb:
            threading.Thread(target=cb, daemon=True).start()

    def stop(self):
        """Stop recording; returns int16 numpy array or None if empty."""
        self._recording = False
        self._on_auto_stop = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        if not self._frames:
            return None
        import numpy as np
        return np.concatenate(self._frames, axis=0)

    def cancel(self) -> None:
        """Stop recording and discard the captured audio (no transcription)."""
        self.stop()
        self._frames = []

    def save_wav(self, data, path: Path, sample_rate: int = None) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate or self.config.sample_rate)
            wf.writeframes(data.tobytes())


def normalize_audio_for_stt(data, source_rate: int, target_rate: int = 16000):
    """Return int16 mono audio resampled to Whisper's preferred rate."""
    import numpy as np

    audio = np.asarray(data)
    if audio.ndim > 1:
        audio = audio.astype(np.float32).mean(axis=1)
    else:
        audio = audio.astype(np.float32)
    if not len(audio):
        return np.asarray([], dtype=np.int16)
    if source_rate != target_rate:
        output_length = max(1, round(len(audio) * target_rate / source_rate))
        old_positions = np.linspace(0.0, 1.0, len(audio), endpoint=False)
        new_positions = np.linspace(0.0, 1.0, output_length, endpoint=False)
        audio = np.interp(new_positions, old_positions, audio)
    return np.clip(audio, -32768, 32767).astype(np.int16)


def microphone_available() -> bool:
    try:
        import sounddevice as sd
        return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
    except Exception:
        return False
