"""Microphone recorder with optional silence auto-stop.
Runs off the GUI thread; sounddevice/numpy are lazy-imported."""

import threading
import time
import wave
from pathlib import Path


class MicrophoneError(Exception):
    pass


def _device_signature(name: str, hostapi: str, channels: int, sample_rate: int) -> str:
    return f"{name}|{hostapi}|{channels}|{sample_rate}"


def _normalize_devices(raw_devices) -> list[dict]:
    if raw_devices is None:
        return []
    if isinstance(raw_devices, dict):
        return [raw_devices]
    return list(raw_devices)


def _hostapi_name(sd, device: dict) -> str:
    try:
        hostapi_index = device.get("hostapi")
        hostapis = _normalize_devices(sd.query_hostapis())
        if isinstance(hostapi_index, int) and 0 <= hostapi_index < len(hostapis):
            return str(hostapis[hostapi_index].get("name", "")).strip()
    except Exception:
        pass
    return ""


def _input_device_entries(sd=None) -> list[dict]:
    if sd is None:
        import sounddevice as sd

    raw_devices = _normalize_devices(sd.query_devices())
    try:
        default_device = sd.query_devices(kind="input")
    except Exception:
        default_device = None
    default_signature = None
    if isinstance(default_device, dict):
        default_signature = _device_signature(
            str(default_device.get("name", "Default microphone")),
            _hostapi_name(sd, default_device),
            int(default_device.get("max_input_channels", 0) or 0),
            int(float(default_device.get("default_samplerate", 0) or 0)),
        )

    entries = []
    for index, device in enumerate(raw_devices):
        channels = int(device.get("max_input_channels", 0) or 0)
        if channels <= 0:
            continue
        name = str(device.get("name", f"Input {index}"))
        hostapi = _hostapi_name(sd, device)
        sample_rate = int(float(device.get("default_samplerate", 0) or 0))
        signature = _device_signature(name, hostapi, channels, sample_rate)
        label = name if not hostapi else f"{name} ({hostapi})"
        if signature == default_signature:
            label += " - default"
        entries.append({
            "id": f"mic::{index}::{signature}",
            "index": index,
            "name": name,
            "hostapi": hostapi,
            "signature": signature,
            "label": label,
            "default_samplerate": sample_rate,
            "is_default": signature == default_signature,
        })
    return entries


# Windows lists the same physical mic once per host API. MME + WASAPI cover
# every real device cleanly; DirectSound/WDM-KS just duplicate them (with
# uglier names), so we hide those from the picker.
_DISPLAY_HOSTAPIS = {"windows wasapi": 0, "mme": 1}

# Virtual / loopback / placeholder "devices" that aren't a usable microphone.
_VIRTUAL_MARKERS = ("sound mapper", "primary sound capture", "stereo mix",
                    "wave out mix", "what u hear", "loopback", "sum",
                    "output ()", "input ()")


def _dedupe_key(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:18]


def _is_real_mic(entry: dict) -> bool:
    name = str(entry.get("name", "")).strip()
    low = name.lower()
    if not name or low.startswith("input (@") or low in ("input ()", "input"):
        return False
    return not any(marker in low for marker in _VIRTUAL_MARKERS)


def _dedupe_for_display(entries: list[dict]) -> list[dict]:
    """Real microphones only, one entry per physical device. Hides duplicate
    host APIs and virtual/loopback devices so the picker isn't cluttered."""
    candidates = [e for e in entries
                  if str(e.get("hostapi", "")).lower() in _DISPLAY_HOSTAPIS
                  and _is_real_mic(e)]
    best: dict[str, dict] = {}
    for entry in candidates:
        key = _dedupe_key(entry.get("name", ""))
        prio = _DISPLAY_HOSTAPIS.get(str(entry.get("hostapi", "")).lower(), 9)
        current = best.get(key)
        if (current is None or entry.get("is_default")
                or (not current.get("is_default")
                    and prio < _DISPLAY_HOSTAPIS.get(
                        str(current.get("hostapi", "")).lower(), 9))):
            best[key] = entry
    seen, out = set(), []
    for entry in candidates:                 # keep discovery order
        key = _dedupe_key(entry.get("name", ""))
        if key not in seen:
            seen.add(key)
            out.append(best[key])
    return out


def _display_label(entry: dict) -> str:
    """Clean label without the noisy host-API suffix; keep the default tag."""
    label = str(entry.get("name", "Microphone")).strip()
    if entry.get("is_default"):
        label += " — default"
    return label

def _device_default_sample_rate(device_info: dict | None, fallback: int) -> int:
    try:
        rate = int(float((device_info or {}).get("default_samplerate", 0) or 0))
    except (TypeError, ValueError):
        rate = 0
    return rate if rate > 0 else fallback


def _rate_supported(sd, device_arg, sample_rate: int) -> bool:
    check = getattr(sd, "check_input_settings", None)
    if check is None:
        return True
    try:
        check(device=device_arg, samplerate=sample_rate, channels=1, dtype="int16")
        return True
    except Exception:
        return False


