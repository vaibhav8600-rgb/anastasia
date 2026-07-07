# HANDOFF — Anastasia (Anna) overhaul

> **Phase 9.1C DONE (Deepgram Aura TTS).** app/voice/tts_deepgram.py:
> synthesize_deepgram (Aura REST, linear16 16kHz PCM -> WAV), validate_
> deepgram_tts, deepgram_tts_available/status; reuses deepgram_key. config
> tts_deepgram_model=aura-2-luna-en; tts_backend adds "deepgram". speech_output:
> _speak_deepgram (WAV via _play_wav_cancellable so barge-in+orb envelope work),
> Aura circuit breaker (2 fails -> deepgram_tts_unhealthy, one warning, fall to
> _speak_piper_or_windows), reset_aura_circuit; barge-in checks _cancel after
> synth. Controller: Voice chip "Voice: Deepgram · luna" / "(Deepgram error)" /
> "(no key)"; validate_deepgram_tts + js_api + settings picker + Privacy note
> ("Deepgram voice sends reply text"). Piper stays the local/default.
> HONEST LATENCY: from this user's location (India) Aura is 2-7s per sentence
> (network RTT to Deepgram US) vs warm Piper 379ms — Aura is SLOWER here, not
> faster; recommend Piper default. 260 tests. Next: 9.1D screenshot thumbnails.


> **Phase 9.1A + 9.1B DONE.** 9.1A: SQLite crash on confirmed actions fixed —
> history.py now per-call connections (WAL + busy_timeout=30000), log/recent/
> clear never raise; pipeline reports success from result.success then logs
> best-effort via _safe_log (execution/logging decoupled). 9.1B: root cause
> was websocket-client in requirements but NOT installed (now installed +
> verified socket connects/auths with the real key). STTRouter.websocket_
> available() + streaming_status()->(state,reason); use_streaming() now
> requires the dep so no silent per-turn fallback. Honest amber STT chip
> ("STT: Local (streaming unavailable)") shown only when stt_mode=streaming;
> warn ONCE per session (_streaming_warned); capability summary line at
> startup ("Capabilities — STT: ... | brain: ... | TTS: ..."); turn_latency
> breakdown uses REAL labeled stt (stt(deepgram)~N / stt(local)~N), never the
> old ~300 estimate. Settings shows streaming status+reason. 252 tests.
> Next: 9.1C Deepgram Aura TTS, 9.1D screenshot thumbnails.


> **Phase 9 COMPLETE (9A-9D). 238 tests passing.** 9D: turn_latency_ms
> consolidated telemetry — controller._start_turn_clock(stt_ms) sets _turn_t0
> before every VOICE pipeline.submit (both _on_stt_final streaming +
> _finish_recording local); _on_first_audio computes turn_latency_ms
> (transcript-ready -> first audible word) + logs breakdown + dispatches
> turn_latency event. Audio-reactive orb: SpeechOutput._wav_envelope (RMS per
> 80ms window, peak-normalized) + _play_with_levels ticks the envelope in sync
> with winsound playback -> on_audio_level(0..1); controller._on_audio_level
> -> speaking_level event; app.js annaLevel eases the particle sphere pulse +
> glow. Gated: speech.emit_levels = (animation_quality=='high'), updated on
> save_settings. NOTE: main.py now has top-level `import time`. Live measured:
> transcript->first-word ~1.0-2.1s (Groq RTT from India is the variable, 576-
> 1253ms; warm-Piper TTS 250-500ms; Deepgram stt_final ~300ms est). Honest:
> sometimes exceeds the 1.5s target when Groq RTT is high — network-bound, not
> fixable locally. config: hands_free/idle_timeout, stt_mode/deepgram_*.
> Phase 9 fully done; user still needs a Deepgram key for live streaming STT.


