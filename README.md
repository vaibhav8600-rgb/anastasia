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

**Answering by voice when there's no window open (D-0.5).** When Anna runs
headless (the `anna-core` background service with no window attached) and she
needs your approval for something, the only way to answer is by voice — so, and
*only* then, she opens the microphone to hear "Anna approve" or "cancel". This
is deliberate and tightly fenced: it happens **only when no window is
connected**, **only for a card the safety validator itself demanded**, she
**speaks the question first** and the mic stays shut until she's done, an
**audible cue** marks the moment the mic opens (a windowless mic is never
silent), and it's **one short listen window** — one answer, then closed. The
strong phrase is unchanged: destructive actions still require "Anna approve", a
casual "yes" is still refused. Attach a window and clicking takes over again.
Turn it off entirely with `headless_voice_confirm: false` in config (then a
windowless Anna simply can't be told "yes" and the card expires).

### 5. Streaming speech + hands-free conversation (optional)

Local Whisper batches your audio and can take several seconds. **Streaming
mode** (Deepgram) sends live mic audio while you talk and returns a final
transcript ~0.3s after you stop, with a live interim transcript as you speak.

1. Free key at https://console.deepgram.com → Settings → Voice input → set
   **Speech recognition** to *streaming* + paste the key (or `DEEPGRAM_API_KEY`
   env var). Local Whisper stays the automatic fallback if streaming fails.
2. **Live-audio privacy:** while the mic is open in streaming mode, audio goes
   to Deepgram — shown unmistakably by a **magenta mic ring + "streaming live
   audio to Deepgram" badge**. The socket closes the instant the mic closes.
   Local mode keeps all audio on-device. Files/screenshots never stream.
3. **Streamed replies:** chat answers stream token-by-token — Anna starts
   speaking sentence 1 while the brain writes sentence 2 (command planning
   stays non-streamed and fully validated locally first).
4. **Continuous conversation:** the bottom-bar **Conversation** toggle keeps
   the mic open between turns — just talk, no button. Say "stop listening",
   "that's all", "bye", tap the mic, or stay quiet 45s to end it. Half-duplex
   (she never hears herself) and barge-in (talk over her) both hold.

`turn_latency_ms` in Developer Tools measures "you stop speaking → Anna's
first word". With streaming + Groq it's typically ~1–2s (the variable is the
network round-trip to Groq; local warm-Piper TTS is ~0.25–0.5s).

### 6. Gemini Live — the premium conversation engine (optional)

Anna has **three conversation engines** (the top-bar **Engine** chip shows
which one is active):

| Engine | What it is | Speed | Needs | Privacy |
|---|---|---|---|---|
| **Gemini Live** (purple) | Native speech-to-speech over one WebSocket — no STT/LLM/TTS handoffs, emotion-aware HD voice, natural barge-in | sub-second | Gemini API key, internet, **billing + privacy opt-in** | **streams your mic to Google continuously while the mic is open**; metered per audio minute |
| **Pipeline** (default) | The Phase-9 stack: Whisper/Deepgram → Groq/Ollama → Piper/Aura with layered fallbacks | ~1.5–2.5s/turn | nothing extra | audio leaves only if you chose streaming STT / Aura |
| **Local** | Whisper → Ollama → Piper, forced | slowest | nothing, works offline | nothing ever leaves this PC |

Setup: key from https://aistudio.google.com → Settings → Conversation engine →
paste it (or `GEMINI_API_KEY` env var), pick **Gemini Live**, and accept the
plain-language **consent card** (what streams, what it costs, how to leave).
Selecting the engine alone is *not* consent — and the session itself
hard-gates on both (`PrivacyViolation` otherwise), so nothing can stream
around it. Model default: `gemini-3.1-flash-live-preview` (preview tier —
Google renames these; editable in Settings if it churns).

**How it behaves:**

- Tap the mic once → one continuous conversation (the model handles
  turn-taking, talk over her freely); tap again → everything closes.
  While live: purple **Engine** chip with a pulsing dot, purple mic ring,
  and a persistent **"● Live — audio streaming to Google · minutes · ~$"**
  badge with the running session cost.
