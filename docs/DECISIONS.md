# Technology decisions

Per `00_AGENT_PROTOCOL.md` §3: options considered → criteria → choice → why →
revisit-when. Benchmarked/verified against the real target machine
(i5-8250U, 4C/8T, 15W, 20GB RAM, Windows 11, Python 3.13, `.venv`).

---

## D-0.1 — IPC transport (core ⇄ UI)

**The killer criterion is not performance — it is that the existing JS frontend
must be able to connect.** `app/web/app.js` runs inside WebView2. A browser
context can open a WebSocket natively; it *cannot* open a named pipe, and gRPC
from a browser needs a proxy. That eliminates two of the four candidates before
any benchmark.

| Option | Version | Deps | Browser can connect? | Verdict |
|---|---|---|---|---|
| **`websockets`** | 16.1 (**16.0 already installed** — transitive) | **none new** | ✅ native | **CHOSEN** |
| `aiohttp` | 3.14.1 | new (~1MB, full HTTP server) | ✅ native | Rejected — heavier, adds a dep for a capability we don't need yet |
| Named pipes (`pywin32`) | — | pywin32 | ❌ **no** | Rejected — the frontend physically cannot speak it |
| gRPC | — | grpcio + protoc codegen | ❌ needs grpc-web proxy | Rejected — heavy, codegen, overkill for localhost JSON |

**Choice: `websockets`.** Asyncio-native (core is asyncio-first per the phase
spec), zero new dependency, and it is the only lightweight option the existing
frontend can talk to unchanged.

**Revisit when:** core also needs to serve HTTP assets (then `aiohttp` earns its
weight). Not now — the UI process loads its own local files.

---

## D-0.2 — Tray icon

| Option | Version | Deps | Windows health | Verdict |
|---|---|---|---|---|
| **`pystray`** | 0.19.5 | new (small, pure-Python + pywin32 on Win) | Maintained; official pywebview example exists | **CHOSEN** |
| `pywin32` `Shell_NotifyIcon` | — | pywin32 | Works, but hand-rolled window proc + message pump | Rejected — reinventing pystray, more code to get wrong |
| `infi.systray` | — | new | Effectively unmaintained | Rejected |

