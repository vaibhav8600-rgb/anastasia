"""Voice package. speak() dispatches Piper -> Windows SAPI fallback."""

import re
import unicodedata


def _clean_for_speech(text: str, limit: int = 2000) -> str:
    """Turn display text into words a speech engine should actually read."""
    def replace_url(match):
        url = match.group(0)
        suffix = ""
        while url and url[-1] in ".,!?":
            suffix = url[-1] + suffix
            url = url[:-1]
        return "link" + suffix

    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", replace_url, text)
    text = re.sub(r"[*_#`>~]", "", text)
    text = "".join(
        ch for ch in text
        if not (unicodedata.category(ch) == "So"
                or 0x1F1E6 <= ord(ch) <= 0x1FAFF
                or 0xFE00 <= ord(ch) <= 0xFE0F)
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def split_sentences(text: str) -> list[str]:
    """Split sanitized speech into ordered, synthesis-sized queue items."""
    cleaned = _clean_for_speech(text)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\s*[\r\n]+\s*", cleaned)
    return [part.strip() for part in parts if part.strip()]


def speak(text: str, config) -> None:
    """Speak text aloud. Never raises — TTS failure must not break the app."""
    if not text or not config.voice_enabled or config.tts_backend == "off":
        return
    text = _clean_for_speech(text)
    if not text:
        return

    backend = config.tts_backend
    if backend in ("auto", "piper"):
        from app.voice.tts_piper import piper_available
        if piper_available(config):
            try:
                from app.voice.tts_piper import speak_piper
                speak_piper(text, config)
                return
            except Exception as e:
                print(f"[tts] Piper failed ({e}); falling back to Windows voice.")
        elif backend == "piper":
            print("[tts] Piper selected but setup is incomplete.")
            return
    if backend == "kokoro":
        try:
            from app.voice.tts_kokoro import kokoro_available, synthesize_kokoro, _temp_wav
            if not kokoro_available(config):
                print("[tts] Kokoro selected but setup is incomplete.")
                return
            wav_path = _temp_wav("anna_kokoro")
            synthesize_kokoro(text, config, wav_path)
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
            wav_path.unlink(missing_ok=True)
            return
        except Exception as e:
            print(f"[tts] Kokoro failed: {e}")
            return
    try:
        from app.voice.tts_windows import speak_windows
        speak_windows(text)
    except Exception as e:
        print(f"[tts] Windows TTS failed: {e}")
