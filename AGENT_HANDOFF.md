# AGENT_HANDOFF — complete takeover guide for Anastasia (Anna)

Self-contained onboarding for any agent taking over this repo. Companion
docs: ARCHITECTURE.md (design detail), SKILL.md (user-facing commands),
FINAL_REPORT.md (before/after + QA), HANDOFF.md (phase-gate log).
This file wins on conflicts; verify against code before large changes.

---

## 1. What this app is

**Anastasia ("Anna")** — a fully local Windows voice assistant with a
futuristic glass UI (pywebview/WebView2). Voice or typed commands →
rule router (instant) or local LLM (Ollama) → safety validator →
whitelisted tool executor → spoken + rendered reply. No cloud calls.
Personality: warm, playful, lightly flirty best-friend; short spoken replies.

**Status: Phases 1–11 COMPLETE; Phase 0 (core/UI split) IN PROGRESS.**
**456 tests passing.** (This file's older sections describe the state at the
end of the original 6-phase overhaul and are kept as history — where they
disagree with the code, the code wins. See `PLAN_PHASE_0.md` and
`docs/DECISIONS.md` for current work.)
The original 6-phase master spec lives in the first user message of the
original session; its binding rules are reproduced in §10 below.

## 2. Run / test / debug

```powershell
.venv\Scripts\python.exe app\main.py             # DEFAULT: Phase-0 split (core+tray+window)
.venv\Scripts\python.exe app\main.py --core      # headless daemon only
.venv\Scripts\python.exe app\main.py --ui        # window only (needs a running --core)
.venv\Scripts\python.exe app\main.py --legacy    # pre-split single-process app (escape hatch)
.venv\Scripts\python.exe app\main.py --doctor    # health check
.venv\Scripts\python.exe -m pytest tests/ -q     # 586 tests, ~70s
```

Git: local repo only (no remote). One commit per unit of work,
`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer, git user
`Vaibhav <vaibhavtest8600@gmail.com>` passed via `-c` flags.

## 3. Target machine facts (load-bearing!)

- Laptop: i5-8250U, Intel UHD 620 iGPU, Windows 11. Python 3.13.5 in `.venv`.
- **iGPU offload corrupts llama3.2 output** (`@@@@` garbage / grammar-stack
  errors). Fix shipped: config `ollama_num_gpu: 0` (CPU-only; `-1`=auto).
  NEVER remove without re-testing on this machine.
- CPU inference ≈5 tok/s: LLM plans take 13–16 s (spec target 2–20 s ✅).
  Prompt prefill is expensive → warm-up MUST send the real intent system
  prompt (`warm_up(build_intent_messages(...))`) so Ollama's prompt cache
  absorbs the 30 s+ prefill once at startup.
- Ollama installed at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`; models
  pulled: `llama3.2:3b` (default), `qwen3:4b` (legacy). The serve process is
  NOT auto-started — user runs the Ollama app; UI shows a setup card if down.
- Whisper `base` cached; Piper NOT installed (Windows SAPI fallback active).
- `openwakeword` NOT installed (wake word off by default).
- User's Desktop is OneDrive-redirected; `C:/Users/LENOVO/Projects` doesn't
  exist → doctor warns; user's real projects are on F:.
- `app/data/memory.json` `user_name` = "LENOVO" (user may set "Vaibhav").
- The user often actively uses this machine — see §9 testing etiquette.

## 4. File inventory (what owns what)

