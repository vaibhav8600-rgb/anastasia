# PLAN — Phase 0: anna-core daemon + anna-ui client

Status: **awaiting human acknowledgment** (Protocol §5.3, §9).
Baseline: **437 tests green** (`437 passed in 61s`). Docs claiming "126 tests"
are stale — see §7.

---

## PART A — PRE-FLIGHT INVESTIGATION (the 7 questions, answered from the code)

### Q1 · The existing event protocol — can it be promoted to a process boundary?

**Mostly yes, but it is not purely one-way.** `UIBridge.dispatch()` builds
`{"type", "payload"}` and already `json.dumps`-es it into `evaluate_js`
(`app/web/bridge.py:41-55`) — so the payloads are **already JSON-serializable by
construction**. 30 event types are emitted:

```
state_change · status · user_message · anna_message · action_result ·
confirm_request · confirm_resolved · confirm_details · setup_card · devlog ·
latency · turn_latency · history · settings · prefs · test_result · toggle_sync ·
clear_conversation · full_state · mic · hands_free · speaking_level ·
stt_streaming · stt_interim · live_streaming · live_cost · live_consent ·
privacy_mode · camera_capture · camera_stop
```

**Three things break a naive one-way promotion:**

1. **Blocking Python→JS RPC.** `camera_capture` is a *request*: core dispatches
   it with a `request_id`, then **blocks a worker thread** on a
   `threading.Event` until JS calls back `camera_frame(request_id, data_url)`
   (`main.py:820-852`). It already carries a correlation id — good — but it is
   an RPC, not an event.
2. **Synchronous JS→Python returns.** `pick_voice_file() -> str`,
   `get_brain_info() -> dict`, `get_history() -> list` return values through
   pywebview's JsApi promise. Over WS these become request/response with
   correlation ids + timeouts.
3. `ui.after(0, fn)` **is a no-op** — it just calls `fn()` because
   `evaluate_js` is thread-safe (`bridge.py:71-72`). So there is **no GUI-thread
   marshalling to preserve**. One less thing to port.

**Verdict:** promote to `{v:1, id, ts, type, payload}` with two channels — `event`
(push, fire-and-forget) and `req`/`res` (correlated, with timeout). No shared
memory is assumed anywhere.

### Q2 · What is UI-thread-bound / reaches into the UI directly?

**The split boundary already exists.** `Controller` talks to a *duck-typed* `ui`
object (`UIBridge` in production, `FakeMainUI` in tests) and
`Controller(ui=..., autostart=False)` already runs fully headless — that is how
437 tests run today. This is the single biggest de-risker in the phase.

Only **five** places reach past that abstraction into the real webview:

| Where | What | Post-split home |
|---|---|---|
| `main.py:1697-1718` | `webview.create_window` / `webview.start` | **anna-ui** |
| `main.py:1274-1280` | `_save_dialog` → `webview.windows[0].create_file_dialog(SAVE_DIALOG)` | **anna-ui** (RPC: core asks UI to pick a path) |
| `main.py:1527-1529` | `pick_voice_file` → `create_file_dialog(OPEN_DIALOG)` | **anna-ui** (same) |
| `main.py:1679` | `_webview2_error_box` | **anna-ui** |
| `main.py:820-852` | **camera capture via browser `getUserMedia`** | ⚠️ **see Q4 — this is the blocker** |

### Q3 · Confirmation cards — do they survive with no UI?

**The state machine is already core-side.** `ConfirmationManager`
(`app/agent/confirmation_manager.py:142`) owns pending state, the 30s expiry
timer, the one-pending-only rule, and the strong-phrase tier. The pipeline owns
it (`pipeline.py:86`). The UI only *renders* the card.

- **Create:** `pipeline._set_pending()` → `ui.ask_confirmation()` (display only).
- **Answer (click):** `JsApi.confirm(action_id, approved)` → `approve/cancel_pending`.
- **Answer (voice):** `pipeline.handle_confirmation_utterance()` (PTT/typed) and
  `LiveEngine._resolve_confirmation_by_voice()` (Live).