def choose_capture_sample_rate(sd, config, device_arg, device_info: dict | None):
    """Pick a hardware capture rate. Prefer Anna's 16k path, but gracefully
    fall back to the device's native/default rate for USB/analog interfaces."""
    preferred = int(getattr(config, "sample_rate", 16000) or 16000)
    device_default = _device_default_sample_rate(device_info, preferred)
    candidates = [preferred, device_default, 48000, 44100, 32000, 22050, 16000]
    seen = set()
    for rate in candidates:
        if rate <= 0 or rate in seen:
            continue
        seen.add(rate)
        if _rate_supported(sd, device_arg, rate):
            return rate
    return preferred

def list_microphones() -> list[dict]:
    try:
        entries = _dedupe_for_display(_input_device_entries())
    except Exception:
        entries = []
    options = [{"id": "", "label": "System default microphone"}]
    options.extend({"id": entry["id"], "label": _display_label(entry)}
                   for entry in entries)
    return options


def resolve_microphone_device(config, entries: list[dict] | None = None):
    if entries is None:
        try:
            entries = _input_device_entries()
        except Exception:
            entries = []

    preferred = str(getattr(config, "microphone_device", "") or "").strip()
    if not preferred:
        default_entry = next((entry for entry in entries if entry["is_default"]),
                             entries[0] if entries else None)
        return None, default_entry, ""

    match = next((entry for entry in entries if entry["id"] == preferred), None)
    if match is None and preferred.startswith("mic::") and "::" in preferred:
        signature = preferred.split("::", 2)[-1]
        signature_matches = [entry for entry in entries
                             if entry["signature"] == signature]
        if len(signature_matches) == 1:
            match = signature_matches[0]
    if match is None and preferred.isdigit():
        match = next((entry for entry in entries if entry["index"] == int(preferred)), None)
    if match is None:
        name_matches = [entry for entry in entries if entry["name"] == preferred]
        if len(name_matches) == 1:
            match = name_matches[0]

    if match is not None:
        return match["index"], match, ""

    default_entry = next((entry for entry in entries if entry["is_default"]),
                         entries[0] if entries else None)
    warning = (f"Selected microphone '{preferred}' isn't available, so Anna is "
               "using the system default microphone.")
    return None, default_entry, warning


def microphone_dropdown_state(config):
    try:
        all_entries = _input_device_entries()
    except Exception:
        all_entries = []
    entries = _dedupe_for_display(all_entries)
    options = [{"id": "", "label": "System default microphone"}]
    options.extend({"id": entry["id"], "label": _display_label(entry)}
                   for entry in entries)
    preferred = str(getattr(config, "microphone_device", "") or "").strip()
    # resolve against the FULL list so a previously-saved (now-deduped) id
    # still matches by signature
    _device_arg, selected_entry, warning = resolve_microphone_device(config, entries=all_entries)
    if preferred and selected_entry is not None and not warning:
        # map the resolved device to its deduped display entry (same mic)
        key = _dedupe_key(selected_entry.get("name", ""))
        shown = next((e for e in entries
                      if _dedupe_key(e.get("name", "")) == key), None)
        selected_id = (shown or selected_entry)["id"]
    else:
        selected_id = ""
    note = warning if warning else ("No microphones detected right now."
                                    if not entries else "")
    return options, selected_id, note


