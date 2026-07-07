# Architecture — Anastasia (Anna)

Local-only Windows voice assistant. Python owns all state and logic; the UI
is a pywebview (Edge WebView2) window rendering a dumb HTML/JS frontend.
No cloud calls anywhere at runtime.

## Big picture

```
 mic ──► Recorder ──► faster-whisper text + confidence ─┐ keyboard hotkey ─┐
                                          ▼                             ▼
 typed text ──────────────────────► CommandPipeline ◄────────── Controller
                                          │
              silence/noise confidence gate → normalize + sentence split
                                          │
                    fast rule router ─── match? ──► ActionPlan
                                          │ no
                    fuzzy target recovery ─ match? ─► ActionPlan/confirm (15s)
                                          │ no
                    low audio confidence? ──────────► polite retry
                                          │ no
                    local chat/command classifier
                         ├─ chat: slim plain-text prompt ──► reply
                         └─ command: full JSON prompt ─────► ActionPlan
                                          │
                              safety validator (whitelist)
                                          │
                        requires confirmation? ──► approval card (30s TTL)
                                          │
                            whitelisted tool executor
                                          │
                    result ──► conversation + UI event + async TTS
                                          │            (half-duplex gate)
                                       finally: busy=False, state=ready
```

## Components

| Path | Role |
|---|---|
| `app/main.py` | `Controller` (wiring, health checks/chips, toggles, settings, PTT, full_state) + `main()` webview bootstrap |
| `app/agent/pipeline.py` | `CommandPipeline` — the only path a command flows through; busy flag with `finally` + 45s watchdog; confirmation ids + auto-cancel |
| `app/agent/normalizer.py` | transcript cleanup, STT fixes, wake-word removal, multi-sentence split |
| `app/agent/router.py` | exact/pattern rules, RapidFuzz target recovery, `Agent.plan_rule/plan_llm/execute` |
| `app/agent/safety.py` | policy: whitelist, blocked tools, dangerous-terminal patterns, safe folders (NEVER weaken) |
| `app/agent/conversation.py` | structured chat log (role/text/ts/action), `snapshot()` for re-hydration |
| `app/agent/devlog.py` | ring-buffer developer log + per-command `CommandTrace` timings |
| `app/llm/ollama_client.py` | `/api/chat` with think:false, keep_alive, num_predict, num_ctx, num_gpu; `warm_up(messages)`; latency/tokens-per-s stats |
| `app/llm/prompt_builder.py` | full command prompt plus <250-token chat prompt and safe memory lines |
| `app/llm/intent_parser.py` | `<think>` stripping, balanced-JSON extraction, `ActionPlan` validation |
| `app/tools/*` | whitelisted executors (`@tool` registry); nothing model-generated ever executes |
| `app/voice/recorder.py` | mic capture, silence auto-stop, drops frames while TTS gate is set |
| `app/voice/stt_whisper.py` | beam/VAD transcription, live vocabulary primer, `stt_ms`, and aggregate confidence signals |
| `app/voice/speech_output.py` | sentence-streamed, sanitized, cancellable TTS (Piper/Kokoro/SAPI), sets the gate |
| `app/voice/tts_piper.py` / `tts_kokoro.py` | backend setup checks, real synthesis validation, and local WAV generation |
| `app/voice/audio_gate.py` | global `speaking` Event + 400ms tail (half-duplex echo fix) |
| `app/voice/wake_word.py` | optional openWakeWord listener, gate-aware, lazy-imported only when enabled |
| `app/web/bridge.py` | `UIBridge` (Python→JS events, pre-ready buffering) + `JsApi` (JS→Python) |
| `app/web/*.html/css/js` | dumb renderer; design tokens; bundled Inter font; avatar.png slot |

## Threading model

GUI thread = pywebview's; it never blocks. Everything slow runs on daemon
threads: STT, Ollama calls, tool execution, TTS worker, health checks, file
search. `window.evaluate_js` is thread-safe, so worker threads dispatch UI
events directly. One command at a time (busy flag); typed input cancels live
recordings; stale STT results are dropped via a generation counter.

## Python → JS events (`ui.dispatch({type, payload})`)

`status` (chips) · `state_change` · `user_message` · `anna_message` (info/
error variants) · `action_result` (payload for result cards) ·
`confirm_request`/`confirm_resolved` · `toggle_sync` · `setup_card` ·
`devlog` · `latency` · `history` · `settings` · `prefs` · `test_result` ·
`clear_conversation` · `full_state` (complete re-hydration snapshot).

Events raised before the page loads are buffered; `JsApi.ready()` flushes
them, then `full_state` follows. The frontend holds no authoritative state.

## JS → Python (`pywebview.api.*`)

`send_text` · `start_ptt`/`stop_ptt` · `confirm(action_id, approved)` ·
`set_toggle(name, value)` · `open_settings`/`save_settings` (whitelisted
fields only) · `get_history` · `open_path` (screenshot dir + safe folders
only) · `clear_history` · `recheck` · `ready` · `test_voice` ·
`test_microphone` · `test_model`.

## Safety invariants

1. LLM output is parsed into a validated `ActionPlan`; it is data, never code.
2. Every plan passes `validate_action` — unknown tools are blocked.
3. `run_terminal`/`window_control`/`delete_files` always require confirmation;
   dangerous terminal patterns are blocked even with confirmation.
4. Deletion/shutdown/email/credentials: refused outright (MVP).
5. The frontend renders events; it can only call the closed `JsApi` surface.
6. Safety confirmations expire after 30s; neutral fuzzy corrections after
   15s. Both are keyed by action id.

## Performance design

- Rule router first — simple commands never touch Ollama (~0–3ms routing).
- Conversational input skips JSON/tool schema processing; an exact handoff
  marker safely re-enters command mode. Ambiguous input prefers command mode.
- Ollama: model pinned warm (`keep_alive`), no thinking tokens, bounded
  generation, small ctx, CPU-only on this machine (`num_gpu: 0` — iGPU
  offload corrupts llama3.2 output), warm-up request carries the real system
  prompt so the prompt cache absorbs the one-time CPU prefill.
- Heavy imports (PIL/pyautogui/pyperclip) preloaded at startup.
- Microphone input is normalized to 16 kHz mono before Whisper; device/rate
  and one-time low-gain diagnostics stay in the developer channel.
- Animations are CSS transform/opacity only, tiered by
  `animation_quality`; canvases pause when the window is hidden.
```
Measured on the target laptop (i5-8250U): open notepad 42ms · open paint
37ms · open downloads 24ms · screenshot ~590ms · copy 157ms · LLM plans
13–16s (≈5 tok/s CPU) · warm-up 5–38s once, hidden at startup.
```
