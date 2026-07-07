"""Piper TTS setup checks, synthesis, and validation."""

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

OFFICIAL_PIPER_PACKAGE = "piper-tts"
OFFICIAL_PIPER_REPO = "github.com/OHF-Voice/piper1-gpl"
_VOICE_CONFIG_FIELDS = (
    ("audio.sample_rate", lambda cfg: cfg["audio"]["sample_rate"]),
    ("espeak.voice", lambda cfg: cfg["espeak"]["voice"]),
    ("num_symbols", lambda cfg: cfg["num_symbols"]),
    ("num_speakers", lambda cfg: cfg["num_speakers"]),
    ("phoneme_id_map", lambda cfg: cfg["phoneme_id_map"]),
)


def piper_json_path(voice_path) -> Path:
    if not voice_path:
        return Path("__missing_voice__.onnx.json")
    voice = Path(voice_path or "")
    return voice.with_suffix(voice.suffix + ".json")


def _official_piper_version() -> str | None:
    try:
        return importlib.metadata.version(OFFICIAL_PIPER_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def _configured_piper_exe(config) -> Path | None:
    configured = (config.piper_exe or "").strip()
    if not configured:
        return None
    exe = Path(configured)
    return exe if exe.is_file() else None


def _resolve_path_piper() -> Path | None:
    which = shutil.which("piper")
    return Path(which) if which else None


def _resolve_piper_runtime(config) -> tuple[list[str] | None, str, str]:
    version = _official_piper_version()
    if version:
        return [sys.executable, "-m", "piper"], "module", f"{OFFICIAL_PIPER_PACKAGE} {version}"

    exe = _configured_piper_exe(config)
    if exe is not None:
        return [str(exe)], "legacy_executable", str(exe)

    path_exe = _resolve_path_piper()
    if path_exe is not None:
        return [str(path_exe)], "path_executable", str(path_exe)

    return None, "", (
        f"Piper runtime not found. Install {OFFICIAL_PIPER_PACKAGE} from "
        f"{OFFICIAL_PIPER_REPO} or configure a Piper executable in Settings."
    )


def _voice_repair_hint(voice_path) -> str:
    voice = Path(voice_path or "")
    if not voice.name:
        return " Re-download the official .onnx and matching .onnx.json for this voice."
    if _official_piper_version():
        return (
            " Re-download the voice with: "
            f"python -m piper.download_voices --force-redownload --download-dir "
            f'"{voice.parent}" {voice.stem}'
        )
    return (
        " Replace the .onnx and matching .onnx.json with the official "
        "Piper voice files."
    )


def _load_voice_metadata(voice_path) -> tuple[bool, str, dict | None]:
    metadata = piper_json_path(voice_path)
    if not metadata.is_file():
        return False, f"Missing matching voice metadata: {metadata.name}", None
    try:
        config = json.loads(metadata.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return (
            False,
            f"Invalid Piper voice metadata: {metadata.name} is not valid JSON "
            f"({exc.msg}).{_voice_repair_hint(voice_path)}",
            None,
        )
    if not isinstance(config, dict):
        return (
            False,
            f"Invalid Piper voice metadata: {metadata.name} must contain a JSON object."
            f"{_voice_repair_hint(voice_path)}",
            None,
        )

    missing = []
    for field_name, getter in _VOICE_CONFIG_FIELDS:
        try:
            value = getter(config)
        except (KeyError, TypeError):
            value = None
        if value in (None, "", {}, []):
            missing.append(field_name)
    if missing:
        return (
            False,
            f"Invalid Piper voice metadata: {metadata.name} is incomplete "
            f"(missing {', '.join(missing)}).{_voice_repair_hint(voice_path)}",
            None,
        )

    try:
        from piper.config import PiperConfig
        PiperConfig.from_dict(config)
    except ImportError:
        pass
    except Exception as exc:
        return (
            False,
            f"Invalid Piper voice metadata: {metadata.name} could not be loaded "
            f"by {OFFICIAL_PIPER_PACKAGE} ({exc}).{_voice_repair_hint(voice_path)}",
            None,
        )

    return True, "Piper voice metadata looks valid.", config


def piper_setup_status(config) -> tuple[bool, str]:
    runtime_cmd, _runtime_kind, runtime_detail = _resolve_piper_runtime(config)
    if runtime_cmd is None:
        return False, runtime_detail

    voice = Path(config.piper_voice or "")
    if not config.piper_voice or not voice.is_file():
        return False, (
            "Piper runtime found, but the .onnx voice file is still missing. "
            "Select a voice in Settings."
        )

    meta_ok, meta_message, _meta = _load_voice_metadata(config.piper_voice)
    if not meta_ok:
        return False, meta_message

    return True, f"Piper is ready via {runtime_detail}."


def piper_available(config) -> bool:
    return piper_setup_status(config)[0]


def _build_piper_command(config, wav_path: Path) -> list[str]:
    runtime_cmd, runtime_kind, runtime_detail = _resolve_piper_runtime(config)
    if runtime_cmd is None:
        raise FileNotFoundError(runtime_detail)

    metadata = piper_json_path(config.piper_voice)
    length_scale = max(
        0.5,
        min(2.0, float(getattr(config, "piper_length_scale", 1.08) or 1.08)),
    )
    rounded = str(round(length_scale, 2))
    if runtime_kind == "module":
        return runtime_cmd + [
            "-m", str(config.piper_voice),
            "-c", str(metadata),
            "-f", str(wav_path),
            "--length-scale", rounded,
        ]
    return runtime_cmd + [
        "--model", str(config.piper_voice),
        "--output_file", str(wav_path),
        "--length_scale", rounded,
    ]


def _ensure_nonempty_wav(wav_path: Path) -> None:
    if not wav_path.is_file():
        raise RuntimeError("Piper produced no audio file.")
    with wave.open(str(wav_path), "rb") as audio:
        if audio.getnframes() <= 0 or audio.getframerate() <= 0:
            raise RuntimeError("Piper created an empty WAV file.")


# --- in-process synthesis (warm voice) --------------------------------------
# Spawning `python -m piper` per sentence costs ~4.6s each (interpreter +
# onnxruntime + model load, all repeated). Loading a PiperVoice ONCE and
# reusing it drops synthesis to ~0.1-0.4s/sentence. The voice is cached by
# path and guarded by a lock (load must be serialized; the loaded ONNX
# session is reused across the single TTS worker thread).
_inproc_voice = None
_inproc_voice_key = None
_inproc_lock = threading.Lock()
_inproc_disabled = False   # set if the Python API is unavailable/errors on load


def _length_scale(config) -> float:
    return max(0.5, min(2.0, float(getattr(config, "piper_length_scale", 1.08) or 1.08)))


def get_inproc_voice(config):
    """Return a cached PiperVoice for the configured .onnx, loading it once.
    Returns None if the official piper Python API isn't importable (callers
    then fall back to the subprocess path). Thread-safe."""
    global _inproc_voice, _inproc_voice_key, _inproc_disabled
    if _inproc_disabled or _official_piper_version() is None:
        return None
    voice_path = str(config.piper_voice or "")
    if not voice_path:
        return None
    with _inproc_lock:
        if _inproc_voice is not None and _inproc_voice_key == voice_path:
            return _inproc_voice
        try:
            from piper import PiperVoice
            metadata = piper_json_path(voice_path)
            _inproc_voice = PiperVoice.load(
                voice_path,
                config_path=str(metadata) if metadata.is_file() else None)
            _inproc_voice_key = voice_path
            return _inproc_voice
        except Exception as exc:
            # Importable but load failed — don't retry every sentence.
            _inproc_disabled = True
            _inproc_voice = None
            _inproc_voice_key = None
            from app.agent.devlog import devlog
            devlog.warn(f"In-process Piper unavailable ({' '.join(str(exc).split())[:150]}); "
                        "using the slower subprocess path.")
            return None


def _synthesize_inproc(text: str, config, wav_path: Path) -> bool:
    """Synthesize with the warm voice. Returns True on success, False if the
    in-process API isn't usable (caller falls back to subprocess)."""
    voice = get_inproc_voice(config)
    if voice is None:
        return False
    from piper import SynthesisConfig
    syn = SynthesisConfig(length_scale=_length_scale(config),
                          volume=max(0.0, min(1.0, getattr(config, "tts_volume", 100) / 100)))
    with _inproc_lock:                       # serialize inference on the session
        with wave.open(str(wav_path), "wb") as wf:
            voice.synthesize_wav(text, wf, syn_config=syn)
    _ensure_nonempty_wav(wav_path)
    return True


def warm_piper(config) -> None:
    """Preload the voice at startup so the first spoken sentence is fast."""
    try:
        get_inproc_voice(config)
    except Exception:
        pass


def _format_process_error(proc: subprocess.CompletedProcess, wav_path: Path) -> str:
    details = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if details:
        return " ".join(details.split())[:300]
    if wav_path.is_file():
        return f"Piper exited with code {proc.returncode} after creating unusable audio."
    return f"Piper exited with code {proc.returncode} without creating audio."


def synthesize_piper(text: str, config, wav_path: Path,
                     timeout: float = 60) -> None:
    ok, message = piper_setup_status(config)
    if not ok:
        raise FileNotFoundError(message)
    # Fast path: warm in-process voice (~0.1-0.4s). Falls through to the
    # subprocess only when the Python API isn't usable.
    if _synthesize_inproc(text, config, wav_path):
        return
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        _build_piper_command(config, wav_path),
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        creationflags=creation,
    )
    if proc.returncode != 0 or not wav_path.is_file():
        raise RuntimeError(_format_process_error(proc, wav_path))
    _ensure_nonempty_wav(wav_path)


def _temp_wav(prefix: str) -> Path:
    name = f"{prefix}_{os.getpid()}_{threading.get_ident()}.wav"
    return Path(tempfile.gettempdir()) / name


def validate_piper_config(config, play: bool = False) -> tuple[bool, str]:
    """Run the selected runtime and load the chosen voice with a real phrase."""
    wav_path = _temp_wav("anna_piper_test")
    try:
        synthesize_piper("Hi, it's Anna. Piper is ready.", config, wav_path,
                         timeout=20)
        _ensure_nonempty_wav(wav_path)
        if play:
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
        return True, "Piper validated - runtime and voice both work."
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
