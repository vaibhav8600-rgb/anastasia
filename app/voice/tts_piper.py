"""Piper TTS via local CLI. Generates a temp WAV and plays it with winsound."""

import subprocess
import tempfile
import winsound
from pathlib import Path


def piper_available(config) -> bool:
    return bool(config.piper_exe) and Path(config.piper_exe).exists() \
        and bool(config.piper_voice) and Path(config.piper_voice).exists()


def speak_piper(text: str, config) -> None:
    if not piper_available(config):
        raise FileNotFoundError("Piper exe or voice model not found (check Settings).")

    wav_path = Path(tempfile.gettempdir()) / "anna_tts.wav"
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [str(config.piper_exe), "--model", str(config.piper_voice),
         "--output_file", str(wav_path)],
        input=text.encode("utf-8"), capture_output=True,
        timeout=60, creationflags=creation)
    if proc.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"Piper failed: {(proc.stderr or b'')[:200]!r}")
    try:
        winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
    finally:
        wav_path.unlink(missing_ok=True)