```
app/main.py            Controller + main(). Health checks -> chips + ONE setup
                       card; send_full_state(); set_toggle; save_settings
                       (whitelist _SETTINGS_FIELDS + _SETTINGS_CHOICES);
                       test_voice/test_microphone/test_model; start_ptt/stop_ptt;
                       toggle_mic (barge-in: cancels TTS); stale-STT generation
                       counter; open_path (whitelisted roots); wake word
                       (lazy import, warn once, flips toggle back off);
                       _warm_imports (PIL/pyperclip/pyautogui); --doctor hook;
                       WebView2-missing message box.
app/agent/pipeline.py  CommandPipeline. THE only command path. Busy flag reset
                       in finally + 45s watchdog (token-guarded); pending
                       confirmation (action_id, 30s auto-cancel Timer);
                       garble->clarify for voice; CommandTrace timings;
                       messages: BUSY/PENDING/EMPTY/GARBLE/TIMEOUT constants.
app/agent/normalizer.py normalize_command -> NormalizedCommand(raw, cleaned,
                       sentences). Wake-word strip, STT_FIXES regexes,
                       TRAILING_FILLER, WHISPER_HALLUCINATIONS set,
                       looks_garbled(). Case preserved; `type ...` skips
                       filler-strip.
app/agent/router.py    match_rule (APP_SYNONYMS, FOLDER_SYNONYMS, KNOWN_SITES,
                       HOTKEY_PHRASES; alias map is first-occurrence-wins) +
                       Agent.plan_rule/plan_llm (1 strict retry)/execute.
app/agent/safety.py    DO NOT WEAKEN. Whitelist SAFE_TOOLS/CONFIRM_TOOLS/
                       BLOCKED_TOOLS, DANGEROUS_TERMINAL_PATTERNS, hotkey
                       allowlist, safe-folder path checks, strict mode.
app/agent/conversation.py Structured chat log; snapshot() for full_state.
app/agent/devlog.py    Global ring buffer `devlog` + CommandTrace.format().
app/llm/ollama_client.py chat(): think:false, keep_alive, format:json (only
                       when json_format=True; summarize passes False),
                       num_predict/num_ctx/num_gpu opts, (3.05, timeout)s;
                       qwen <think> strip; warm_up(messages); latency +
                       tokens/s stats (last/avg).
app/llm/prompt_builder.py Intent prompt < 800 tokens (test-enforced <3200
                       chars); persona + summarize prompts.
app/llm/intent_parser.py strip_thinking, balanced-JSON extraction, ActionPlan
                       (pydantic, normalizing validators).
app/tools/__init__.py  @tool registry; run_tool never raises outward.
app/tools/open_app.py  resolve_app + fallback: absolute path missing -> start
                       <stem>. Result msg: "Done — I opened X for you."
app/tools/file_tools.py open_folder (fire-and-forget explorer Popen),
                       search_files, delete_files stub (always refuses).
app/tools/screenshot.py PIL.ImageGrab direct (NOT pyautogui — 6s import).
app/tools/{clipboard_tools,browser,keyboard_mouse,terminal,window_control}.py
app/voice/audio_gate.py Global `speaking` Event + TAIL_SECONDS=0.4.
app/voice/speech_output.py SpeechOutput: queue worker; speak_async/cancel
                       (barge-in)/shutdown; sets gate for playback+tail;
                       Piper (winsound SND_ASYNC + purge-on-cancel,
                       --length_scale from tts_rate) -> SAPI PowerShell
                       (Rate/Volume from tts_rate/tts_volume, kill-on-cancel).
                       Helpers: sapi_rate(), piper_length_scale().
app/voice/recorder.py  Drops frames while gate set; cancel() discards;
                       silence auto-stop; max duration.
app/voice/stt_whisper.py faster-whisper (cached model, int8) with
                       stt_language; whisper_cpp alt path.
app/voice/wake_word.py openWakeWord listener ("hey jarvis"), gate-aware.
app/web/bridge.py      UIBridge (buffers until JsApi.ready(), then flush +
                       full_state; duck-types the controller's UI surface:
                       transcript.*, confirm_panel.show/hide, set_state,
                       set_mic_active, set_wake_switch, show/hide_setup_card,
                       show_history_window, after, destroy) + JsApi.
app/web/index.html     5 regions: topbar chips / sidebar+status card+devtools
                       btn / hero core (SVG arcs, halo canvas, avatar img +
                       avatar-canvas + fallback div) / conversation / bottom
                       bar. Inline Lucide-style SVGs.
app/web/styles.css     :root tokens (spec sec 4); glass blur(18px);
                       body[data-anim=low|medium|high] gates; state styling
                       via #core[data-state=...]; confirm/result cards; modal.
app/web/app.js         Dumb renderer. handlers{} for every dispatch type;
                       STATES map; autoscroll-unless-scrolled; escape all
                       text (esc()); settings modal (collectSettings());
                       particle sphere + halo canvases (pause on hidden;
                       prefers-reduced-motion forces low).
app/web/assets/fonts/InterVariable.woff2  bundled, no CDN.
app/web/assets/avatar.png  USER-SUPPLIED drop-in (not present yet).
app/doctor.py          --doctor checks (utf-8 stdout reconfigure).
app/config.py          AppConfig (pydantic) + _DEFAULT_MIGRATIONS (old
                       defaults -> new: qwen3:4b->llama3.2:3b, 120->20,
                       1.6->1.2, 30->8) + alias merge via setdefault.
tests/fakes.py         TestConfig(no-save)/make_config, FakePipelineUI,
                       FakeMainUI, FakeSpeech, FakeAgent, FakeRecorder,
                       ExplodingLLM, FakeHistory.
tests/test_*.py        pipeline, router_rules, audio (half-duplex/barge-in),
                       ollama (payload/migration/prompt budget), wakeword,
                       conversation (startup/setup card), bridge (buffering/
                       full_state/confirm ids/FakeWindow decodes dispatches),
                       voice_settings, + original intent_parser/safety/tools.
```

## 5. Config keys (app/data/config.json; example in config.example.json)

Identity: assistant_name/nickname. LLM: ollama_url, ollama_model
(llama3.2:3b), ollama_timeout (20), ollama_keep_alive (30m),
ollama_num_predict (220), ollama_num_ctx (2048), **ollama_num_gpu (0)**.
Voice: voice_enabled, wake_word_enabled, push_to_talk_hotkey
(ctrl+alt+space). STT: stt_backend, faster_whisper_model (base),
stt_language (auto), sample_rate, silence_auto_stop, silence_threshold,
silence_seconds (1.2), max_record_seconds (8), whisper_cpp_*. TTS:
tts_backend (auto|piper|windows|off), piper_exe, piper_voice, tts_rate
(1.0; 0.5–2), tts_volume (100; SAPI only). Safety: confirmation_mode
(strict), max_type_text_no_confirm (500), safe_folders, app_aliases
(paint->mspaint.exe etc.). UI: animation_quality (medium). Tools:
default_browser, screenshot_dir. Loading migrates old defaults and merges
missing aliases without touching user customizations.

