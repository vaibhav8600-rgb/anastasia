"""Deepgram Aura TTS — cloud streaming voice (9.1C).

Synthesizes a sentence to 16kHz linear16 PCM via the Aura REST stream and
writes a WAV the existing cancellable playback path plays (so barge-in and
the orb envelope work unchanged). Reuses the Deepgram key from 9.1B.

Privacy: this sends Anna's REPLY TEXT to Deepgram. Piper/SAPI keep synthesis
fully local and remain the privacy default.
"""

import wave
from pathlib import Path

import requests

from app.voice.stt_providers import deepgram_key

AURA_URL = "https://api.deepgram.com/v1/speak"
AURA_MODELS = ("aura-2-luna-en", "aura-asteria-en", "aura-luna-en")
_SAMPLE_RATE = 16000


def deepgram_tts_available(config) -> bool:
    return bool(deepgram_key(config))


def deepgram_tts_status(config) -> tuple[bool, str]:
    if not deepgram_key(config):
        return False, "Add a Deepgram API key in Settings → Voice input."
    return True, f"Deepgram Aura ({config.tts_deepgram_model}) ready."


def synthesize_deepgram(text: str, config, wav_path: Path,
                        timeout: float = 15.0) -> None:
    """POST text to Aura, receive linear16 PCM, wrap it in a WAV. Raises on
    any failure so the caller can fall back to Piper."""
    key = deepgram_key(config)
    if not key:
        raise RuntimeError("Deepgram key not configured.")
    model = getattr(config, "tts_deepgram_model", "aura-2-luna-en")
    params = {"model": model, "encoding": "linear16", "sample_rate": _SAMPLE_RATE}
    r = requests.post(
        AURA_URL, params=params, json={"text": text},
        headers={"Authorization": f"Token {key}"},
        timeout=(3.05, timeout), stream=True)
    if r.status_code in (401, 403):
        raise RuntimeError("Deepgram key rejected (auth).")
    if r.status_code >= 400:
        raise RuntimeError(f"Deepgram Aura HTTP {r.status_code}: {r.text[:120]}")
    pcm = bytearray()
    for chunk in r.iter_content(chunk_size=8192):
        if chunk:
            pcm.extend(chunk)
    if not pcm:
        raise RuntimeError("Deepgram Aura returned no audio.")
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(bytes(pcm))


def validate_deepgram_tts(config, play: bool = False) -> tuple[bool, str]:
    import os
    import tempfile
    import threading
    wav_path = Path(tempfile.gettempdir()) / \
        f"anna_aura_test_{os.getpid()}_{threading.get_ident()}.wav"
    try:
        synthesize_deepgram("Hi, it's Anna. Deepgram voice is ready.",
                            config, wav_path, timeout=15.0)
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getnframes() <= 0:
                raise RuntimeError("empty audio")
        if play:
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
        return True, "Deepgram Aura validated — cloud voice works."
    except Exception as exc:
        return False, f"Deepgram Aura failed: {' '.join(str(exc).split())[:200]}"
    finally:
        wav_path.unlink(missing_ok=True)
