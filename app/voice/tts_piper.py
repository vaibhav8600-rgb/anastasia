"""Piper TTS setup checks, synthesis, and validation."""

import os
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path


def piper_json_path(voice_path) -> Path:
    if not voice_path:
        return Path("__missing_voice__.onnx.json")
    voice = Path(voice_path or "")
    return voice.with_suffix(voice.suffix + ".json")


PIP_PIPER_MESSAGE = (
    "This looks like a pip-installed 'piper' package, which is not Piper TTS. "
    "Download piper_windows_amd64.zip from github.com/rhasspy/piper/releases "
    "and point this at the extracted piper.exe.")


def looks_like_pip_piper(path) -> bool:
    """True for Python entry-point stubs (venv/Scripts/piper.exe) — those
    crash with 'sys.exit(main())' tracebacks; only the standalone C++ binary
    from github.com/rhasspy/piper works."""
    parts = [p.lower().rstrip("\\/") for p in Path(str(path or "")).parts]
    if not parts:
        return False
    dirs = parts[:-1]
    return ".venv" in dirs or "venv" in dirs or (dirs and dirs[-1] == "scripts")


def _resolve_piper_exe(config) -> Path | None:
    """Only the configured path or a non-venv PATH entry. Never fall back to
    a venv Scripts stub — that's the pip package, not Piper TTS."""
    configured = (config.piper_exe or "").strip()
    if configured:
        exe = Path(configured)
        if exe.is_file() and not looks_like_pip_piper(exe):
            return exe
        return None
    which = shutil.which("piper")
    if which and not looks_like_pip_piper(which):
        return Path(which)
    return None


def piper_setup_status(config) -> tuple[bool, str]:
    configured = (config.piper_exe or "").strip()
    if configured and looks_like_pip_piper(configured):
        return False, PIP_PIPER_MESSAGE
    exe = _resolve_piper_exe(config)
    if exe is None:
        return False, ("Piper executable not found. Download the standalone "
                       "binary (piper_windows_amd64.zip) from "
                       "github.com/rhasspy/piper/releases — do NOT pip "
                       "install piper — and select piper.exe in Voice settings.")
    voice = Path(config.piper_voice or "")
    if not config.piper_voice or not voice.is_file():
        return False, "Piper executable found, but the .onnx voice file is still missing. Select a voice in Voice settings."
    metadata = piper_json_path(config.piper_voice)
    if not metadata.is_file():
        return False, f"Missing matching voice metadata: {metadata.name}"
    return True, "Piper paths and voice metadata are present."


def piper_available(config) -> bool:
    return piper_setup_status(config)[0]


def synthesize_piper(text: str, config, wav_path: Path,
                     timeout: float = 60) -> None:
    ok, message = piper_setup_status(config)
    if not ok:
        raise FileNotFoundError(message)
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    length_scale = max(0.5, min(2.0,
        float(getattr(config, "piper_length_scale", 1.08) or 1.08)))
    proc = subprocess.run(
        [str(config.piper_exe), "--model", str(config.piper_voice),
         "--output_file", str(wav_path),
         "--length_scale", str(round(length_scale, 2))],
        input=text.encode("utf-8"), capture_output=True,
        timeout=timeout, creationflags=creation)
    if proc.returncode != 0 or not wav_path.is_file():
        error = (proc.stderr or proc.stdout or b"Piper produced no audio")
        raise RuntimeError(error.decode("utf-8", errors="replace")[:300])


def _temp_wav(prefix: str) -> Path:
    name = f"{prefix}_{os.getpid()}_{threading.get_ident()}.wav"
    return Path(tempfile.gettempdir()) / name


def validate_piper_config(config, play: bool = False) -> tuple[bool, str]:
    """Run the executable and load the selected voice with a real phrase."""
    wav_path = _temp_wav("anna_piper_test")
    try:
        synthesize_piper("Hi, it's Anna. Piper is ready.", config, wav_path,
                         timeout=10)
        with wave.open(str(wav_path), "rb") as audio:
            if audio.getnframes() <= 0 or audio.getframerate() <= 0:
                raise RuntimeError("Piper created an empty WAV file.")
        if play:
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
        return True, "Piper validated — executable and voice both work."
    except Exception as exc:
        return False, f"Piper validation failed: {exc}"
    finally:
        wav_path.unlink(missing_ok=True)


def speak_piper(text: str, config) -> None:
    wav_path = _temp_wav("anna_tts")
    try:
        synthesize_piper(text, config, wav_path)
        import winsound
        winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
    finally:
        wav_path.unlink(missing_ok=True)