## 6. Event protocol

Python→JS (`ui.dispatch({type,payload})`): status{chips} · state_change
{state,detail} · user_message/anna_message{text,ts,info?,error?} ·
action_result{text,ts,action{intent,success,data}} · confirm_request
{id,transcript,tool,arguments,risk,message} · confirm_resolved ·
toggle_sync{name,value} · setup_card{issues[]} · devlog{ts,category,message}
· latency{summary} · history{rows} · settings{...} · prefs
{animation_quality} · test_result{kind,ok,message} · clear_conversation ·
full_state{state,chips,toggles,conversation,devlog,pending,hotkey,
assistant,prefs}.

JS→Python (`pywebview.api.*`): ready · send_text · start_ptt/stop_ptt ·
confirm(action_id,approved) · set_toggle · open_settings/save_settings ·
get_history · open_path · clear_history · recheck · test_voice ·
test_microphone · test_model.

States: ready|listening|transcribing|thinking|executing|speaking|
waiting_confirmation|error.

## 7. Immutable safety invariants

LLM returns JSON plans only, never executes; every plan through
validate_action; unknown tools blocked; run_terminal/window_control/
delete_files always confirm; dangerous terminal patterns blocked even with
approval; delete/shutdown/email/credentials refused; frontend can only call
the closed JsApi; open_path restricted to screenshot dir + safe folders;
confirmations expire (30 s) and are id-keyed. Simple commands NEVER call
Ollama (ExplodingLLM tests enforce).

## 8. Performance truths (don't regress)

Rule route 0–3 ms; notepad 42 ms / paint 37 ms / downloads 24 ms / copy
157 ms / screenshot 0.4–0.6 s tool-time; LLM 13–16 s; warm-up 5–38 s hidden
at startup. Non-blocking TTS; busy flag can never stick (finally + watchdog);
typed input cancels recordings; empty STT never sets busy.

## 9. Testing etiquette & gotchas

- Use `webview.create_window(..., hidden=True)` + `evaluate_js` for E2E
  checks — no visible window, user is often active on the machine.
- **Hidden windows freeze CSS transitions** — computed values of
  *transitioned* props are stale; verify those in a brief visible window.
- Disable voice (`voice_enabled=False` config or FakeSpeech) in E2E runs or
  Anna SPEAKS ALOUD on the user's speakers (happened once; user warned).
- Clean up: taskkill test-opened notepad/mspaint, delete
  `~/Pictures/AnnaScreenshots/anna_*.png` created by tests, close Downloads
  Explorer windows via Shell.Application COM.
- `devlog` is a module-global — `devlog.clear()` in tests asserting content.
- Controller for tests: `Controller(ui=FakeMainUI()/UIBridge, autostart=False,
  config=make_config(), memory=..., history=FakeHistory())`; then
  `controller.speech.shutdown()` to kill the real TTS thread.
- Pipeline tests: `run_async=False`; timers tested with tiny
  watchdog_seconds/confirm_timeout_seconds.
- pip installs: pywebview is installed; requirements.txt is authoritative.

## 10. Binding process rules (from the master spec)

1. Never weaken safety (§7). 2. Simple commands never hit Ollama.
3. Small scoped commits. 4. Report deviations explicitly. 5. The avatar
image is user-supplied — never generate it; keep the drop-in slot working.
6. UI is a dumb renderer; all state lives in Python. 7. No CDN/cloud at
runtime. 8. If resuming multi-phase work: stop at gates, report measured
latencies, wait for user approval.

## 11. Accepted deviations (already approved — don't "fix" them back)

format:json only on planning calls · `msteams:` alias colon · case-preserving
normalizer · `ollama_*` key names kept · num_gpu added · warm-up uses real
prompt · native window frame · extra dispatch event types · Piper volume not
adjustable (winsound) · voice-approval not on web confirm cards (controller
`voice_confirm` exists, unwired).

## 12. Open follow-ups (nice-to-have, user-driven)

- User actions: drop avatar.png; set user_name; install Piper (+ set paths,
  Test voice); optionally openwakeword; fix safe_folders (OneDrive Desktop,
  F:-drive projects); real-mic QA.
- Engineering ideas: voice-approve buttons on confirm cards; Conversations
  page (history.sqlite paging UI beyond the modal); Skills page fed by
  SKILL.md; custom frameless chrome; Piper WAV-scaling volume; trim LLM JSON
  fields to cut plan latency toward ~8 s; custom "Hey Anna" wake model;
  packaging (pyinstaller); auto-start Ollama serve if installed but down.
