"""Speech-to-text: faster-whisper (default, CPU int8) or whisper.cpp CLI."""

import re
import subprocess
from pathlib import Path


class STTError(Exception):
    pass


_fw_model = None
_fw_model_name = None


def transcribe_wav(wav_path: str, config) -> str:
    if config.stt_backend == "whisper_cpp":
        return _whisper_cpp(wav_path, config)
    return _faster_whisper(wav_path, config)


def _faster_whisper(wav_path: str, config) -> str:
    global _fw_model, _fw_model_name
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise STTError(
            "faster-whisper is not installed. Run: pip install faster-whisper "
            "(or configure whisper.cpp in Settings).") from e

    if _fw_model is None or _fw_model_name != config.faster_whisper_model:
        # First call downloads the model (~150 MB for 'base') then caches it.
        _fw_model = WhisperModel(config.faster_whisper_model,
                                 device="cpu", compute_type="int8")
        _fw_model_name = config.faster_whisper_model

    segments, _info = _fw_model.transcribe(wav_path, vad_filter=True, beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


def _whisper_cpp(wav_path: str, config) -> str:
    exe = Path(config.whisper_cpp_exe or "")
    model = Path(config.whisper_cpp_model or "")
    if not exe.exists():
        raise STTError(f"whisper.cpp executable not found: {exe}")
    if not model.exists():
        raise STTError(f"whisper.cpp model not found: {model}")

    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [str(exe), "-m", str(model), "-f", wav_path, "-nt", "-np"],
            capture_output=True, text=True, timeout=120, creationflags=creation)
    except subprocess.TimeoutExpired as e:
        raise STTError("whisper.cpp timed out.") from e
    if proc.returncode != 0:
        raise STTError(f"whisper.cpp failed: {(proc.stderr or '')[:200]}")

    lines = []
    for line in (proc.stdout or "").splitlines():
        line = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()  # strip timestamps
        if line:
            lines.append(line)
    return " ".join(lines).strip()


def backend_ready(config) -> tuple[bool, str]:
    """Startup check: (ok, human-readable status)."""
    if config.stt_backend == "whisper_cpp":
        if Path(config.whisper_cpp_exe or "").exists() and Path(config.whisper_cpp_model or "").exists():
            return True, "whisper.cpp is configured."
        return False, "whisper.cpp exe/model path is missing (check Settings)."
    try:
        import faster_whisper  # noqa: F401
        return True, f"faster-whisper ready (model: {config.faster_whisper_model})."
    except ImportError:
        return False, "faster-whisper is not installed — voice input won't work yet."
