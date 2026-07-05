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

### 2. Speech-to-text

Nothing to do — faster-whisper downloads the `base` model (~150 MB) on first
use, then works offline. Model size and language: Settings → Voice input.

### 3. Anna's voice (TTS)

Works out of the box with the built-in Windows voice. For a much more
natural voice, install **Piper**:

1. Download Piper for Windows: https://github.com/rhasspy/piper/releases
2. Download a voice, e.g. `en_US-amy-medium.onnx` (+ its `.json`)
3. Settings → Voice output → set the piper.exe and .onnx paths → **Test voice**

### 4. Run

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
| Window doesn't open | Install the WebView2 runtime (link above) |
| Wake word warning | `pip install openwakeword`, then re-enable the toggle |

## Development

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q   # 126+ tests
python app\main.py --debug                      # DevTools console
```

Architecture, event protocol and safety invariants: [ARCHITECTURE.md](ARCHITECTURE.md).
