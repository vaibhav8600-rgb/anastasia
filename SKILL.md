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

## Refused entirely (by design)

Deleting files · shutdown/restart · sending email/messages · payments ·
passwords/credentials · installing software · killing processes ·
arbitrary code execution.

## Voice behavior

- **Half-duplex:** Anna never hears herself; the mic ignores everything
  while she speaks (plus a 0.4 s echo tail).
- **Barge-in:** press push-to-talk while she's talking to cut her off and
  speak immediately.
- **Wake word** (optional, off by default): needs `pip install openwakeword`;
  uses the pre-trained "Hey Jarvis" model until a custom "Hey Anna" model is
  trained. Push-to-talk stays the primary path.
- Whisper confidence—not wording—decides whether audio needs a retry. Strong
  fuzzy corrections run directly; uncertain ones show a neutral Yes/No card.
  Typing always wins over an in-flight recording.

## Where things live

- Screenshots: `~/Pictures/AnnaScreenshots`
- Safe folders (Explorer/file-search scope): Desktop, Downloads, Documents,
  Pictures, Projects — editable in `app/data/config.json`
- History: sidebar → Conversations, or the History button
- Technical logs: sidebar → Developer tools
