# HANDOFF — Anastasia (Anna) overhaul

> Continuation file for any agent picking up this project mid-flight.
> Updated at every phase gate. Last update: **2026-07-05, end of Phase 5
> (awaiting user approval to start Phase 6 — the final phase)**.
> See also: ARCHITECTURE.md (system design) and SKILL.md (command reference).

## What this project is

Windows-local voice AI assistant "Anastasia (Anna)" (Python 3.13, Ollama LLM,
faster-whisper STT, Piper/SAPI TTS, whitelisted tool executor + safety
validator). A master spec (in the first user message of the original session)
mandates a 6-phase overhaul: make it fast first, then a futuristic pywebview
"Jarvis" UI matching a reference image (dark glass, neon cyan/blue/violet,
central holographic avatar orb).

**Process rules (binding):**
- Work phase by phase (spec section 25). After each phase: run tests, report
  measured latencies + deviations, STOP and wait for user approval.
- Never weaken the safety system (spec sec 15): tool whitelist, safety
  validator on every plan, terminal always confirms, delete/shutdown blocked,
  LLM only emits JSON plans, frontend never executes raw commands.
- Simple commands must NEVER call Ollama. Commit per phase (small, scoped).
- The user supplies `app/web/assets/avatar.png` themselves — code must load it
  if present, procedural fallback otherwise. Never generate the avatar.

## Phase status

| Phase | Status | Commit |
|---|---|---|
| 1 backend pipeline + Ollama root causes | ✅ approved | `acbe4b6`, `65e4633` |
| 2 user/dev message separation | ✅ approved | `9342e35` |
| 3 pywebview UI shell | ✅ approved | `724e9b3` |
| 4 visual polish (glass/glow/orb/animations) | ✅ done, awaiting approval | see git log |
| 5 voice settings (Piper UI, test voice, STT settings) | ✅ done, awaiting approval | see git log |
| 6 final QA + README + final report | ⬜ pending | — |

Run tests: `./.venv/Scripts/python.exe -m pytest tests/ -q` (118 passing at
end of Phase 3). Run app: `./.venv/Scripts/python.exe app/main.py`.

## Architecture map

- `app/main.py` — Controller: wires config/agent/recorder/SpeechOutput/
  pipeline; implements the pipeline's UI-facing methods; health checks build
  status chips + ONE setup card; `send_full_state()` re-hydrates frontend;
  `set_toggle`, `save_settings`, `open_path` (whitelisted roots), PTT;
  `main()` creates pywebview window (WebView2, missing-runtime message box).
- `app/agent/pipeline.py` — CommandPipeline: normalize → rule router → (LLM
  only if no rule) → safety → confirm (action IDs, 30s auto-cancel) → tool →
  async TTS. Busy flag ALWAYS reset in `finally`; 45s watchdog; typed/voice
  separation; garble → clarification (voice only); timing traces to devlog.
- `app/agent/normalizer.py` — wake-word strip, STT fixes, trailing filler,
  multi-sentence split (first rule-matching sentence wins), Whisper
  hallucination list. Case preserved (needed for `type_text`).
- `app/agent/router.py` — `match_rule` (apps/folders/screenshot/clipboard/
  hotkeys/web/file-search; FOLDER_SYNONYMS, APP_SYNONYMS) + Agent with
  `plan_rule`/`plan_llm` (1 strict JSON retry).
- `app/agent/devlog.py` — ring-buffer DevLog + CommandTrace timing format.
- `app/agent/conversation.py` — structured chat entries (role/text/ts/action
  payload), `snapshot()` for full_state.
- `app/llm/ollama_client.py` — think:false, keep_alive 30m, format:json (on
  planning calls; NOT on summarize — prose needed), num_predict 220,
  num_ctx 2048, `num_gpu` option, (3.05, timeout)s timeouts, warm_up(messages)
  primes the REAL system prompt into Ollama's cache, latency/tokens-per-s.
- `app/llm/prompt_builder.py` — compact intent prompt (<800 tokens, ~570).
- `app/agent/safety.py` — UNCHANGED policy (do not touch).
- `app/voice/audio_gate.py` — global `speaking` Event + 400ms tail.
- `app/voice/speech_output.py` — queued async cancellable TTS worker (Piper →
  winsound async+purge; SAPI → PowerShell Popen+kill); sets/clears the gate.
- `app/voice/recorder.py` — drops frames while gate set; `cancel()` discards.
- `app/web/bridge.py` — UIBridge: everything → `ui.dispatch({type,payload})`;
  buffers events until JsApi.ready(); JsApi: send_text, start_ptt/stop_ptt,
  confirm(action_id, approved), set_toggle, open_settings/save_settings,
  get_history, open_path, clear_history, recheck.
- `app/web/index.html|styles.css|app.js` — dumb renderer. Design tokens from
  spec sec 4 in `:root`. Inter font bundled at `assets/fonts/` (no CDN).
  `assets/avatar.png` drop-in slot (img onload shows it; fallback div/canvas
  otherwise).
- `tests/fakes.py` — FakePipelineUI, FakeMainUI, FakeSpeech, FakeAgent,
  FakeRecorder, ExplodingLLM (fails test if a rule command touches the LLM),
  TestConfig (never writes config.json).

## Critical machine-specific discoveries (do not lose these)