- **Timeout:** the manager's own `threading.Timer` — independent of any UI.

So requirement #4 ("cards live in core") is **already satisfied**. ✅

⚠️ **But there is one real gap.** With no window, the *only* answer channel is
voice — and `confirmation_voice_listen` **defaults to `False`**
(`config.py:201`), meaning Anna deliberately does **not** reopen the mic to hear
"approve". Windowless, a card would sit there until it expires unless the user
hits push-to-talk. **M0.2 step 3 explicitly requires voice answering to work with
the window closed.** Fix: when no UI client is attached, core must auto-enable the
listen-for-confirmation path (and say the card aloud, which it already does).

### Q4 · Where does the hardware live? ⚠️ **THE ONE ARCHITECTURAL CONFLICT**

| Device | Owner today | Post-split |
|---|---|---|
| **Microphone** | `sounddevice.InputStream` in `app/voice/recorder.py:296-320` — pure Python | ✅ **core, unchanged** |
| **Speakers** | winsound / SAPI / `sounddevice` (`speech_output.py`, `gemini_live.py:478`) | ✅ **core, unchanged** |
| **Screen** | `PIL.ImageGrab` (`app/vision/screen.py`) | ✅ **core, unchanged** |
| **Camera** | ❌ **the WebView's `getUserMedia`** — `BrowserCameraStream` (`camera.py:69`), wired by `main.py:93` | 🔴 **UI-side. A headless core cannot open the camera at all.** |

Audio belongs to core exactly as the phase demands. **The camera does not.** We
chose the browser path deliberately in Phase 11: it gives the self-preview, the
OS camera indicator, and needs no OpenCV dependency. `OpenCVCameraStream` exists
as an unused alternative (`camera.py`), but **opencv is not installed**.

**This needs your decision — see §DECISIONS NEEDED (1).**

### Q5 · `--doctor`

`app/doctor.py:run_doctor()` runs **in-process** checks (Ollama, mic, config,
paths). Post-split it must additionally answer *"is the daemon alive?"*: probe
the core's WS port, present the auth token, and report PID/uptime/tray/event-log
health — while still working (and saying so plainly) when core is **not** running.

### Q6 · Settings

`AppConfig` (pydantic) ← `app/data/config.json`; the only writer is
`Controller.save_settings` behind a field whitelist (`_SETTINGS_FIELDS` +
`_SETTINGS_CHOICES`).

Post-split: **core is the sole owner and sole writer.** The UI never touches the
file — it requests `settings`, sends `save_settings`, and core validates,
persists, hot-applies, and **broadcasts the new view to every connected client**.
This prevents two processes racing the same JSON file.

### Q7 · What do the tests import?

437 tests. `tests/fakes` is used by 38 files; **17 files import `app.main`**.

Because `Controller(ui=FakeMainUI(), autostart=False)` **already runs headless
in-process**, the existing suite *is* the core-in-process harness — no rewrite
needed. Plan: keep `Controller` as core's brain, and add a `WsBridge` that
implements the *same duck-typed `ui` surface* as `UIBridge`. New tests then cover
only the new IPC/protocol/event-log layer.

---

## PART B — SCOPE, RISK, ROLLBACK

### Blast radius (Protocol §9 — this exceeds the stop threshold)

- `app/` contains **64 Python files**; **17 test files import `app.main`**.
- Phase 0 rewrites the **process model** — a working subsystem.
- It touches **§4 paths**: the validator's trigger path, mic/camera ownership,
  Gemini Live billing on quit, and a new persistent log that must contain no
  secrets.

