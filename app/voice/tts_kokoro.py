"""Optional Kokoro ONNX backend. Missing dependencies are always non-fatal."""

import importlib.util
import os
import tempfile
import threading
import wave
from pathlib import Path

KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

_engine = None
_engine_key = None


def kokoro_setup_status(config) -> tuple[bool, str]:
    try:
        package_found = importlib.util.find_spec("kokoro_onnx") is not None
    except (ImportError, ValueError):
        package_found = False
    if not package_found:
        return False, "Install the optional package: pip install kokoro-onnx"
    model = Path(config.kokoro_model or "")
    voices = Path(config.kokoro_voices or "")
    if not config.kokoro_model or not model.is_file():
        return False, "Select kokoro-v1.0.onnx in Voice settings."
    if not config.kokoro_voices or not voices.is_file():
        return False, "Select voices-v1.0.bin in Voice settings."
    return True, "Kokoro package and model files are ready."


def kokoro_available(config) -> bool:
    return kokoro_setup_status(config)[0]


def _temp_wav(prefix: str) -> Path:
    name = f"{prefix}_{os.getpid()}_{threading.get_ident()}.wav"
    return Path(tempfile.gettempdir()) / name


def synthesize_kokoro(text: str, config, wav_path: Path) -> None:
    ok, message = kokoro_setup_status(config)
    if not ok:
        raise RuntimeError(message)
    global _engine, _engine_key
    key = (str(config.kokoro_model), str(config.kokoro_voices))
    if _engine is None or _engine_key != key:
        from kokoro_onnx import Kokoro
        _engine = Kokoro(*key)
        _engine_key = key
    samples, sample_rate = _engine.create(
        text, voice=config.kokoro_voice or "af_heart",
        speed=max(0.5, min(2.0, float(config.tts_rate or 1.0))),
        lang="en-us")

    import numpy as np
    audio = np.asarray(samples)
    if np.issubdtype(audio.dtype, np.floating):
        audio = np.clip(audio, -1.0, 1.0) * 32767
    audio = np.asarray(audio, dtype=np.int16).reshape(-1)
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(int(sample_rate))
        output.writeframes(audio.tobytes())


def validate_kokoro_config(config, play: bool = False) -> tuple[bool, str]:
    wav_path = _temp_wav("anna_kokoro_test")
    try:
        synthesize_kokoro("Hi, it's Anna. Kokoro is ready.", config, wav_path)
        if play:
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
        return True, f"Kokoro validated with voice {config.kokoro_voice}."
    except Exception as exc:
        return False, f"Kokoro setup incomplete: {exc}"
    finally:
        wav_path.unlink(missing_ok=True)