1. **iGPU corruption:** Ollama's partial offload to Intel UHD 620 makes
   llama3.2:3b emit `@@@@` garbage / grammar-stack errors. Fix:
   `ollama_num_gpu: 0` config default (CPU-only; `-1` = auto). Test coverage
   exists. README troubleshooting row added.
2. **Warm-up must send the real intent system prompt** (not "hi") so the
   prompt cache eats the ~35s CPU prefill once, at startup, hidden.
3. This laptop (i5-8250U) generates ~5 tok/s → LLM plans take 13–16s
   (within the 2–20s target). Rule commands: notepad 42ms, paint 37ms,
   downloads 24ms, screenshot ~590ms, copy 157ms (after warm imports).
4. `llama3.2:3b` was pulled via ollama CLI. **Ollama serve is NOT reliably
   running** — it was started manually during the session and later stopped;
   the UI's setup card correctly shows it. User must run the Ollama app.
5. `user_name` in `app/data/memory.json` is "LENOVO" — user may want to set
   "Vaibhav" via the Settings modal.

## Key decisions / deviations already reported & accepted

- `format:"json"` on planning calls only (summarize/persona need prose).
- `teams` alias stays `msteams:` (colon required for URI launch).
- Normalizer preserves case; `type ...` commands skip filler-stripping.
- Config keys keep `ollama_*` names (migration in `AppConfig.load` maps old
  defaults → new: qwen3:4b→llama3.2:3b, 120→20s, 1.6→1.2s, 30→8s rec).
- Native window frame kept (no custom min/max/close) — Windows snap UX.
- Basic settings modal shipped in Phase 3 (name/url/model/timeout/anim
  quality) via `save_settings` extension; full voice settings = Phase 5.
- Extra dispatch event types beyond spec list: `devlog`, `setup_card`,
  `confirm_resolved`, `settings`, `history`, (`prefs` added in Phase 4).
- Screenshot tool uses PIL.ImageGrab (pyautogui import cost 6s);
  `open_folder` fire-and-forget Popen; heavy imports preloaded at startup.

## Phase 4 — DONE (2026-07-05)

Delivered: glass blur(18px) on panels; 3 SVG partial arcs counter-rotating
(state-dependent speeds/colors); dotted ring; layered ring glows per state
(ready breathe / listening cyan pulse / thinking indigo + fast arcs +
animated ellipsis / executing violet / speaking blue pulse / confirmation
amber incl. arcs / error red pulse x3); procedural avatar fallback =
fibonacci particle sphere on canvas (240 pts, palette colors), swaps out
automatically when assets/avatar.png exists; high-tier halo particle canvas
(36 drifting dots) + ambient gradient drift; mic conic-gradient rotating
highlight while recording; quality tiers low/medium/high via
body[data-anim] (low kills blur/animations/canvases), wired from
config.animation_quality through full_state.prefs + live `prefs` dispatch on
save_settings; prefers-reduced-motion forces low; canvases pause on
visibilitychange. Verified: 119 tests; hidden-window DOM checks
(tier switching end-to-end via save_settings); visible-window screenshots in
scratchpad (p4_ready/p4_listening/p4_confirm.png).

**Gotcha discovered:** hidden pywebview windows freeze the compositor — CSS
*transitions* never advance, so getComputedStyle returns stale values for
transitioned properties. Verify transitioned styles in a visible window;
static computed properties (animationDuration etc.) are fine hidden.

## Phase 5 — DONE (2026-07-05)

Delivered: sectioned settings modal (General / Local model / Voice input /
Voice output) with 14 fields; new config keys `tts_rate` (0.5–2.0),
`tts_volume` (0–100, SAPI only — winsound can't attenuate Piper playback;
noted limitation), `stt_language` (auto/en/hi/mr, passed to faster-whisper);
SAPI `$s.Rate`/`$s.Volume` mapping via `sapi_rate()` and Piper
`--length_scale` via `piper_length_scale()` (both unit-tested);
save_settings whitelist extended with choice validation (bad enum values
rejected); Test model / Test microphone / Test voice buttons — each saves
the form first, then runs async and reports back via `test_result` dispatch
into a status line; friendly warning when tts_backend=piper but paths
missing. 126 tests. Also added ARCHITECTURE.md + SKILL.md.

## Phase 6 scope

Run all tests; manual QA matrix (typed, voice, Ollama offline, Piper missing,
wake word disabled, timeout recovery, confirm approve/deny/timeout); README
rewrite (pywebview + WebView2 note, ollama pull llama3.2:3b, troubleshooting:
slow first response → warm-up; Anna hears herself → half-duplex; @@@@ →
num_gpu 0); final report: files changed, before/after latencies, deviations.
Also still open (spec items not yet built): `--doctor` health check command
(sec 19), voice-confirm button on web confirm cards, Conversations page
beyond the history modal, custom frameless chrome (declined for now).

## Gotchas for the next agent

- Tests import from `tests.fakes`; pipeline tests run `run_async=False`.
- Don't run the visible app while the user is active — use
  `webview.create_window(..., hidden=True)` + `evaluate_js` DOM checks
  (see scratchpad smoke_web.py pattern in session notes).
- Kill any Notepad/Paint/Explorer windows and delete AnnaScreenshots files
  that automated tests create.
- devlog is a module-global; clear it in tests that assert its contents.
- `Controller(ui=...)` requires a ui (bridge or fake); autostart=False in
  tests skips hotkey/startup threads.
- Git: commit per phase, Co-Authored-By Claude line, no push (no remote).
