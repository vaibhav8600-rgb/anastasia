"""`python app/main.py --doctor` — offline health check (spec sec 19)."""

import sys
from pathlib import Path


def _line(ok, text, warn=False):
    mark = "✅" if ok else ("⚠" if warn else "❌")
    print(f"{mark} {text}")
    return ok or warn


def run_doctor() -> int:
    from app.config import AppConfig
    try:  # Windows consoles default to cp1252, which can't print ✅
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("Anna System Check")
    ok = True

    v = sys.version_info
    ok &= _line(v >= (3, 10), f"Python {v.major}.{v.minor}")

    try:
        config = AppConfig.load()
        _line(True, "Config valid")
    except Exception as e:
        _line(False, f"Config invalid: {e}")
        return 1

    missing = [f for f in config.safe_folders if not Path(f).exists()]
    ok &= _line(not missing, "Safe folders exist" if not missing
                else f"Missing safe folders: {', '.join(missing)}", warn=True)

    from app.tools.open_app import resolve_app
    bad = [a for a in ("notepad", "paint", "calculator") if not resolve_app(a, config)]
    ok &= _line(not bad, "App aliases resolve" if not bad
                else f"Unresolvable aliases: {bad}")

    from app.voice.recorder import microphone_available
    ok &= _line(microphone_available(), "Mic available", warn=True)

    from app.voice.stt_whisper import backend_ready
    stt_ok, msg = backend_ready(config)
    ok &= _line(stt_ok, msg, warn=True)

    from app.llm.ollama_client import OllamaClient
    llm = OllamaClient(config)
    if llm.is_available():
        _line(True, "Ollama running")
        models = llm.list_models()
        ok &= _line(any(config.ollama_model in m for m in models),
                    f"Model {config.ollama_model} available" if models
                    else f"Model {config.ollama_model} NOT installed — "
                         f"run: ollama pull {config.ollama_model}")
    else:
        ok &= _line(False, "Ollama not running — simple commands still work, "
                           "chat/reasoning needs it", warn=True)

    from app.voice.tts_piper import piper_available
    if piper_available(config):
        _line(True, "Piper voice configured")
    else:
        _line(False, "Piper not configured, using Windows fallback", warn=True)

    if config.wake_word_enabled:
        try:
            import openwakeword  # noqa: F401
            _line(True, "Wake word package installed")
        except ImportError:
            _line(False, "Wake word enabled but openwakeword missing — "
                         "pip install openwakeword", warn=True)
    else:
        _line(True, "Wake word disabled")

    return 0 if ok else 1