> **Phase 9C (continuous hands-free) DONE.** Replaced the 8D one-shot 6s
> follow-up with a true loop. config: hands_free (persists), 
> hands_free_idle_timeout_s (45). Controller: start_hands_free/stop_hands_free
> (signoff "I'll be right here..."), _hands_free_continue (reopens mic after
> _on_speaking_changed(False) when active + not processing/pending/speaking,
> after audio_gate.TAIL echo clear), _reset_idle_timer/_on_idle_timeout,
> _hands_free_handle_final(text,confidence) -> stop-phrase (STOP_PHRASES:
> "stop listening"/"that's all"/"bye"/"goodbye anna"/... -> stop+signoff),
> empty/low-conf(<0.4) garble -> don't route, keep listening, 3-streak ->
> ONE "still there?" check-in. Wired into BOTH _on_stt_final (streaming) and
> _finish_recording (local) before submit. Mic tap (start/stop_ptt) ends loop.
> Barge-in in toggle_mic already aborts stream+TTS. set_toggle("hands_free")
> + startup resume (ui.after 1500 if config.hands_free) + full_state toggles.
> arm_followup now a no-op. UI: #toggle-hands-free (bottom bar), #hands-free-badge
> ("conversation mode"), body.hands-free, hands_free event. 233 tests (13 new;
> updated 2 obsolete 8D/bridge tests). Next: 9D full-loop measure + avatar.


