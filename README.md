# Anastasia (Anna) 💜 — Local Voice AI Desktop Assistant

A fully local, Jarvis-style voice assistant for Windows with a futuristic
glass UI. Anna listens (push-to-talk or wake word), understands with a small
local LLM via Ollama, safely controls your computer through a whitelist of
tools, and talks back — no cloud, ever.

- **Fast.** Simple commands (open apps/folders, screenshot, clipboard) are
  rule-routed in milliseconds and never touch the LLM.
- **Safety first.** Every action passes a validator; risky actions need your
  explicit approval; destructive actions are disabled entirely.
- **Local.** Ollama + faster-whisper + Piper/Windows TTS, all on-device.

Docs: [ARCHITECTURE.md](ARCHITECTURE.md) · [SKILL.md](SKILL.md) (what you can
say) · [HANDOFF.md](HANDOFF.md) (development state).

## Requirements

- Windows 10/11 + **Microsoft Edge WebView2 runtime** (preinstalled on most
  systems; otherwise the app shows a download link:
  https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
- Python 3.10+ (tested on 3.13)
- ~6 GB free RAM while running (Whisper base + llama3.2:3b)
- A microphone (optional — typing works too)

## Install

```powershell
cd anastasia
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt   # includes pywebview (WebView2 UI)
```

### 1. Ollama (the local brain)

1. Install Ollama: https://ollama.com/download/windows
2. Pull the default model (no hidden "thinking" tokens — fast on CPU):

```powershell
ollama pull llama3.2:3b
```

Other lightweight options: `phi4-mini`, `gemma3:4b`, `qwen3:4b` (set in
Settings). Thinking-mode models like qwen3 are slower for command planning;
Anna disables thinking (`think: false`) either way.

Chat uses a separate compact plain-text prompt. On slower CPUs you can run
`ollama pull llama3.2:1b` and select it as the Chat model in Settings while
keeping `llama3.2:3b` for structured command planning.

### 2. Speech-to-text

Nothing to do — faster-whisper downloads the `base` model (~150 MB) on first
use, then works offline. Anna uses English, beam search, VAD, and a vocabulary
primer built from your configured apps/folders. If recognition is weak, try
`small.en` in Settings → Voice input (slower but more accurate).

### 3. Anna's voice (TTS)

Works out of the box with the built-in Windows voice. For a much more
natural voice, install **Piper**:

1. Install the official runtime from
   https://github.com/OHF-Voice/piper1-gpl with `pip install piper-tts`.
2. Download a voice and its matching config together. The official Piper docs
   require both the `.onnx` model file and the matching `.onnx.json` config
   file. A simple option is:
   `python -m piper.download_voices --download-dir app\data\voices\piper en_US-lessac-medium`
3. Settings -> Voice output -> select the `.onnx` file. `piper.exe` is now
   optional and only used as a legacy fallback if you already have a standalone
   Piper install.
4. Press **Validate Piper**. Anna synthesizes a real test phrase and re-enables
   Piper automatically if it had been benched after failures.

The voice model loads once at startup and stays warm in-process, so each
sentence synthesizes in ~0.2–0.4s (not the ~4.6s a cold `python -m piper`
subprocess costs per sentence). Replies are synthesized sentence-by-sentence
so speaking begins without
waiting for the entire response. An optional Kokoro ONNX setup card is also
available in Settings for a warmer voice (`af_heart` or `af_bella`).

### 4. Cloud brain (optional, hugely faster on weak CPUs)

A 3B local model on a 2017 laptop CPU takes ~10–20s per reply. **Hybrid mode**
sends only your transcribed/typed text to Groq's free tier
(`llama-3.3-70b-versatile`, ~1s replies) and automatically falls back to the
local Ollama model if the cloud is unreachable.

1. Get a free key at https://console.groq.com → API Keys.
2. Settings → Cloud brain → paste it (or set the `GROQ_API_KEY` env var, which
   wins over config), keep mode **hybrid**.
3. The **Brain** chip shows `Groq · 70B` (green), `Local · 3B` (blue), or
   `Local (cloud offline)` (amber). Click it for the circuit state and the
   last 10 LLM calls. `brain_mode: "local_only"` disables the cloud entirely.

**Privacy (enforced in code, see Settings → Privacy):** instant commands never
use any AI model; hybrid sends only transcribed/typed text + recent chat turns;
**files, screenshots and raw audio never leave this PC**; clipboard text stays
local unless you enable the clipboard opt-in. If the cloud fails 3× in a row a
circuit breaker routes everything local for 120s (no dead air), then probes.

### 5. Run

```powershell
python app\main.py            # the app
python app\main.py --doctor   # health check
```

## Using Anna

- **Talk:** press the mic button or **Ctrl + Alt + Space**, speak, stop
  talking (silence ends the recording). Press again while she's speaking to
  cut her off (barge-in).
- **Type:** the input box at the bottom right. Enter sends.
- **Approvals:** terminal/window commands show an amber card — Run it or
  Cancel (auto-cancels in 30 s).
- **Wake word** (optional): `pip install openwakeword`, then flip the toggle.
  Uses the pre-trained "Hey Jarvis" model.
- Full command list: [SKILL.md](SKILL.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| First LLM response is slow | Normal — the model loads + warms up in the background at startup (chip shows it). Rule commands are instant regardless |
| "Ollama is not running" setup card | Start the Ollama app or `ollama serve`, then Recheck |
| "Model not installed" | `ollama pull llama3.2:3b` |
| Model replies with garbage (`@@@@`) | Keep `"ollama_num_gpu": 0` (default) — partial iGPU offload corrupts some models. Only set `-1` (auto) with a dedicated GPU |
| Anna hears herself / transcribes her own voice | Can't happen by design (half-duplex gate mutes the mic during playback +0.4 s). If you use external speakers at high volume and see it anyway, file an issue with the Developer Tools log |
| Voice input does nothing | Settings → Voice input → Test microphone; check the Mic chip in the top bar |
| Robotic voice | Configure Piper (section 3 above) |
| "Voice: Windows fallback (Piper error)" chip | Piper failed twice and was benched for the session. Fix the Piper runtime or voice files, then press Validate Piper. Full errors: `app/data/tts_errors.log` |
| Piper says "voice metadata is incomplete" | The selected `.onnx.json` is missing required Piper fields. Re-download the voice with `python -m piper.download_voices --force-redownload --download-dir app\data\voices\piper <voice-name>` |
| Window doesn't open | Install the WebView2 runtime (link above) |
| Wake word warning | `pip install openwakeword`, then re-enable the toggle |
| "Brain: Local (cloud offline)" chip | Groq failed 3× (no internet / bad key / rate limit); circuit routes local for 120s then auto-probes. Check the key in Settings → Cloud brain |
| Slow spoken replies | The voice model warms once at startup; if replies still lag, confirm the Brain chip is green (Groq) and see `tts_first_audio_ms` in Developer Tools |

## Development

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q   # 126+ tests
python app\main.py --debug                      # DevTools console
```

Architecture, event protocol and safety invariants: [ARCHITECTURE.md](ARCHITECTURE.md).
