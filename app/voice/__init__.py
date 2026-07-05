"""Voice package. speak() dispatches Piper -> Windows SAPI fallback."""

import re


def _clean_for_speech(text: str, limit: int = 400) -> str:
    text = re.sub(r"```.*?```", " code block ", text, flags=re.DOTALL)
    text = re.sub(r"[*_#`>\[\]]", "", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def speak(text: str, config) -> None:
    """Speak text aloud. Never raises — TTS failure must not break the app."""
    if not text or not config.voice_enabled or config.tts_backend == "off":
        return
    text = _clean_for_speech(text)
    if not text:
        return

    backend = config.tts_backend
    if backend in ("auto", "piper") and config.piper_exe:
        try:
            from app.voice.tts_piper import speak_piper
            speak_piper(text, config)
            return
        except Exception as e:
            print(f"[tts] Piper failed ({e}); falling back to Windows voice.")
    if backend == "piper":
        # explicitly piper-only but it failed/is unconfigured — stay silent
        if not config.piper_exe:
            print("[tts] Piper selected but piper_exe is not configured.")
        return
    try:
        from app.voice.tts_windows import speak_windows
        speak_windows(text)
    except Exception as e:
        print(f"[tts] Windows TTS failed: {e}")
