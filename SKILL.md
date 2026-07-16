# Anna's Skills — command reference

What you can say (or type) and what happens. Wake-word prefixes ("Anna,
hey Anna…"), polite fillers ("please", "for me") and punctuation are cleaned
automatically. Push-to-talk: **Ctrl + Alt + Space** (press again to stop —
or just stop talking; silence ends the recording).

## Instant commands (never use the LLM, < 1 second)

| Say / type | Action |
|---|---|
| open notepad / paint / ms paint / chrome / edge / vs code / calculator / terminal / powershell / file explorer / teams | Launches the app |
| open downloads / documents / desktop / pictures / projects / my project folder | Opens the folder in Explorer (safe folders only) |
| take a screenshot · screenshot | Saves a PNG to Pictures/AnnaScreenshots, shows a View card |
| copy · paste · select all · save · show desktop · switch window | Presses the matching hotkey |
| read clipboard · what's on my clipboard | Shows clipboard text |
| summarize clipboard | Summarizes clipboard text (uses the local model) |
| type &lt;anything&gt; | Types it into the focused window (casing preserved) |
| search google for &lt;query&gt; · google &lt;query&gt; | Web search |
| search youtube for &lt;query&gt; · open youtube / google / github / gmail | Opens the site/search |
| open website &lt;domain&gt; | Opens the URL |
| search downloads for invoice · find resume in documents | File-name search inside safe folders |

## Natural language (local model)

Conversation uses a small plain-text personality prompt; commands use the
full structured planner. Examples: "how are you feeling today", "put a short
hello note on my clipboard", "run git status in my project". A chat response
that detects a computer action hands back to command mode. The model never
executes anything itself.

## Actions that ask for approval first

Terminal commands, window close/minimize/maximize, very long text typing.
An amber card appears — **Run it** or **Cancel** (auto-cancels after 30 s).
Dangerous terminal patterns (recursive delete, shutdown, registry edits,
encoded PowerShell, …) are refused even if you approve.

**Sending** anything — an email, a message, or clicking any Send/Submit/Post
button — needs the strong phrase **"Anna approve"**, not just a tap. The
recipient is checked first, and the click lands on the real button via the
resolved-target path, never a guess.

## Refused entirely (by design)

Payments and money movement (Pay / transfer / place order · refused outright,
never merely confirmed) · deleting files · shutdown/restart ·
passwords/credentials · installing software · killing processes ·
arbitrary code execution.

## Voice behavior

- **Half-duplex:** Anna never hears herself; the mic ignores everything
  while she speaks (plus a 0.4 s echo tail).
- **Barge-in:** press push-to-talk while she's talking to cut her off and
  speak immediately.
- **Wake word** (optional, off by default): flip the toggle and say
  **"Hey Anna"** or **"Anastasia"** — a local-Whisper name spotter (no training,
  no extra install). Say the full "Hey Anna" (a bare "Anna" is too short to
  recognize reliably); expect a ~1–3 s delay since it's local STT. The classic
  "Hey Jarvis" model is still available via `wake_word_backend: "openwakeword"`.
  Push-to-talk stays the primary path.
- Whisper confidence—not wording—decides whether audio needs a retry. Strong
  fuzzy corrections run directly; uncertain ones show a neutral Yes/No card.
  Typing always wins over an in-flight recording.

## Where things live

- Screenshots: `~/Pictures/AnnaScreenshots`
- Safe folders (Explorer/file-search scope): Desktop, Downloads, Documents,
  Pictures, Projects — editable in `app/data/config.json`
- History: sidebar → Conversations, or the History button
- Technical logs: sidebar → Developer tools

<!-- BEGIN GENERATED: tool-registry -->

<!-- Generated from app/tools by `python -m app.tools.gen_skill`.
     Do not edit this region by hand — edit the @tool declarations. -->

## Tool inventory (generated)

Every tool Anna can run, straight from the registry. **Tier is a floor** — the safety validator computes the real risk at runtime and can only *raise* it above this, never lower it. "Cloud-visible" is whether a cloud model may even be told the tool exists.

| Tool | Tier (floor) | Offline | Cloud-visible | What it does |
|---|---|---|---|---|
| `active_window_capture` | runs freely | yes | yes | Capture and describe just the active window. |
| `browser_find_and_click` | runs freely | yes | yes | Click an element in the attached browser page. SAFE floor; the validator raises on a destructive or guessed target. |
| `browser_get_visible_links` | runs freely | yes | yes | List the visible links on the attached browser page. |
| `browser_navigate` | runs freely | yes | yes | Navigate the attached browser (Playwright over CDP) to a URL. |
| `browser_open` | runs freely | yes | yes | Open a URL or a web search in the default browser. |
| `browser_read_page_text` | runs freely | yes | yes | Read the visible text of the attached browser page. |
| `browser_type_into` | runs freely | yes | yes | Type into a field in the attached browser page (never a password field). SAFE floor; the validator raises on a destructive or guessed target. |
| `camera_look` | runs freely | yes | yes | Open the camera, take ONE frame, describe it, stop the camera. Requires the window; sensitive content needs a separate OK. |
| `click_control` | runs freely | yes | yes | Click a resolved native control. Declared SAFE, but the validator RAISES to a strong-phrase confirmation whenever the resolved target is destructive (Send/Submit/Pay/…) or a vision guess. |
| `clipboard_read` | runs freely | yes | yes | Read the current clipboard text. |
| `clipboard_write` | runs freely | yes | yes | Put text on the clipboard. |
| `compose_email` | runs freely | yes | yes | Open a pre-filled email DRAFT (Gmail in the browser or Outlook). Shows a preview and sends nothing. |
| `delete_files` | asks first | yes | no | Deletion is disabled in this build; the tool always declines. Never named to a cloud model (NEVER_DECLARE). |
| `find_control` | runs freely | yes | yes | Read-only: locate an on-screen control (UIA/vision) and report what was found. |
| `look_at_screen` | runs freely | yes | yes | Capture one frame of the whole desktop and describe it. Sensitive-looking screens need a separate explicit OK. |
| `open_app` | runs freely | yes | yes | Open one of the user's registered apps by name. |
| `open_folder` | runs freely | yes | yes | Open one of the user's safe folders in Explorer. |
| `press_hotkey` | runs freely | yes | yes | Press one hotkey from a fixed allow-list (e.g. ctrl+c, alt+tab). |
| `privacy_mode` | runs freely | yes | yes | Kill switch: stop screen watching, the camera, and any Live audio session. |
| `read_window_text` | runs freely | yes | yes | Read the visible text of a window. Password fields are never read. |
| `region_capture` | runs freely | yes | yes | Capture and describe a region around the cursor (or a named region). |
| `run_terminal` | asks first | yes | yes | Run a PowerShell command. Dangerous patterns are blocked outright; every run requires explicit confirmation. |
| `screen_capture` | runs freely | yes | yes | Capture one frame of the whole desktop and describe it. |
| `search_files` | runs freely | yes | yes | Search the user's safe folders for files whose name matches a query. |
| `send_email` | asks first | no | yes | Send the open draft by clicking the real Send button. Needs a clear recipient and confirmation; the Send click is itself a destructive target, so the strong phrase is demanded. |
| `start_screen_watch` | runs freely | yes | yes | Start watching the screen — one frame every interval — until told to stop. |
| `stop_screen_watch` | runs freely | yes | yes | Stop watching the screen. |
| `summarize_clipboard` | runs freely | yes | yes | Summarize the clipboard text with the local model (reaches the cloud only with the clipboard opt-in). |
| `take_screenshot` | runs freely | yes | yes | Save a screenshot of the desktop (or one monitor) to disk. |
| `type_into_control` | runs freely | yes | yes | Type into a resolved native field (never into a password field). SAFE floor; the validator raises on a destructive or guessed target. |
| `type_text` | runs freely | yes | yes | Type text into the active window (long text escalates to confirmation). |
| `window_control` | asks first | yes | yes | Close, minimize or maximize a named app window (needs confirmation; never closes Anna's own window). |

<!-- END GENERATED: tool-registry -->
