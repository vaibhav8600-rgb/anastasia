"""Configuration loading/saving for Anastasia (Anna)."""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Anastasia"
    return APP_DIR / "data"


DATA_DIR = _data_dir()
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
    "gemini_live_voice": ("Kore", "Sulafat"),   # 10D: the warm HD voice
    # 11B: a long-edge cap of 1280 squashed dual-monitor grabs to ~360px tall.
    "vision_max_edge": (1280, 2600),
    # gemini-2.5-flash now 404s for newly-issued keys (verified 2026-07).
    "vision_cloud_model": ("gemini-2.5-flash", "gemini-3-flash-preview"),
    # Groq deprecated llama-3.3-70b-versatile on 2026-06-17 (free/dev tier).
    # Migrate the default hybrid brain to its recommended successor; users who
    # picked their own cloud model keep it. See DECISIONS D-1.0.
    "cloud_model": ("llama-3.3-70b-versatile", "openai/gpt-oss-120b"),
}


class GarbleConfig(BaseModel):
    no_speech_prob: float = 0.6
    avg_logprob: float = -1.0
    compression_ratio: float = 2.4


class STTConfig(BaseModel):
    garble: GarbleConfig = Field(default_factory=GarbleConfig)


class AppConfig(BaseModel):
    """All user-tunable settings. Persisted to DATA_DIR/config.json."""

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
    # llama-3.3-70b-versatile was deprecated by Groq 2026-06-17; gpt-oss-120b is
    # its recommended successor. Config-driven — override in Settings. (D-1.0)
    cloud_model: str = "openai/gpt-oss-120b"
    cloud_timeout_s: float = 8.0          # Groq answers ~1s; >8s = fail over
    # Privacy (8C): clipboard text may only reach the cloud with this opt-in.
    # Files, screenshots and raw audio NEVER leave the machine regardless.
    allow_clipboard_to_cloud: bool = False

    # Gemini Live (Phase 10, premium engine). Key also via env GEMINI_API_KEY
    # or GOOGLE_API_KEY (env wins); never logged, masked in Settings.
    # Model ID verified 2026-07 against ai.google.dev/gemini-api/docs/live-api:
    # the Gemini API native-audio Live model is gemini-3.1-flash-live-preview
    # (gemini-live-2.5-flash-native-audio is the Vertex AI variant). Preview
    # tier — expect churn; editable in Settings.
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-3.1-flash-live-preview"
    # HD voice, verified 2026-07 against the TTS voice list (native-audio
    # Live models take the full set): Sulafat is the documented "Warm" one.
    gemini_live_voice: str = "Sulafat"
    live_affective_dialog: bool = True   # applied only on models that support it
    live_stall_timeout_s: float = 20.0   # mid-turn silence -> teardown + fallback
    live_half_duplex: bool = True        # drop mic frames while Anna's audio plays
    # Cost transparency (10D): estimates only, editable because pricing moves.
    live_price_in_per_min: float = 0.005
    live_price_out_per_min: float = 0.018
    live_idle_close_s: float = 60.0      # idle Live session auto-closes (0 = never)
    live_monthly_cap_usd: float = 0.0    # soft cap: warn past this, never block (0 = off)
    # Conversation engine (10C). ALWAYS defaults to pipeline: Live streams
    # continuous mic audio to Google and bills per minute, so it additionally
    # requires the explicit live_audio_consent opt-in — never auto-on.
    engine_mode: str = "pipeline"        # "gemini_live" | "pipeline" | "local"
    engine_rules_first: bool = True      # simple commands stay local + instant
    live_audio_consent: bool = False     # billing + continuous-audio opt-in
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
    # "whisper" = listen for Anna's actual name via local STT (no training,
    # no extra deps). "openwakeword" = the pre-trained "Hey Jarvis" model.
    wake_word_backend: str = "whisper"
    # "base" recognizes "Hey Anna"/"Anastasia" reliably; "tiny" is faster but
    # mishears short names; "small" is most accurate but slowest.
    wake_word_model: str = "base"
    wake_word_phrases: List[str] = Field(
        default_factory=lambda: ["hey anna", "anna", "anastasia", "hey anastasia"])
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
    tts_backend: str = "auto"            # auto | piper | kokoro | deepgram | windows | off
    # Deepgram Aura (cloud streaming voice). Reuses deepgram_api_key. Sends
    # Anna's REPLY TEXT to Deepgram to synthesize; Piper/SAPI stay fully local.
    tts_deepgram_model: str = "aura-2-delia-en"
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
    # Voice approval (11A). A pending confirmation auto-cancels after this
    # many seconds. Destructive-tier actions additionally demand the strong
    # phrase ("Anna approve"); a casual "yes" is refused.
    confirmation_timeout_s: float = 30.0
    # Reopen the mic automatically while a confirmation card is up, so
    # "approve"/"cancel" can be spoken without pressing push-to-talk. Only
    # applies in continuous hands-free mode. OFF by default: it opens the
    # microphone on its own, and 9C deliberately never grabs the mic while
    # waiting for approval. Push-to-talk and the voice-confirm button always
    # accept spoken approval regardless of this setting.
    confirmation_voice_listen: bool = False

    # Phase 0 / D-0.5: with NO window attached (headless anna-core), let the
    # user ANSWER a confirmation by voice — spoken question first, an audible
    # cue when the mic opens, one short listen window. The strong phrase is
    # unchanged, and this only ever triggers for a card the validator demanded
    # and only when nobody can click. Default ON (it is the ONLY answer channel
    # windowless); a kill switch for users who want windowless to stay mute.
    headless_voice_confirm: bool = True
    headless_confirm_listen_s: float = 6.0

    # Phase 0 / D-0.3: start anna-core at logon via a Task Scheduler ONLOGON
    # task. OPT-IN — mirrors the actual scheduled task, only ever changed by an
    # explicit user action (installer checkbox or the Settings toggle). Nothing
    # on boot creates the task; editing machine startup needs consent.
    autostart_enabled: bool = False

    # Phase 1: the proactive loop (watchers → salience → feed). OFF by default —
    # proactivity is opt-in per the consent doctrine. Every watcher is gated by
    # its own watch_<name>_enabled, but nothing runs unless this master is on.
    proactive_enabled: bool = False
    watch_system_enabled: bool = True
    watch_system_interval_s: float = 30.0
    watch_disk_min_pct: float = 10.0        # alert below; re-arms at +2 (hysteresis)
    watch_ram_max_pct: float = 90.0         # alert above; re-arms at -3
    watch_battery_min_pct: float = 20.0     # alert below (on battery); re-arms +5
    # Filesystem watcher (watchdog). Defaults to Downloads; add project dirs.
    watch_filesystem_enabled: bool = True
    watch_paths: list = Field(default_factory=lambda: [str(Path.home() / "Downloads")])
    watch_fs_exclude: list = Field(default_factory=lambda: [
        ".git", "node_modules", "__pycache__", ".venv", "venv", "AppData",
        "$RECYCLE.BIN", ".cache", ".idea", ".vs"])
    # Active-window watcher. PRIVACY: window TITLES are opt-in (they carry doc
    # names / email subjects); process names are always fine. Default OFF.
    watch_window_enabled: bool = True
    watch_window_interval_s: float = 5.0
    watch_window_titles: bool = False
    watch_focus_minutes: float = 20.0       # emit focus_session after this long in one app

    # UI
    animation_quality: str = "medium"    # "low" | "medium" | "high"

    # Conversation (8D): after a spoken chat reply, briefly reopen the mic for
    # a follow-up without pressing PTT. Off by default; half-duplex still applies.
    hands_free_followup: bool = False
    hands_free_window_s: float = 6.0
    # Continuous hands-free (9C): a true back-and-forth loop — the mic reopens
    # automatically after Anna finishes, until a stop phrase / mic tap / idle
    # timeout. Persists across restarts. Half-duplex + barge-in still apply.
    hands_free: bool = False
    hands_free_idle_timeout_s: float = 45.0

    # Vision (11B). Capture is OFF until explicitly triggered — Anna never
    # watches silently. Mode A (on-demand single frame) runs only on a trigger
    # phrase; Mode B (low-frequency watching) must be started explicitly and
    # auto-stops when idle. Frames are processed once and discarded.
    screen_watch_interval_s: float = 1.5     # Mode B: one frame per N seconds
    screen_watch_idle_timeout_s: float = 120.0
    # Downscale budget before OCR/transport. An AREA budget, not a long-edge
    # one: a dual 2560x1440 desktop is 5120px wide, and capping the long edge
    # squashes it to ~360px tall — unreadable. max_edge is only a backstop.
    vision_max_pixels: int = 1_600_000
    vision_max_edge: int = 2600
    vision_save_captures: bool = False       # raw frames are never kept unless on
    ocr_backend: str = "auto"                # auto | tesseract | off
    tesseract_exe: str = ""                  # path to tesseract.exe if not on PATH
    # OCR of a dense desktop is intrinsically slow (~15-20s at full res), so
    # Anna downscales HARD before running and caps the time. The fast pass
    # drives the sensitive-content scan; the full pass is used only in
    # local-only mode (cloud vision describes a screen faster and better).
    ocr_fast_pixels: int = 500_000
    ocr_max_pixels: int = 1_100_000
    ocr_timeout_s: int = 8
    camera_device: str = ""              # browser deviceId; empty = default cam
    camera_preview: bool = True          # show a live self-view while capturing
    # In a Live conversation, send the camera frame straight into the session
    # so Anna sees it natively (faster, no separate call) — still one frame.
    live_native_camera: bool = True
    # Cloud vision (separate, explicit consent — screen/camera frames may be
    # sent to Gemini). Off by default; local OCR needs no consent.
    cloud_vision_consent: bool = False
    # Verified live 2026-07: gemini-2.5-flash now 404s for new keys and
    # gemini-3.5-flash/gemini-flash-latest were 503. This one answered
    # correctly. Preview tier — expect churn; editable in Settings.
    vision_cloud_model: str = "gemini-3-flash-preview"

    # App control (11C). Structured backends first; vision coordinates are a
    # last resort and are always confirmation-gated.
    # Attach to YOUR running browser so Anna acts on the real logged-in tab:
    #   chrome.exe --remote-debugging-port=9222
    browser_cdp_url: str = "http://localhost:9222"
    # Resolved click targets whose accessible name matches any of these ALWAYS
    # require confirmation, whatever risk the plan claimed. Enforced in the
    # safety validator, so a misfiring planner cannot bypass it.
    destructive_targets: List[str] = Field(
        default_factory=lambda: ["send", "submit", "pay", "delete", "confirm",
                                 "install", "post", "purchase", "transfer",
                                 "approve"])

    # Guided multi-step tasks (11D). After this many steps without finishing,
    # Anna pauses and checks in rather than running an unbounded chain.
    task_max_steps_before_checkin: int = 5

    # Email (11E). "auto" -> Gmail in the browser if a browser is your default,
    # else the desktop client (Outlook) via mailto. No API/OAuth required;
    # Anna opens a pre-filled draft and the send is a confirmed click.
    email_provider: str = "auto"         # auto | gmail | outlook

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
