"""Configuration loading/saving for Anastasia (Anna)."""

import json
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
MEMORY_PATH = DATA_DIR / "memory.json"
HISTORY_DB_PATH = DATA_DIR / "history.sqlite"


def _default_safe_folders() -> List[str]:
    home = Path.home()
    return [str(home / name).replace("\\", "/")
            for name in ("Desktop", "Downloads", "Documents", "Pictures", "Projects")]


def _default_app_aliases() -> Dict[str, str]:
    # Values are either an absolute exe path, a command resolvable via
    # `start` (Windows App Paths / PATH), or a URI protocol like "msteams:".
    # Absolute paths that don't exist fall back to a `start <stem>` launch.
    return {
        "notepad": "notepad.exe",
        "paint": "mspaint.exe",
        "mspaint": "mspaint.exe",
        "chrome": "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "edge": "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "vscode": "code",
        "vs code": "code",
        "file explorer": "explorer.exe",
        "explorer": "explorer.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "terminal": "wt.exe",
        "powershell": "powershell.exe",
        "teams": "msteams:",
    }


# Old default values -> new defaults. Applied only when the stored value still
# equals the old default, so user customizations always win.
_DEFAULT_MIGRATIONS = {
    "ollama_model": ("qwen3:4b", "llama3.2:3b"),
    "ollama_timeout": (120, 20),
    "silence_seconds": (1.6, 1.2),
    "max_record_seconds": (30, 8),
    "stt_language": ("auto", "en"),
}


class GarbleConfig(BaseModel):
    no_speech_prob: float = 0.6
    avg_logprob: float = -1.0
    compression_ratio: float = 2.4


class STTConfig(BaseModel):
    garble: GarbleConfig = Field(default_factory=GarbleConfig)


class AppConfig(BaseModel):
    """All user-tunable settings. Persisted to app/data/config.json."""

    model_config = {"extra": "ignore"}

    # Identity
    assistant_name: str = "Anastasia"
    assistant_nickname: str = "Anna"

    # LLM (Ollama)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    chat_model: str = ""                  # empty = use ollama_model

    # Cloud brain (Groq, optional). Key also via env GROQ_API_KEY (env wins);
    # never logged, never sent to the frontend (masked in Settings).
    brain_mode: str = "hybrid"            # "hybrid" | "local_only"
    groq_api_key: str = ""
    cloud_model: str = "llama-3.3-70b-versatile"
    cloud_timeout_s: float = 8.0          # Groq answers ~1s; >8s = fail over
    # Privacy (8C): clipboard text may only reach the cloud with this opt-in.
    # Files, screenshots and raw audio NEVER leave the machine regardless.
    allow_clipboard_to_cloud: bool = False
    ollama_timeout: int = 20
    ollama_keep_alive: str = "30m"   # keeps the model loaded between requests
    ollama_num_predict: int = 220    # hard cap on generated tokens
    ollama_num_ctx: int = 2048
    # 0 = CPU-only (default: partial iGPU offload corrupts some models,
    # e.g. llama3.2 emitting "@@@@" on Intel UHD graphics). -1 = Ollama auto.
    ollama_num_gpu: int = 0

    # Voice
    voice_enabled: bool = True
    wake_word_enabled: bool = False
    push_to_talk_hotkey: str = "ctrl+alt+space"

    # STT
    stt: STTConfig = Field(default_factory=STTConfig)
    stt_backend: str = "faster_whisper"  # "faster_whisper" | "whisper_cpp"
    # Streaming STT (Deepgram, optional). Key also via env DEEPGRAM_API_KEY
    # (env wins); never logged, never sent to the frontend (masked in Settings).
    # "streaming" sends live mic audio to Deepgram while the mic is open;
    # "local" keeps all audio on-device (default until a key is added).
    stt_mode: str = "local"              # "streaming" | "local"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"
    faster_whisper_model: str = "base"   # tiny | base | small ...
    whisper_cpp_exe: str = ""            # path to whisper.cpp main/whisper-cli exe
    whisper_cpp_model: str = ""          # path to ggml model file
    sample_rate: int = 16000
    microphone_device: str = ""         # empty = use the system default mic
    silence_auto_stop: bool = True
    silence_threshold: float = 0.012     # RMS on 0..1 scale
    silence_seconds: float = 1.2
    max_record_seconds: int = 8

    # TTS
    tts_backend: str = "auto"            # auto | piper | kokoro | windows | off
    piper_exe: str = ""                  # optional path to piper.exe fallback
    piper_voice: str = ""                # path to .onnx voice model
    piper_length_scale: float = 1.08      # >1 is a slightly more relaxed pace
    kokoro_model: str = ""               # path to kokoro-v1.0.onnx
    kokoro_voices: str = ""              # path to voices-v1.0.bin
    kokoro_voice: str = "af_heart"
    tts_rate: float = 1.0                # speed multiplier 0.5..2.0
    tts_volume: int = 100                # 0..100 (SAPI only; Piper ignores)

    # STT language: "auto" or a code like en / hi / mr
    stt_language: str = "en"

    # Safety / behavior
    confirmation_mode: str = "strict"    # "strict" | "normal"
    max_type_text_no_confirm: int = 500

    # UI
    animation_quality: str = "medium"    # "low" | "medium" | "high"

    # Conversation (8D): after a spoken chat reply, briefly reopen the mic for
    # a follow-up without pressing PTT. Off by default; half-duplex still applies.
    hands_free_followup: bool = False
    hands_free_window_s: float = 6.0

    # Tools
    default_browser: str = ""            # empty = system default; or alias key e.g. "chrome"
    screenshot_dir: str = str(Path.home() / "Pictures" / "AnnaScreenshots").replace("\\", "/")
    safe_folders: List[str] = Field(default_factory=_default_safe_folders)
    app_aliases: Dict[str, str] = Field(default_factory=_default_app_aliases)

    # ---------------------------------------------------------------
    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        """Load config.json, creating it with defaults on first run.
        Older configs are migrated in place: outdated default values are
        replaced and newly added alias keys merged, without touching
        anything the user customized."""
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for key, (old, new) in _DEFAULT_MIGRATIONS.items():
                    if data.get(key) == old:
                        data[key] = new
                aliases = data.get("app_aliases")
                if isinstance(aliases, dict):
                    for key, value in _default_app_aliases().items():
                        aliases.setdefault(key, value)
                cfg = cls(**data)
            except Exception:
                cfg = cls()  # corrupt config -> fall back to defaults, don't crash
        else:
            cfg = cls()
        cfg.save(path)
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(), indent=2), encoding="utf-8")
