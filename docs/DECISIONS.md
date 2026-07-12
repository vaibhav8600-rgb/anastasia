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