**Both §9 stop conditions fire** (">15 files" and "rewriting a working
subsystem"), and §5.3 requires plan confirmation whenever §4 is touched. Hence
this document, and hence the stop.

### Proposed commit sequence (small, one concern each)

| # | Commit | Files | Risk |
|---|---|---|---|
| 1 | Event log: `app/core/eventlog.py` (sqlite3+WAL, single writer, **field allowlist per event type**) + tests | new | low |
| 2 | Tool registry formalization (`{name, schema, permission_tier, offline_ok, description}`) + SKILL.md generator | `app/tools/__init__.py` | low |
| 3 | Protocol: `{v,id,ts,type,payload}` envelope, event + req/res channels, schema in ARCHITECTURE.md | new | low |
| 4 | `anna-core`: asyncio process, WS server bound **127.0.0.1 only**, token auth, hosts the existing `Controller` | new + `main.py` | **high** |
| 5 | `WsBridge` (implements the existing duck-typed `ui` surface) + `anna-ui` thin client; `app.js` swaps `pywebview.api` → WS | `bridge.py`, `app.js` | **high** |
| 6 | Tray (pystray, core-owned): Open / Pause listening / Quit-with-graceful-teardown | new | med |
| 7 | Supervision: Task Scheduler ONLOGON (opt-in), `--doctor` daemon health | installer, `doctor.py` | med |
| 8 | `--legacy` single-process flag (escape hatch) + docs | `main.py` | low |

### Top risks

1. **Camera breaks headless** (Q4) — unresolved, see below.
2. **Windowless confirmations unanswerable** (Q3) — must auto-enable voice listen.
3. **Quit must not leak a metered Live session** — `Controller.shutdown()` already
   closes Live/mic/camera/browser-driver; the tray Quit **must** route through it.
4. **Event log leaking secrets** — mitigate by *construction*: allowlist the
   payload fields per event type; never `json.dumps` a whole object. Test by
   injecting a fake key and asserting absence.
5. **Two processes racing `config.json`** — mitigated by core-sole-writer (Q6).
6. **Latency regression** from the extra hop — the WS hop is localhost JSON
   (sub-ms) and the *core* pipeline is unchanged, but this must be measured
   (§7 budget: within ±10%).

### Rollback

Every commit is independent and revertible. Commit 8 (`--legacy`) keeps the
current single-process mode as a runtime flag, so a bad daemon can be bypassed
without a revert. Ship `--legacy` **before** flipping the default.

---

## DECISIONS NEEDED FROM YOU (blocking — I will not guess on §4 paths)

**1. 🔴 The camera cannot work in a headless core.** It physically lives in the
WebView (`getUserMedia`). Pick one:

| Option | Consequence |
|---|---|
| **(a) Camera requires the window** *(my recommendation)* | Simplest, zero new deps, keeps the self-preview + OS camera light + the Mira consent pattern intact. Headless, "what do you see" answers honestly: *"I need my window open to use the camera."* M0.2 doesn't test camera windowless — only M0.1 (window open) does. |
| (b) Move camera into core via OpenCV | Truly headless camera, but: **new ~60MB dep**, we lose the browser self-preview and the OS indicator, and we'd rebuild the virtual-camera/grey-frame handling we just got working. |
| (c) Hybrid — browser when a UI is attached, OpenCV when not | Both code paths, both sets of bugs, for a case (headless selfie) nobody asked for. |

**2. Auto-start modifies your machine.** Task Scheduler ONLOGON is the only
supervision option that works (D-0.3). Do you want it (a) installed opt-in by the
installer, or (b) left out of Phase 0 entirely — daemon started manually — and
revisited later? Protocol §4 says machine-level changes need explicit consent.

**3. Scope confirmation.** This is 8 commits across a 64-file app and a new
process, touching §4. Confirm you want it as one phase, or whether to land it in
two halves (log+registry+protocol first; daemon+UI+tray+supervision second).

---

## Definition of Done (Protocol §10) — tracked, not yet met

- [ ] Exit criteria 1–5 demonstrated · [ ] full suite green + new IPC tests
- [x] `docs/DECISIONS.md` recorded (D-0.1…D-0.4)
- [ ] ARCHITECTURE.md: process model, protocol schema, threat model · HANDOFF.md
- [ ] Perf: idle CPU delta <1%, RAM, turn latency ±10% of baseline
- [ ] `docs/MANUAL_TEST_PHASE_0.md` + skeptic's "what could still go wrong" (5+)
