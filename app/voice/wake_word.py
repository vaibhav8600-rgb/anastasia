"""Wake word listeners.

Two backends, selected by config.wake_word_backend:

* "whisper" (default) — listens for Anna's ACTUAL name ("Anna", "Hey Anna",
  "Anastasia", …) using the local faster-whisper STT. No model training and
  no extra dependency: it VAD-gates the mic and only transcribes a short
  utterance once speech is followed by a brief silence, then fuzzy-matches
  the wake phrases. Fully local.
* "openwakeword" — the pre-trained "Hey Jarvis" model (needs the optional
  openwakeword package). A true custom "Hey Anna" model would need separate
  training; the Whisper backend avoids that entirely.

Both are half-duplex (they ignore audio while Anna is speaking) and fire a
single on_wake() with a cooldown.
"""

import threading
import time

DEFAULT_PHRASES = ["hey anna", "anna", "anastasia", "hey anastasia"]


class WakeWordUnavailable(Exception):
    pass


def make_wake_word(config, on_wake):
    """Factory: build the configured wake-word listener (not started)."""
    backend = getattr(config, "wake_word_backend", "whisper")
    if backend == "openwakeword":
        return OpenWakeWordListener(config, on_wake)
    return WhisperWakeWord(config, on_wake)


# Common Whisper mishears of the names -> treated as a match (word-boundary).
_MISHEAR_WORDS = {"anastasia", "anastasija", "anastasiya", "stasia", "annah"}


def _match_phrase(text: str, phrases) -> bool:
    """True if a transcribed utterance is one of the wake phrases. Word-aware
    plus a fuzzy pass so Whisper mishears still trigger, without firing on
    'banana'/'wanna'/'open notepad'."""
    import re
    norm = re.sub(r"[^a-z\s]", "", (text or "").lower()).strip()
    if not norm:
        return False
    words = norm.split()
    wordset = set(words)
    if wordset & _MISHEAR_WORDS:          # a known name mishear appears whole
        return True
    try:
        from rapidfuzz import fuzz
    except Exception:
        fuzz = None
    for phrase in phrases:
        pw = phrase.split()
        for i in range(len(words) - len(pw) + 1):   # exact word-boundary run
            if words[i:i + len(pw)] == pw:
                return True
        if fuzz is None:
            continue
        # short utterance that is roughly the whole phrase
        if len(words) <= len(pw) + 1 and fuzz.ratio(norm, phrase) >= 82:
            return True
        # the phrase appears (fuzzily) inside a slightly longer utterance,
        # e.g. 'hey im stasia' ~ 'hey anastasia'. Guard against ultra-short
        # transcripts ('n', 'and') that partial-match any long phrase.
        if len(phrase) >= 6 and len(norm) >= 5 \
                and fuzz.partial_ratio(norm, phrase) >= 88:
            return True
    return False