> **Phase 9B (streamed LLM->TTS) DONE.** GroqProvider.complete_stream (SSE,
> stream:true, on_token/should_abort, first_token_ms/aborted on LLMResult);
> BrainRouter.stream_chat (Groq stream -> Ollama non-streaming fallback, same
> circuit breaker). app/voice StreamingSentencer (boundary + abbrev/decimal
> guards: 'Dr.'/'3.5' don't split). Agent.plan_chat_stream emits sentences to
> on_sentence, holds the {"handoff":"command"} sentinel back (never spoken;
> whole-reply sentinel -> command handoff), same (plan,handoff) contract.
> Pipeline: chat route uses _run_streaming_chat when voice on -> speaks each
> sentence via speech.speak_async as it arrives; _streamed_reply flag makes
> _respond skip re-speaking; _stream_epoch + abort_stream() for barge-in
> (toggle_mic barge-in calls pipeline.abort_stream()+speech.cancel()).
> COMMAND MODE NEVER STREAMS (plan_llm still complete(); test asserts
> complete_stream unused + partial JSON unparseable). Live: first_token 576ms,
> first_sentence_to_TTS 631ms (short reply; win scales with reply length).
> 221 tests (9 new, SSE mocked). Next: 9C continuous hands-free, 9D measure.


> **Phase 9A (streaming STT) DONE, awaiting approval + user's Deepgram key
> for live numbers.** New: app/voice/stt_providers.py — STTResult,
> DeepgramSTT (WebSocket-client, nova-2, interim_results, endpointing=300;
> _connect() isolated for test mocking; DeepgramStream._handle_message parses
> Deepgram JSON -> on_partial/on_final), WhisperSTT (batch fallback),
> STTRouter (mode()/use_streaming()/circuit breaker 3-fails-120s like the
> brain; masked key; env DEEPGRAM_API_KEY wins). DataClass.LIVE_AUDIO_STREAM
> added (NEVER to brain; Deepgram-only via stt_stream_allowed hard gate).
> Recorder.set_frame_observer feeds live PCM to Deepgram AND still buffers
> for the Whisper safety net. Controller: toggle_mic branches on
> use_streaming; _begin/_finish/_end_streaming_stt, _on_stt_partial/final/
> error; final routes via pipeline.submit same as Whisper; failure/no-final
> falls back to _finish_recording (Whisper on buffer); socket closes when mic
> closes / on cancel / on shutdown. UI: body.stt-streaming (magenta mic ring)
> + #stt-streaming-badge + #stt-interim (greyed live transcript); events
> stt_streaming/stt_interim. Settings: stt_mode select + masked Deepgram key
> + privacy note. config: stt_mode(local)/deepgram_api_key/deepgram_model
> (nova-2). requirements: websocket-client. 212 tests (10 new, WS mocked).
> Next: 9B streamed LLM->TTS, 9C continuous hands-free, 9D full-loop measure.


> **Phase 8 COMPLETE (8A-8D). 202 tests passing.** 8D: TTS delay root-caused
> and fixed — `python -m piper` per sentence cost 4.6s each (interpreter +
> onnxruntime + model load repeated); now an in-process warm PiperVoice
> (tts_piper.get_inproc_voice/_synthesize_inproc/warm_piper) loads once at
> startup (~3.1s hidden) and synthesizes each sentence in ~0.2-0.4s.
> synthesize_piper tries in-process first, subprocess fallback. Live voice
> turn "how are you": STT ~3s + Groq LLM ~1s + tts_first ~0.4s = ~4.5s
> (target <=6s). Added: tts_first_audio_ms telemetry (speech.on_first_audio ->
> devlog), 10-turn hybrid chat context (build_chat_messages history_turns;
> agent.recent_chat_turns set by controller._recent_chat_turns from the
> Conversation model; 10 cloud / 4 local), optional hands_free_followup
> (default off, config hands_free_window_s=6, pipeline arm_followup hook ->
> controller reopens mic after a spoken chat reply). Safety proof test:
> test_groq_plan_still_passes_local_safety_validator (cloud plan understating
> risk still gets locally escalated to confirmation). README hybrid+privacy+
> piper-warm sections added.
>
> --- earlier Phase 8 notes ---
> **8A + 8B DONE (approved/live), 8C done.**
> 8A hybrid brain: app/llm/providers.py — GroqProvider (OpenAI-compat,
> json_object mode, typed errors), OllamaProvider (getter-based), BrainRouter
> (hybrid→Groq 8s→Ollama 15s capped fallback→BrainUnavailable honest msg;
> circuit: 3 fails→open 120s→probe). plan_llm/plan_chat use
> brain.complete(kind); trace has provider/failover; Brain chip + popover;
> cloud settings w/ masked key (never logged/dispatched, test-enforced;
> env GROQ_API_KEY wins). **Live measured: chat 637ms, complex command
> 758ms via Groq (was 9-22s/timeout local).** User's key is in
> app/data/config.json (gitignored).
> 8B piper fix: pip-entry-point paths (venv/Scripts) rejected w/ specific
> message; venv fallback resolution deleted; 10s synth probe at validation
> + startup; TTS circuit breaker (2 fails→bench for session, ONE warning,
> chip 'Voice: Windows fallback (Piper error)', Validate Piper restores);
> errors truncated 200 chars, full tracebacks → app/data/tts_errors.log.
> Mute-fix: piper/kokoro selected-but-unconfigured now falls back to SAPI
> with one warning (was silent + per-sentence spam). User still needs to
> install the piper binary (piper_windows_amd64.zip) and re-point piper_exe.
> Tests: 180 passing; save_settings no longer spawns health threads in
> tests (_background_checks guard). 8C scope: privacy tiers (DataClass
> enum, provider hard gate, clipboard opt-in, private_ memory keys,
> Privacy settings section). 8D: streaming/latency polish + acceptance.

> Continuation file for any agent picking up this project mid-flight.
> Last update: **2026-07-05, end of Phase 6 — ALL SIX PHASES COMPLETE.**
> See: ARCHITECTURE.md (design) · SKILL.md (commands) · FINAL_REPORT.md
> (before/after numbers, deviations, QA matrix, remaining human-only checks).

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
| 6 final QA + README + final report | ✅ done | see git log |

Phase 6 delivered: `--doctor` health check (app/doctor.py, green on target
machine), README rewritten (pywebview/WebView2, llama3.2:3b, troubleshooting
incl. warm-up / half-duplex / @@@@-num_gpu rows), hidden-window E2E QA
(304ms rule cmd, 13.4s correct LLM answer, confirm/cancel by id, no stuck
busy), FINAL_REPORT.md. Open follow-ups (user-driven, not spec debt): real-mic
/ Piper / wake-word manual checks, avatar.png drop-in, user_name setting,
optional Piper WAV volume, voice-approval buttons on web confirm cards.

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

## Phase 6  DONE (2026-07-05)

Delivered: Run all tests; manual QA matrix (typed, voice, Ollama offline, Piper missing,
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