**Known Windows issue — and why the split dissolves it:** pystray's `run()` is
blocking and wants the main thread; so does pywebview. Running both **in one
process crashes on Windows**
([pywebview#720](https://github.com/r0x0r/pywebview/issues/720)); the documented
workaround is `run_detached()` before entering pywebview's loop.

In Phase 0 the tray is **core-owned**, and **core has no pywebview at all**. The
two main-thread hogs end up in different processes, so the conflict is
**eliminated by construction** rather than worked around. Core is asyncio: run
pystray via `run_detached()` / its own thread and marshal menu clicks onto the
core loop with `loop.call_soon_threadsafe`.

**Revisit when:** never move the tray into the UI process — that re-creates the
crash.

---

## D-0.3 — Supervision / auto-start

**A Windows Service is RULED OUT on evidence, not preference.**

Services run in **Session 0**, which is isolated from the user's interactive
desktop. Cross-session UI and kernel-object sharing requires explicit
global-namespace qualification and `CreateProcessAsUser` gymnastics, and
hardware/UI frameworks expect the user's session context
([Microsoft: Session 0 isolation](https://techcommunity.microsoft.com/blog/askperf/application-compatibility---session-0-isolation/372361),
[MS Q&A](https://learn.microsoft.com/en-us/answers/questions/27517/is-there-any-workaround-in-win10-to-allow-service),
[Ionescu, Inside Session 0 Isolation](https://www.alex-ionescu.com/inside-session-0-isolation-and-the-ui-detection-service-part-1/)).

Anna's core owns **UIA (`uiautomation`), `pyautogui`, screen capture, and the
microphone** — every one of them a user-session resource. A service would break
all four. This is the phase file's own suspicion, now confirmed.

| Option | Runs in user session? | Restart-on-crash | Deps | Verdict |
|---|---|---|---|---|
| **Task Scheduler, ONLOGON** | ✅ yes | ✅ built-in (`-RestartCount`/`-RestartInterval`) | none (`schtasks`) | **CHOSEN** |
| Windows Service (pywin32/nssm) | ❌ **session 0** | ✅ | pywin32/nssm | **RULED OUT** — no mic, no UIA, no desktop |
| Startup shortcut + watchdog process | ✅ yes | ⚠️ hand-rolled | none | Rejected — re-implements what Task Scheduler gives free |

**Choice: Task Scheduler ONLOGON**, restart 3× at 1-minute intervals, logging to
Event Viewer. Installed by the installer, removed on uninstall.

⚠️ **This modifies the user's machine and therefore needs explicit consent** —
it must be opt-in in the installer, never silent (Protocol §4 spirit).

**Revisit when:** never for pre-login start — Anna needs a logged-in user session
by definition (mic, desktop, UIA).

---

## D-0.4 — Event log store

| Option | Deps | Verdict |
|---|---|---|
| **stdlib `sqlite3` + WAL** | **none** (3.49.1 present) | **CHOSEN** |
| peewee / SQLAlchemy | new ORM | Rejected — an ORM for one 7-column append-only table is pure overhead |

**Choice: stdlib `sqlite3`, WAL mode, one writer task fed by an `asyncio.Queue`.**

Precedent in-repo: `app/agent/history.py` already uses SQLite + WAL +
`busy_timeout=30000`. Phase 9.1A fixed a real crash there by moving to per-call
connections. Phase 0 asks for something *stricter* — a **single writer** — which
removes the concurrency problem at the root rather than tolerating it. Reads
(dump/inspect) may use their own short-lived connections.

**Revisit when:** the log exceeds ~1GB or needs full-text search (then FTS5 —
still stdlib).

---

## D-0.5 — Answering a confirmation when there is no window

**This is a deliberate design decision, not a bug fix.** It is the one place
Phase 0 changes Anna's *behaviour*, and it changes when she opens the mic — so
it is recorded here and in the README privacy section, not slipped in.

**The problem.** With the window closed, the only answer channel for a
confirmation card is voice. But `confirmation_voice_listen` defaults to `False`
(`config.py`) — Anna deliberately does **not** reopen the mic to hear "approve",
because a card appearing should never silently start listening. Windowless, that
leaves a card sitting unanswerable until it expires. M0.2 step 3 requires the
opposite: *"She asks by voice, and 'Anna approve' works."*

| Option | Verdict |
|---|---|
| Leave it off | Rejected — a headless Anna could never be told "yes". Breaks the point of the phase. |
| Turn it on globally | **Rejected** — with a window open, clicking works fine; auto-opening the mic on every card is a privacy regression for zero benefit. |
| **On only when no UI client is attached** | **CHOSEN** |

**Chosen behaviour** (implemented in half B):

1. Only when **zero UI clients are connected** to core. Attach a window and it
   reverts to click-or-push-to-talk.
2. She **asks the question aloud first** — the mic does not open until she has
   finished speaking the card (half-duplex already guarantees she can't hear
   herself).
3. An **audible cue** marks the mic opening, so an open mic is never silent or
   ambiguous — the windowless equivalent of the on-screen mic ring.
4. A **short listen window** (one answer, then closed) — not an open-ended
   session. Expiry still auto-cancels as always.
5. The strong-phrase tier is **unchanged**: destructive actions still demand
   "Anna approve". A windowless Anna gets no easier to persuade.

**Why this is safe:** the mic opens only in direct response to a card *Anna
herself raised*, only when nobody can click, only after she has said why, only
with an audible cue, and only for one short window. It cannot be reached without
a pending confirmation, which cannot exist without the validator having demanded
one.

**Revisit when:** never widen the trigger. If a future phase wants ambient
listening, that is a new consent card, not an extension of this.

---

## D-0.6 — The camera stays in the window

The camera is opened by the **WebView's `getUserMedia`** (`BrowserCameraStream`),
not by core. A headless daemon has no browser and therefore **cannot open the
camera at all**.

| Option | Verdict |
|---|---|
| **(a) Camera requires the window** | **CHOSEN** |
| (b) Move it to core via OpenCV | Rejected — a ~60MB dependency, and we would lose the browser self-preview *and* the OS camera indicator, then have to rebuild the virtual-camera / grey-frame handling that Phase 11 just got working. |
| (c) Hybrid (browser when attached, OpenCV when not) | Rejected — two code paths and two sets of bugs, for a case (a headless selfie) nobody asked for. |

**Chosen behaviour:** with the window open, the camera works exactly as it does
today — self-preview, red badge, one frame, Mira consent pattern intact.
Windowless, Anna **offers to open the window** ("I need my window for the camera
— shall I open it?") and does so on approval. If the user declines she says so
plainly. **She never pretends to have looked.**

The offer is cheap because core can already raise the UI (the tray owns
Open Anna); it is a prompt, not a new capability.

**Revisit when:** a future phase genuinely needs headless vision (e.g. a security
watcher). That is a new consent card and a new dependency decision, not a quiet
extension of this one.

---

## D-1.0 — Default hybrid-brain model (Groq) after the 70B deprecation

**Groq deprecated `llama-3.3-70b-versatile` (and `llama-3.1-8b-instant`) on
2026-06-17** for free/developer tiers
([Groq deprecations](https://console.groq.com/docs/deprecations)). Still served
during a transition window, but a shipped default on a deprecated model is a
time-bomb for the hybrid brain — verified before it bit us mid-Phase-1.

| Option | Tier | Notes | Verdict |
|---|---|---|---|
| **`openai/gpt-oss-120b`** | large (70B-class successor) | Groq's own recommended migration for the 70B; strong instruction-following | **CHOSEN default** |
| `qwen/qwen3.6-27b` | mid | lighter/faster; a non-reasoning instruct model — a fallback if gpt-oss's reasoning format hurts planning-JSON reliability | Recorded alternative |
| Keep `llama-3.3-70b-versatile` | — | deprecated; will 404 when retired | Rejected |

**Choice: default `cloud_model = openai/gpt-oss-120b`,** applied via
`_DEFAULT_MIGRATIONS` so users on the old default move forward while any custom
choice is preserved. The field is config-driven (Settings → Cloud brain), so no
code path hardcodes a model.

**Watch (brain-swap risk):** gpt-oss is a *reasoning*-family model; if Groq
surfaces analysis/reasoning text inline it could bleed into chat replies or
break the planner's JSON extraction (`strip_thinking`/intent parsing). A human
sanity pass (chat turn · command plan · multi-step proposal) gates this before
building on it.

**Revisit when:** the sanity pass shows planning-JSON regressions (→ switch
default to `qwen/qwen3.6-27b` or adjust parsing), or Groq's small/large lineup
changes again. The Phase-1 cloud-triage model is a *separate* config key
(default `openai/gpt-oss-20b`), decided in 1B.

---

## D-1.1 — System metrics: pure ctypes (not psutil)

The Phase-1 plan proposed `psutil` for disk/RAM/battery. But psutil isn't
installed (it's a NEW dependency), it ships a C extension that needs a
PyInstaller hook, and the session-lock spike already proved direct Win32 ctypes
calls work cleanly here. Only three simple metrics are needed.

| Option | Deps | Windows | Verdict |
|---|---|---|---|
| **ctypes Win32** (`GetDiskFreeSpaceExW`, `GlobalMemoryStatusEx`, `GetSystemPowerStatus`) | **none** | native | **CHOSEN** — zero-dep, no packaging impact, matches the local-first identity and the ctypes precedent |
| `psutil` | new (+C ext) | good, cross-platform | Rejected for 3 metrics — install weight + a packaging hook, for no gain here |
| WMI (`wmi`/comtypes) | new, heavy | slow | Rejected |

**Choice: pure ctypes.** ~30 lines total, verified on the target laptop (disk
11%, RAM 66%, battery 100/plugged). **CPU temperature has no cheap documented
Windows API** (WMI thermal zones are flaky/often unavailable), so temp is
**absent by design** — the per-metric probe degrades it to absent-and-doctor-
noted, never a recurring error (Phase-1 rider 1).

**Revisit when:** a watcher needs a metric ctypes can't reach cheaply, or Anna
ever targets Linux/macOS (then psutil's cross-platform value would justify the
dependency).

---

## D-1.2 — Filesystem watching: `watchdog` (accepted, conditionally)

Pre-approved conditionally: accept `watchdog` only if it is healthily maintained
on Windows AND bundles cleanly into a frozen build — checked NOW, not at
packaging time.

| Option | Deps | Windows | Verdict |
|---|---|---|---|
| **`watchdog` 6.0.0** | new (79 KB wheel, pure-Python + ctypes) | native `ReadDirectoryChangesW` (`WindowsApiObserver`) | **CHOSEN** |
| Polling (`os.scandir` every N s) | none | works | Fallback if watchdog failed the conditions — it didn't |

**Maintenance (verified):** watchdog 6.0.0, actively maintained (Python 3.12+,
FileSystemEvent dataclasses). The Windows observer started+stopped cleanly on
the target machine.

**Frozen bundling (verified NOW):** watchdog selects its observer dynamically, so
PyInstaller's static analysis misses `watchdog.observers.read_directory_changes`;
there is **no** contrib hook. But `collect_submodules("watchdog")` **does**
capture it (confirmed) — the same one-line spec fix already used for websockets/
pystray. Added to `packaging/anastasia.spec` (collect + explicit hidden imports)
so the packaged build is ready.

**Choice: watchdog**, imported lazily so a box without it benches only the
filesystem watcher, never core. Disk/RAM/battery/window stay pure-ctypes (D-1.1)
— watchdog earns its keep only for the event-driven directory watch, which
polling can't do cheaply.

**Revisit when:** watchdog's Windows backend regresses, or a watched root is a
network/OneDrive path where ReadDirectoryChangesW is known-flaky (then poll that
root specifically).

## D-1.3 — Salience rules format: TOML via stdlib `tomllib`

The local salience table (event kind → score) is a config file the user
**hand-annotates live during the soak**, so the format had hard requirements:
comments (for the annotations), read-only parsing (a live-edit typo must not
execute), and no new dependency. The user set the tie-break: TOML if our real
Python floor is 3.11+, YAML-with-a-frozen-bundling-check if we genuinely need
3.10; explicitly **not** JSON (no comments) and **not** a `.py` table
(executable config; a typo would crash hot-reload).

**Python floor — confirmed 3.11+.** The repo pins no `python_requires`; the
"3.10+" in the README was aspirational text, and the only environment this has
ever run/tested in is the 3.13.5 venv. Nothing depends on 3.10. So the floor is
raised to **3.11** (README line 22 updated in this commit), which makes
`tomllib` — **stdlib since 3.11** — available with zero dependency.

| Option | Deps | Comments | Executable? | Verdict |
|---|---|---|---|---|
| **TOML / `tomllib`** | none (stdlib 3.11+) | yes | no (read-only parse) | **CHOSEN** |
| YAML / `pyyaml` | new dep + frozen-bundle hook | yes | no | Only if floor were 3.10 — it isn't |
| JSON | none | **no** | no | Rejected — user annotates the file |
| `.py` table | none | yes | **yes** | Rejected — a live-edit typo crashes hot-reload |

**Fail-safe hot-reload (rider 2):** a malformed edit **keeps the last-good
table**, sets a doctor-visible error, and never crashes or zeroes. On a
malformed *first* load (no last-good yet) it falls back to the embedded
`DEFAULT_RULES_TOML` so scores never silently collapse to the default. mtime is
stamped on the bad read too, so the same broken file isn't re-reported on every
event.

**Provenance (rider 1):** `score()` returns `(score, rule)`, stamped onto the
event as `payload["score"]`/`payload["rule"]`, so the feed shows *which* rule
fired, not a bare number — calibration needs the reason.

**Unknown kinds (rider 3):** an event no rule covers scores `2` with
rule `"default"` — log-only, fail-quiet; a new watcher kind is never dropped and
never shouted about.

**Trust ratchet (unchanged):** these are local rules only. In 1B, cloud triage
may *score*, but only a local rule can push an event to speak-tier. The embedded
`DEFAULT_RULES_TOML` is the source of truth; the seeded on-disk copy is
gitignored (it's user-edited, per-install).

**Revisit when:** the floor ever needs to drop to 3.10 (then YAML + bundling
hook), or scores need to be conditional on payload values rather than a flat
kind→score table (then the parser grows, not the format).