class Recorder:
    def __init__(self, config):
        self.config = config
        self._frames = []
        self._stream = None
        self._recording = False
        self._speech_seen = False
        self._silence_start = None
        self._start_time = 0.0
        self._on_auto_stop = None
        self._last_logged_device = None
        self._last_device_warning = None
        self._capture_sample_rate = int(getattr(config, "sample_rate", 16000) or 16000)
        self._frame_observer = None   # streaming STT: called per captured frame
        self._rolling_seconds = 0.0   # >0 = bounded live-mode buffer (10C)

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def capture_sample_rate(self) -> int:
        return self._capture_sample_rate

    def set_frame_observer(self, cb) -> None:
        """cb(pcm_int16_bytes) receives each captured frame live (streaming
        STT). The frames are STILL buffered locally as the Whisper safety net."""
        self._frame_observer = cb

    def buffered_pcm(self) -> bytes:
        """Audio captured so far as 16k int16 PCM for streaming STT replay."""
        if not self._frames:
            return b""
        try:
            import numpy as np
            data = np.concatenate(self._frames, axis=0)
            return normalize_audio_for_stt(
                data, self._capture_sample_rate, 16000).tobytes()
        except Exception:
            return b""

    def start(self, on_auto_stop=None, rolling_seconds: float = 0.0) -> None:
        """Begin recording. on_auto_stop fires (once, from a worker thread)
        when trailing silence or the max duration is reached.

        rolling_seconds > 0 (Gemini Live mode, 10C): keep only the most
        recent N seconds in the local buffer — the conversation can run
        indefinitely without growing memory, while the tail stays available
        as the same-turn fallback for local Whisper if Live drops."""
        if self._recording:
            return
        try:
            import sounddevice as sd
        except Exception as e:
            raise MicrophoneError(f"Audio library unavailable: {e}") from e

        device_arg, device_info, warning = resolve_microphone_device(self.config)
        capture_rate = choose_capture_sample_rate(
            sd, self.config, device_arg, device_info)
        fallback_rate = _device_default_sample_rate(
            device_info, int(getattr(self.config, "sample_rate", 16000) or 16000))
        self._frames = []
        self._speech_seen = False
        self._silence_start = None
        self._on_auto_stop = on_auto_stop
        self._rolling_seconds = float(rolling_seconds or 0.0)
        self._start_time = time.time()
        try:
            self._stream = sd.InputStream(
                samplerate=capture_rate, channels=1, device=device_arg,
                dtype="int16", callback=self._callback)
            self._stream.start()
        except Exception as first_error:
            self._stream = None
            if fallback_rate > 0 and fallback_rate != capture_rate:
                try:
                    self._stream = sd.InputStream(
                        samplerate=fallback_rate, channels=1, device=device_arg,
                        dtype="int16", callback=self._callback)
                    self._stream.start()
                    capture_rate = fallback_rate
                except Exception as fallback_error:
                    self._stream = None
                    raise MicrophoneError(
                        "Could not open the microphone at "
                        f"{capture_rate}Hz or {fallback_rate}Hz: "
                        f"{fallback_error}") from fallback_error
            else:
                raise MicrophoneError(
                    f"Could not open the microphone: {first_error}") from first_error
        self._capture_sample_rate = capture_rate
        self._recording = True
        try:
            from app.agent.devlog import devlog
            if warning and warning != self._last_device_warning:
                devlog.warn(warning)
                self._last_device_warning = warning
            elif not warning:
                self._last_device_warning = None
            label = (device_info or {}).get("label", "System default microphone")
            if label != self._last_logged_device:
                default_rate = int((device_info or {}).get("default_samplerate", 0) or 0)
                devlog.log(f"Microphone input: {label} | capture: "
                           f"{self._capture_sample_rate}Hz mono | "
                           f"device: {default_rate}Hz")
                self._last_logged_device = label
        except Exception:
            pass

    def _callback(self, indata, frames, time_info, status) -> None:
        if not self._recording:
            return
        from app.voice import audio_gate
        if audio_gate.speaking.is_set():
            return  # half-duplex: never capture Anna's own voice (sec 13a)
        import numpy as np
        self._frames.append(indata.copy())
        if self._rolling_seconds > 0:   # live mode: bounded local buffer
            cap = int(self._rolling_seconds * self._capture_sample_rate)
            total = sum(len(f) for f in self._frames)
            while len(self._frames) > 1 and total - len(self._frames[0]) >= cap:
                total -= len(self._frames[0])
                del self._frames[0]
        if self._frame_observer is not None:   # feed streaming STT live
            try:
                frame = normalize_audio_for_stt(
                    indata, self._capture_sample_rate, 16000)
                self._frame_observer(frame.tobytes())
            except Exception:
                pass
        now = time.time()
        cfg = self.config
        if cfg.silence_auto_stop and self._on_auto_stop is not None:
            rms = float(np.sqrt(np.mean((indata.astype(np.float32) / 32768.0) ** 2)))
            if rms > cfg.silence_threshold:
                self._speech_seen = True
                self._silence_start = None
            elif self._speech_seen:
                if self._silence_start is None:
                    self._silence_start = now
                elif now - self._silence_start >= cfg.silence_seconds:
                    self._fire_auto_stop()
        if now - self._start_time > cfg.max_record_seconds:
            self._fire_auto_stop()

    def _fire_auto_stop(self) -> None:
        cb, self._on_auto_stop = self._on_auto_stop, None
        if cb:
            threading.Thread(target=cb, daemon=True).start()

    def stop(self):
        """Stop recording; returns int16 numpy array or None if empty."""
        self._recording = False
        self._on_auto_stop = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        if not self._frames:
            return None
        import numpy as np
        return np.concatenate(self._frames, axis=0)

    def cancel(self) -> None:
        """Stop recording and discard the captured audio (no transcription)."""
        self.stop()
        self._frames = []

    def save_wav(self, data, path: Path, sample_rate: int = None) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate or self.config.sample_rate)
            wf.writeframes(data.tobytes())


def normalize_audio_for_stt(data, source_rate: int, target_rate: int = 16000):
    """Return int16 mono audio resampled to Whisper's preferred rate."""
    import numpy as np

    audio = np.asarray(data)
    if audio.ndim > 1:
        audio = audio.astype(np.float32).mean(axis=1)
    else:
        audio = audio.astype(np.float32)
    if not len(audio):
        return np.asarray([], dtype=np.int16)
    if source_rate != target_rate:
        output_length = max(1, round(len(audio) * target_rate / source_rate))
        old_positions = np.linspace(0.0, 1.0, len(audio), endpoint=False)
        new_positions = np.linspace(0.0, 1.0, output_length, endpoint=False)
        audio = np.interp(new_positions, old_positions, audio)
    return np.clip(audio, -32768, 32767).astype(np.int16)


def microphone_available() -> bool:
    try:
        return bool(_input_device_entries())
    except Exception:
        return False