- **Every tool call is still validated locally** — same safety rules,
  same confirmation cards, same whitelist as every other engine. The cloud
  model can only *request*; blocked tools aren't even declared to it.
  Screenshots, files and clipboard keep their never-cloud/opt-in rules.
- **Instant commands stay local**: "open paint" etc. run through the local
  rule router immediately, even in Live mode (toggle in Settings).
- **Any Live failure falls back to the pipeline in the same turn** — the
  rolling mic buffer is transcribed locally so your sentence isn't lost.
  3 failures open a circuit for 120s (pipeline handles everything), then a
  probe re-enables Live. Offline skips Live entirely.
- **Cost safety:** per-minute prices are editable (defaults ~$0.005 in /
  $0.018 out — verify against Google's current pricing), a month-to-date
  estimate shows in Settings, an optional monthly **soft cap warns** (never
  blocks), and an idle session **auto-closes after 60s** of quiet so a
  forgotten session can't bill silently. Sessions also close with the app.
- **Voice:** warm HD voice **Sulafat** by default, picker in Settings.
  Emotion-aware *affective dialog* is wired but only applies on 2.5-era
  Live models (Gemini 3.1's voice is already emotion-aware natively).

### 7. Run

```powershell
python app\main.py            # the app
python app\main.py --doctor   # health check
```

## Using Anna

- **Talk:** press the mic button or **Ctrl + Alt + Space**, speak, stop
  talking (silence ends the recording). Press again while she's speaking to
  cut her off (barge-in).
- **Type:** the input box at the bottom right. Enter sends.
- **Approvals (voice or click):** risky commands show an amber card — click
  *Run it* / *Cancel*, or just say it. Whichever comes first wins.

  | Say | Effect |
  |---|---|
  | `approve` · `yes` · `do it` · `go ahead` · `run it` | approve an ordinary confirmation |
  | **`anna approve`** · `i approve` · `confirm action` | **required** for destructive-tier actions (see below) |
  | `cancel` · `no` · `stop` · `not now` · `leave it` | cancel (a casual word is always enough to stop) |
  | `what are you asking?` | Anna repeats the confirmation aloud |
  | `show details` | expands the card with the exact tool + arguments |

  **Destructive tier needs the strong phrase.** Terminal commands, deletes,
  moves/renames and (from 11C) sends/submits are refused a plain "yes" — the
  card turns red and Anna asks for **"Anna approve"**. This is decided from
  the *safety validator's* result plus a hardcoded tool list, so a plan that
  under-states its own risk cannot dodge it.

  Anything Anna doesn't recognise as approve/cancel is **never** treated as
  approval — she asks once more and the action stays parked. A stray "yes"
  with nothing pending does nothing and is routed as normal speech.
  Confirmations auto-cancel after `confirmation_timeout_s` (default 30 s),
  and only one can be pending at a time — a second risky action is deferred,
  never silently swapped in.

  By default Anna does **not** open the mic on her own to hear your approval:
  press push-to-talk (or the card's voice button) and speak. If you want the
  mic to reopen automatically while a card is up during continuous
  hands-free conversation, set `confirmation_voice_listen: true`.
- **Wake word** (optional): flip the toggle and just say **"Hey Anna"** (or
  "Anastasia"). This uses the local Whisper STT to listen for her name — no
  training, no extra install. Say the full "Hey Anna"; a bare "Anna" is too
  short for reliable recognition, and there's a ~1–3 s recognition delay
  since it's local speech-to-text. Prefer the classic "Hey Jarvis" wake model?
  `pip install openwakeword` and set `wake_word_backend: "openwakeword"`.
- **Vision — Anna can look, but only when you ask.** Nothing is ever captured
  silently, and a badge is on screen for the entire time anything is.

  | Say | What happens |
  |---|---|
  | `look at my screen` · `what's on my screen` · `read this error` · `summarize this page` | **one** frame of the desktop, read once, thrown away |
  | `analyze this window` | just the focused window (best for reading text) |
  | `what's under my cursor` | a small box around the pointer |
  | `watch my screen` | **one frame every ~1.5 s**, each read independently and discarded — a cyan *Screen Vision Active* badge + border stays up the whole time, and it stops itself when idle |
  | `stop looking` | ends watching |
  | `what do you see` · `look through the camera` | camera opens for **exactly one frame**, then stops — red *Camera on* badge for precisely that moment |
  | **`privacy mode`** | kills screen watching, the camera, **and** any Gemini Live audio session, instantly |

  **This is snapshots, never a stream.** Even watching mode grabs single stills;
  no video feed is ever opened to any cloud provider, and no frame outlives the
  moment it is read.

  **Frames stay on this PC by default.** Text is extracted by local OCR
  (install Tesseract: `winget install UB-Mannheim.TesseractOCR` then
  `python -m pip install pytesseract` — without it Anna says so honestly rather
  than pretending she read your screen). Sending a frame to Gemini for a richer
  description needs its **own** consent toggle in Settings → Vision, separate
  from every other cloud setting and **off by default**. Frames are never
  saved to disk unless you turn that on, and never appear in the logs.

  **Screens that look sensitive are not analyzed at all** — not locally, not in
  the cloud. If Anna spots a password field, an API key, or words like "account
  number" or "CVV", she stops and asks. Overriding that ("look at my screen
  anyway") is a **high-risk action**, so it needs the strong approval phrase.

  That check is fed by local OCR, so **if the scan can't run, Anna won't send
  the screen either** — a scan that never ran is not a scan that found nothing.
  (Without Tesseract installed at all, cloud vision still works, but the log
  says plainly that no pre-scan happened.)

  On a **multi-monitor** setup, "what's on my screen" captures the monitor you're
  actually working on, not both squashed together. Say "screen two" to pick one.

#### Setting up the camera

Anna opens the webcam only when you ask, for **one frame**, then closes it — with
a red badge and a live self-view so you can see exactly what she captured.

Go to **Settings → Vision → Camera**, pick your webcam, and hit **📷 Test this
camera**. The preview stays up while the camera wakes (some take several seconds)
and tells you honestly whether it works, is blank, or can't be opened. Once it
says ✅, **Save** to pin it.

> **Virtual cameras show grey.** If you have **Camo, OBS, DroidCam** or similar
> installed, one of them is often the Windows *default* — and it hands back a grey
> placeholder unless its phone/source is connected. Anna flags these in the
> dropdown, skips them on "Automatic", and refuses to describe a blank frame. If
> the camera stays grey even for a real webcam, another app is holding it — quit
> Camo/Teams/the Camera app and retry.

Anna also **never opens the Windows Camera app** to "look" — that app seizes the
webcam and would leave her with a blank image. She uses the camera directly. (If
you genuinely want the app, say "open the camera **app**".)

**In a Gemini Live conversation the camera frame goes straight into the session**,
so Anna sees the photo herself and describes it in her own voice — no separate,
slower vision call. It is still **one frame** per look, never a video stream. She
describes people, objects and setting, but will not try to identify anyone.

- **Controlling other apps — Anna clicks real controls, not pixels.** Two
  backends, each for what it's good at:
  - **Native Windows apps** (Notepad, File Explorer, Mail/Outlook, Teams…) via
    **UIA**: she finds the actual button/menu/field by its accessible name and
    type, with a real bounding box. `uiautomation` is the dependency.
  - **Web pages** (Gmail, WhatsApp Web, any site) via **Playwright** over the
    DevTools Protocol: she reads the real DOM and clicks by role/text/selector.
    She **attaches to your already-open, logged-in browser** — start it once
    with a debug port so she can: `chrome.exe --remote-debugging-port=9222`
    (she never opens a separate, logged-out profile, and you don't need
    `playwright install`).
  - **Vision-guessed coordinates are the last resort only** — used when neither
    backend can find the control (custom-drawn UI, some Electron apps with a
    sparse accessibility tree). Any vision guess is flagged *low confidence* and
    **always** asks first, showing a cropped screenshot of exactly what it would
    click.

  **The destructive-target guard lives in the safety validator, not the
  planner.** Any resolved control whose name contains **Send, Submit, Pay,
  Delete, Confirm, Install, Post, Purchase, Transfer, Approve** forces a
  confirmation at high risk — *regardless of what the plan or a cloud model
  claimed the risk was*. A misfiring planner literally cannot click "Send"
  without asking, and clicking one of these needs the strong **"Anna approve"**
  phrase. **Password fields** (detected via UIA's `IsPassword`) are never typed
  into unasked and never read aloud or logged.

- **Multi-step tasks are checkpointed, never an autonomous run.** For a task
  like "draft an email to Rahul", Anna proposes a short step list, then runs it
  **one step at a time**: she re-checks the screen before each step, every step
  still passes the same safety validator, the final Send is always confirmed,
  and she **pauses to check in** after a few steps (`task_max_steps_before_checkin`,
  default 5) rather than barrelling ahead. Any step that wasn't in the plan she
  showed you needs fresh approval before it runs, and if a step fails she stops
  and tells you which one and why — she never ploughs on past a failure.

- **Email, messaging, and other apps.** Say "email rahul@x.com saying I'll be
  late" and Anna opens a **pre-filled draft** — Gmail in your browser, or your
  desktop client (Outlook) via `mailto:` — for you to review. **No API or
  sign-in setup is required.** Nothing is sent by opening a draft; the **send**
  is a separate, always-confirmed step that needs the strong "Anna approve"
  phrase, refuses to go out without a clear recipient, and names every
  recipient when there's more than one. Set `email_provider` (auto/gmail/
  outlook) in config.

  Messaging apps (Teams, WhatsApp, Telegram) follow the same draft → preview →
  confirm → send pattern, and general apps work through the same UIA control
  (11C) — "in Notepad type…", "in Calculator press…". **Payments and money
  transfers are refused outright**, never merely confirmed: Anna will open your
  bank's page and read it, but she will not click Pay/Transfer/Place-order —
  you complete any payment yourself.

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
| Magenta mic ring / "streaming" badge | Normal in streaming mode — it means live mic audio is going to Deepgram. Switch Speech recognition to *local* to keep audio on-device |
| Streaming stopped working | Deepgram failed 3× → the STT circuit routes to local Whisper for 120s, then auto-probes. Anna keeps working on local Whisper meanwhile |
| Mic stays on between replies | Continuous Conversation mode is on. Say "stop listening", tap the mic, or toggle Conversation off |
| Saying "yes" doesn't approve a command | Destructive-tier actions (terminal, delete, move/rename) need the strong phrase **"Anna approve"** — the card is red and says so. Casual words always work for *cancel* |
| Anna doesn't hear my spoken "approve" | She doesn't open the mic by herself while a card is up. Press Ctrl+Alt+Space and say it, click the card, or set `confirmation_voice_listen: true` to auto-listen in hands-free mode |
| "I'm still waiting on your approval for the last one" | Only one confirmation can be pending. Answer or cancel it (or wait 30 s for it to expire) and the new one will be offered |
| "I can't read the text: install Tesseract OCR" | Local OCR isn't installed. `winget install UB-Mannheim.TesseractOCR` then `python -m pip install pytesseract`. Everything stays on this PC. Alternatively enable cloud vision in Settings → Vision |
| "Look at my screen" is slow or reads little text (local-only) | Local OCR of a dense screen (a code editor especially) is intrinsically slow — Anna caps it and downscales, so it degrades to partial text rather than hanging. Turn on cloud vision in Settings → Vision for a fast, full description, or ask about a single window ("analyze this window") which reads cleaner |
| Cyan border / "Screen Vision Active" badge | Watching mode is on (one frame every ~1.5 s, each discarded). Say "stop looking" or "privacy mode". It also stops itself when idle |
| Red "Camera on" badge stays up | It shouldn't — the camera is stopped in a `finally` block after a single frame. If it sticks, say "privacy mode" and file an issue with the Developer Tools log |
| "the camera didn't respond" | The app window needs camera permission in Windows (Settings → Privacy → Camera → allow desktop apps) |
| Camera shows a **grey/blank picture** | A virtual camera (Camo, OBS, DroidCam) is being used and its phone/source isn't connected — these are often the Windows *default*. Settings → Vision → Camera → pick a real webcam → **Test** → Save. If a real webcam is also grey, another app is holding it: quit Camo / Teams / the Camera app |
| Settings shows "Camera 1" instead of real names | Browsers hide camera names until permission has been granted once. Open Settings again (Anna unlocks the names on the way in), or run one camera Test first |
| The **Windows Camera app** opens when I ask Anna to look | It shouldn't — that app seizes the webcam and leaves Anna blank, so she refuses to launch it. If it still opens, file an issue with the log. To open it deliberately, say "open the camera **app**" |
| Anna won't send a screen: "couldn't read it well enough to check it" | The local sensitive-content scan (OCR) failed, so she won't ship an unchecked screen to the cloud. Retry, or say "look at my screen anyway" and approve with the strong phrase |
| Anna refuses to look at a screen | She spotted something that looks like a password, API key or banking detail and won't analyze it. Say "look at my screen anyway" and approve with the strong phrase |
| Cloud vision says a model is unavailable | Preview models churn. Anna retries a fallback model automatically and, failing that, degrades to local OCR. Set a different `vision_cloud_model` in Settings → Vision |
| "I can't reach your browser on localhost:9222" | Playwright attaches to your real browser over CDP. Start it once with a debug port: close Chrome, then `chrome.exe --remote-debugging-port=9222`. Native-app control (UIA) needs nothing extra |
| Anna clicked the wrong thing / says "only 72% sure" | She couldn't find the control through UIA or the browser DOM and fell back to guessing from pixels. That always asks first and shows a crop — check it before approving. Better accessibility (native apps, real web pages) avoids the guess entirely |
| "that's a destructive control — needs your OK" on a normal button | The button's name contains Send/Submit/Pay/Delete/Confirm/Install/Post/Purchase/Transfer/Approve. That's deliberate and enforced in the validator. Edit `destructive_targets` in config if a specific app misuses one of these words |
| Electron app controls aren't found | Some Electron apps expose a sparse accessibility tree to UIA. Anna falls back to the vision guess (which asks first). Where the app has a web version, the browser backend works better |
| `pip` fails with "Fatal error in launcher" | This venv's `pip.exe` has a stale baked-in path. Use `python -m pip install ...` instead |
| "Engine: Pipeline (Live offline)" chip | Gemini Live is selected but unreachable (no key / no consent / offline / circuit open after 3 failures). Anna keeps working on the pipeline; Live auto-probes after 120s |
| Purple mic ring / "Live — audio streaming to Google" badge | Normal in Live mode — continuous mic audio is going to Google and the session is metered. Tap the mic to end it; idle sessions auto-close |
| Live session ends by itself mid-conversation | Either the ~15-min session cap hit without a resumption handle (rare; it normally resumes invisibly), the 20s stall watchdog fired, or the 60s idle auto-close — all fall back cleanly. See Developer Tools for which |
| Live costs more than expected | Settings → Conversation engine shows the month-to-date estimate and editable per-minute prices; set a monthly soft cap to get warned. The estimate is local — Google's billing console is the source of truth |

## Windows installer

For a source install with `.venv`, a launcher and a desktop shortcut:

```powershell
.\installer\Install-Anastasia.ps1
```

For a packaged build plus an optional Inno Setup installer:

```powershell
.\installer\Build-Installer.ps1 -InstallBuildTools -Version 0.1.0
```

Details: [INSTALLER.md](INSTALLER.md).
## Development

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q   # 456 tests
python app\main.py --debug                      # DevTools console
```

Architecture, event protocol and safety invariants: [ARCHITECTURE.md](ARCHITECTURE.md).
