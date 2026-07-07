"""Speech-to-text: faster-whisper (default, CPU int8) or whisper.cpp CLI."""

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class STTError(Exception):
    pass


_fw_model = None
_fw_model_name = None


@dataclass(frozen=True)
class SpeechConfidence:
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    compression_ratio: Optional[float] = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    confidence: SpeechConfidence = SpeechConfidence()
    stt_ms: float = 0.0


def build_stt_prompt(config, max_chars: int = 200) -> str:
    """Compact vocabulary primer built from live app/folder configuration."""
    from app.config import _default_app_aliases

    max_chars = max(1, int(max_chars))
    aliases = list(config.app_aliases)
    defaults = set(_default_app_aliases())
    aliases.sort(key=lambda name: (name in defaults,))  # custom aliases first
    unique_aliases = []
    seen_targets = set()
    for alias in aliases:
        target = str(config.app_aliases[alias]).lower()
        if target not in seen_targets:
            unique_aliases.append(alias)
            seen_targets.add(target)
    aliases = unique_aliases
    folders = [Path(folder).name for folder in config.safe_folders]
    words = [
        "open", "close", "launch", "minimize", "maximize", "screenshot",
        "copy", "paste", "search", "Google", "YouTube",
        *folders, *aliases,
    ]
    prompt = f"{config.assistant_nickname} Windows voice commands:"
    if len(prompt) + 1 > max_chars:
        return prompt[:max_chars - 1].rstrip(" ,:.") + "."
    for word in words:
        candidate = f"{prompt} {word},"
        if len(candidate.rstrip(",") + ".") > max_chars:
            continue
        prompt = candidate
    return prompt.rstrip(",") + "."


def transcribe_wav(wav_path: str, config) -> TranscriptionResult:
    if config.stt_backend == "whisper_cpp":
        return _whisper_cpp(wav_path, config)
    return _faster_whisper(wav_path, config)


def _faster_whisper(wav_path: str, config) -> TranscriptionResult:
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

    language = getattr(config, "stt_language", "en") or "en"
    started = time.perf_counter()
    segments, _info = _fw_model.transcribe(
        wav_path,
        language=None if language == "auto" else language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        initial_prompt=build_stt_prompt(config),
        condition_on_previous_text=False,
    )
    segments = list(segments)
    stt_ms = (time.perf_counter() - started) * 1000
    text = " ".join(seg.text.strip() for seg in segments).strip()

    def average(name):
        values = [float(value) for seg in segments
                  if (value := getattr(seg, name, None)) is not None]
        return sum(values) / len(values) if values else None

    ratios = [float(value) for seg in segments
              if (value := getattr(seg, "compression_ratio", None)) is not None]
    confidence = SpeechConfidence(
        avg_logprob=average("avg_logprob"),
        no_speech_prob=average("no_speech_prob"),
        compression_ratio=max(ratios) if ratios else None,
    )
    return TranscriptionResult(text=text, confidence=confidence, stt_ms=stt_ms)


def _whisper_cpp(wav_path: str, config) -> TranscriptionResult:
    exe = Path(config.whisper_cpp_exe or "")
    model = Path(config.whisper_cpp_model or "")
    if not exe.exists():
        raise STTError(f"whisper.cpp executable not found: {exe}")
    if not model.exists():
        raise STTError(f"whisper.cpp model not found: {model}")

    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = time.perf_counter()
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
    return TranscriptionResult(text=" ".join(lines).strip(),
                               stt_ms=(time.perf_counter() - started) * 1000)


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