class WhisperWakeWord(threading.Thread):
    """Local name-based wake word — no training, no extra deps."""

    RMS_THRESHOLD = 0.015     # speech energy on 0..1
    SILENCE_CHUNKS = 6        # ~0.6s of trailing silence ends an utterance
    MAX_UTTERANCE_S = 3.0
    COOLDOWN_SECONDS = 2.5

    def __init__(self, config, on_wake):
        super().__init__(daemon=True, name="anna-wakeword")
        self.config = config
        self.on_wake = on_wake
        self._stop_flag = threading.Event()
        self.phrases = [p.lower() for p in
                        (getattr(config, "wake_word_phrases", None) or DEFAULT_PHRASES)]
        try:
            import sounddevice  # noqa: F401
            import faster_whisper  # noqa: F401
        except ImportError as e:
            raise WakeWordUnavailable(
                "Wake word needs faster-whisper and sounddevice "
                "(already required for voice input).") from e

    def run(self) -> None:
        import numpy as np
        import sounddevice as sd
        from faster_whisper import WhisperModel

        from app.voice import audio_gate
        from app.voice.recorder import (choose_capture_sample_rate,
                                        resolve_microphone_device)

        model_size = getattr(self.config, "wake_word_model", "base")
        try:
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
        except Exception as e:
            from app.agent.devlog import devlog
            devlog.warn(f"Wake word model load failed: {e}")
            return

        device_arg, device_info, _warn = resolve_microphone_device(self.config)
        rate = choose_capture_sample_rate(sd, self.config, device_arg, device_info)
        chunk = max(400, int(rate * 0.1))   # 100ms frames
        buf, speech_seen, silence, last_fire = [], False, 0, 0.0
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=chunk, device=device_arg)
        except Exception as first_error:
            fallback = int(float((device_info or {}).get("default_samplerate", 0) or 0))
            if fallback > 0 and fallback != rate:
                try:
                    rate = fallback
                    chunk = max(400, int(rate * 0.1))
                    stream = sd.InputStream(samplerate=rate, channels=1,
                                            dtype="int16", blocksize=chunk,
                                            device=device_arg)
                except Exception as fallback_error:
                    from app.agent.devlog import devlog
                    devlog.warn("Wake word mic unavailable: "
                                f"{fallback_error}")
                    return
            else:
                from app.agent.devlog import devlog
                devlog.warn(f"Wake word mic unavailable: {first_error}")
                return
        with stream:
            while not self._stop_flag.is_set():
                audio, _ = stream.read(chunk)
                if audio_gate.speaking.is_set():   # half-duplex
                    buf, speech_seen, silence = [], False, 0
                    continue
                mono = np.squeeze(audio)
                rms = float(np.sqrt(np.mean((mono.astype(np.float32) / 32768.0) ** 2)))
                if rms > self.RMS_THRESHOLD:
                    speech_seen = True
                    silence = 0
                    buf.append(mono)
                elif speech_seen:
                    silence += 1
                    buf.append(mono)
                    too_long = len(buf) * chunk / rate > self.MAX_UTTERANCE_S
                    if silence >= self.SILENCE_CHUNKS or too_long:
                        if self._check(model, np.concatenate(buf), rate, last_fire):
                            last_fire = time.time()
                        # reset for the next utterance
                        buf, speech_seen, silence = [], False, 0

    def _check(self, model, data, rate, last_fire) -> bool:
        """Transcribe a captured utterance and fire on a phrase match."""
        if time.time() - last_fire < self.COOLDOWN_SECONDS:
            return False
        import numpy as np
        from app.voice.recorder import normalize_audio_for_stt
        try:
            data = normalize_audio_for_stt(data, rate, 16000)
            segments, _info = model.transcribe(
                data.astype(np.float32) / 32768.0, language="en", beam_size=1)
            text = " ".join(s.text for s in segments).strip()
        except Exception:
            return False
        if text and _match_phrase(text, self.phrases):
            from app.agent.devlog import devlog
            devlog.log(f"Wake word matched: {text!r}")
            try:
                self.on_wake()
            except Exception:
                pass
            return True
        return False

    def stop(self) -> None:
        self._stop_flag.set()


class OpenWakeWordListener(threading.Thread):
    """Pre-trained 'Hey Jarvis' model via openWakeWord (optional package)."""

    SCORE_THRESHOLD = 0.6
    COOLDOWN_SECONDS = 3.0

    def __init__(self, config, on_wake):
        super().__init__(daemon=True)
        self.config = config
        self.on_wake = on_wake
        self._stop_flag = threading.Event()
        try:
            import openwakeword  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError as e:
            raise WakeWordUnavailable(
                "The 'Hey Jarvis' wake word needs the optional 'openwakeword' "
                "package: pip install openwakeword. (Or use the 'whisper' "
                "backend to wake on Anna's name with no extra install.)") from e

    def run(self) -> None:
        import numpy as np
        import sounddevice as sd
        from openwakeword.model import Model

        try:
            model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception:
            model = Model()

        from app.voice import audio_gate
        from app.voice.recorder import (choose_capture_sample_rate,
                                        normalize_audio_for_stt,
                                        resolve_microphone_device)

        device_arg, device_info, _warn = resolve_microphone_device(self.config)
        rate = choose_capture_sample_rate(sd, self.config, device_arg, device_info)
        chunk = max(320, int(rate * 0.08))
        last_fire = 0.0
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=chunk, device=device_arg)
        except Exception as first_error:
            fallback = int(float((device_info or {}).get("default_samplerate", 0) or 0))
            if fallback > 0 and fallback != rate:
                try:
                    rate = fallback
                    chunk = max(320, int(rate * 0.08))
                    stream = sd.InputStream(samplerate=rate, channels=1,
                                            dtype="int16", blocksize=chunk,
                                            device=device_arg)
                except Exception as fallback_error:
                    from app.agent.devlog import devlog
                    devlog.warn("OpenWakeWord mic unavailable: "
                                f"{fallback_error}")
                    return
            else:
                from app.agent.devlog import devlog
                devlog.warn(f"OpenWakeWord mic unavailable: {first_error}")
                return
        with stream:
            while not self._stop_flag.is_set():
                audio, _ = stream.read(chunk)
                if audio_gate.speaking.is_set():
                    continue
                audio_16k = normalize_audio_for_stt(audio, rate, 16000)
                scores = model.predict(np.squeeze(audio_16k))
                if any(s >= self.SCORE_THRESHOLD for s in scores.values()):
                    now = time.time()
                    if now - last_fire >= self.COOLDOWN_SECONDS:
                        last_fire = now
                        model.reset()
                        self.on_wake()

    def stop(self) -> None:
        self._stop_flag.set()


# Back-compat alias — the controller historically imported WakeWordListener.
WakeWordListener = WhisperWakeWord
