# Anastasia (Anna) 💜 — Local Voice AI Desktop Assistant

A lightweight, **fully local** "Jarvis-like" voice assistant for Windows.
Anna listens to your voice, understands your intent with a small local LLM
(via Ollama), safely controls your computer through a whitelist of tools,
and talks back to you.

- **No cloud APIs.** Everything runs on your machine once models are downloaded.
- **Safety first.** Every action passes a safety validator; risky actions need
  your explicit approval; destructive actions are disabled entirely in this MVP.
- **Light.** Runs on CPU with small models (Whisper base + a 3–4B LLM).

---

## Requirements

- **Windows 10/11**
- **Python 3.10+** (tested on 3.13)
- ~6 GB free RAM while running (Whisper base + llama3.2:3b)
- A microphone (optional — you can also type commands)

## Install

```powershell
cd anastasia
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 1. Ollama (the local brain)

1. Install Ollama: https://ollama.com/download/windows
2. Pull the default model (no hidden "thinking" tokens — fast on CPU):

```powershell
ollama pull llama3.2:3b
```

Other lightweight options: `phi4-mini`, `gemma3:4b`, `qwen3:4b`
(set the model name in **Settings** or `app/data/config.json`).
Note: thinking-mode models like `qwen3` are noticeably slower for
command planning; Anna disables thinking (`think: false`) either way.

### 2. Speech-to-text (Whisper)

**Default: faster-whisper** — nothing to do. The `base` model (~150 MB)
downloads automatically the first time you speak, then works offline.

**Alternative: whisper.cpp** — download a build + a ggml model
(e.g. `ggml-base.bin`), then set in Settings:
- `stt_backend` → `whisper_cpp`
- `whisper_cpp_exe` → path to `whisper-cli.exe` / `main.exe`
- `whisper_cpp_model` → path to the `.bin` model

### 3. Text-to-speech (Anna's voice)

**Default:** the built-in Windows female voice (SAPI) — works out of the box.

**Better: Piper** (natural local voice):
1. Download Piper for Windows: https://github.com/rhasspy/piper/releases
2. Download a female voice, e.g. `en_US-amy-medium.onnx` (+ its `.json`):
   https://huggingface.co/rhasspy/piper-voices
3. In Settings set `piper_exe` and `piper_voice` to those paths.

### 4. Wake word (optional, off by default)

```powershell
pip install openwakeword
```

Then flip the **Wake word** switch. It uses the pre-trained **"Hey Jarvis"**
model (a custom "Hey Anna" model would need separate training). Push-to-talk
is the recommended mode.

## Run

```powershell
python app\main.py
```

- Click the big 🎤 button (or press **Ctrl+Alt+Space**) and speak.
  Recording stops automatically after ~1.6 s of silence, or click ⏹.
- Or just **type a command** in the box at the bottom — great for testing.

## Configuration

Everything lives in `app/data/` (created on first run):

| File | Purpose |
|---|---|
| `config.json` | settings — see `config.example.json` in the repo root |
| `memory.json` | Anna's convenience memory (your name, favorite folders…) — **never put secrets here** |
| `history.sqlite` | command/action log |

Edit via the **⚙ Settings** window or directly in the JSON files.

- **App aliases** (`app_aliases`): spoken name → launch command or absolute path.
  e.g. `"spotify": "spotify"` or `"chrome": "C:/Program Files/Google/Chrome/Application/chrome.exe"`.
- **Safe folders** (`safe_folders`): the *only* folders Anna may open/search.

## What Anna can do (MVP tools)

| Tool | Confirmation? |
|---|---|
| open_app, open_folder, take_screenshot | no |
| clipboard read/write/summarize, search_files | no |
| type_text | only if > 500 chars |
| press_hotkey (ctrl+c/v/a/s, alt+tab, win+d) | ctrl+shift+esc only |
| browser_open (URL / web search) | no |
| run_terminal | **always** |
| window_control (close/min/max) | **always** |
| delete_files | confirmation shown, **execution disabled** |

## Demo commands

- "Anna, open Chrome" / "open VS Code" / "open notepad"
- "Anna, open Downloads" / "open my projects folder"
- "Anna, take a screenshot" → saved to `Pictures/AnnaScreenshots`
- "Anna, type hello world"
- "Anna, copy this" / "paste"
- "Anna, read my clipboard" / "summarize my clipboard"
- "Anna, search my Downloads for invoice"
- "Anna, open YouTube" / "search Google for python virtual environment"
- "Anna, run npm dev" → **asks for confirmation**
- "Anna, close this window" → **asks for confirmation**
- "Anna, shutdown my laptop" → **refused** (blocked in MVP)
- "Anna, how's your day going?" → she'll just chat with you 💜

## Safety limitations (by design)

- The LLM **never executes anything** — it only emits JSON plans, which are
  validated against a whitelist (`app/agent/safety.py`).
- Blocked outright: file deletion*, shutdown/restart, emails/messages, form
  submission, payments, killing processes, installing software, anything
  touching passwords/credentials/security, arbitrary code execution.
  (*deletion shows the confirmation flow but the executor is a stub.)
- Dangerous terminal patterns (`format`, `rm -rf`, `reg delete`, encoded
  PowerShell, downloading executables…) are refused even *with* confirmation.
- Folder access is restricted to your configured safe folders.
- Raw audio is deleted immediately after transcription and never logged.

## Tests

```powershell
python -m pytest tests -q
```

## Project layout

```
app/
  main.py            entry point + controller (threads, pipeline)
  config.py          Pydantic config, config.json
  gui/               CustomTkinter UI (main window, status, confirmation, settings)
  voice/             recorder, whisper STT, piper/SAPI TTS, wake word
  llm/               Ollama client, prompts, JSON intent parsing
  agent/             router (rules + LLM), safety validator, memory, history
  tools/             whitelisted executors (apps, files, clipboard, browser…)
tests/               safety / parser / tool tests
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "The local AI model is not running" | Start the Ollama app or `ollama serve` |
| "Model not installed" | `ollama pull llama3.2:3b` |
| Model replies with garbage (`@@@@`) | keep `"ollama_num_gpu": 0` — partial iGPU offload corrupts some models. Only set `-1` (auto) with a dedicated GPU |
| No transcription | `pip install faster-whisper`; check mic in Windows privacy settings |
| No global hotkey | run the terminal as admin (the `keyboard` lib needs it sometimes) |
| Robotic voice | set up Piper (see above) |
