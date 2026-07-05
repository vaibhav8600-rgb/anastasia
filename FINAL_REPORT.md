# Final Report — Anastasia (Anna) overhaul

Six phases, commits `acbe4b6` → HEAD. All 126 automated tests pass.
`python app\main.py --doctor` is green on the target machine (2 warnings:
OneDrive-redirected Desktop and missing C:/Users/LENOVO/Projects safe folder
— edit `safe_folders` in config to taste).

## Before → after

| Metric | Before | After |
|---|---|---|
| "open paint" | went to qwen3:4b, minutes/timeout | 37 ms tool-time, ~300 ms end-to-end, LLM never called |
| "open notepad" | worked but LLM-adjacent flow | 42 ms tool-time |
| take screenshot | ~6 s (pyautogui import) | 0.4–0.6 s tool-time (PIL direct) |
| LLM command | 2–10 min or timeout (thinking tokens, unload/reload, 120 s timeout) | 13–16 s incl. correct structured plans; 20 s hard timeout with friendly recovery |
| First LLM use per session | full cold cost on first command | 5–38 s warm-up at startup, hidden, real-prompt cache |
| Busy state | stuck "still on the last one" | finally-reset + 45 s watchdog; never stuck in QA |
| Echo loop | Anna transcribed her own speech | impossible while gate is set (mic drops all frames during TTS +0.4 s) |
| UI | CustomTkinter debug-style window | pywebview glass UI per reference: chips, sidebar, particle-orb core, conversation cards, animated states, quality tiers |
| Debug output | mixed into chat | Developer Tools drawer only |

Root causes fixed, not patched: qwen thinking tokens (→ llama3.2:3b +
`think:false`), model unload (`keep_alive: 30m`), unbounded generation
(`num_predict: 220`), oversized prompt (<800 tokens), **iGPU offload
corrupting llama3.2 output on Intel UHD 620 (`num_gpu: 0`)**, missing paint
alias, blocking TTS, mic open during playback.

## Files changed (high level)

- **New:** `app/agent/{pipeline,normalizer,devlog,conversation}.py`,
  `app/voice/{audio_gate,speech_output}.py`, `app/web/*` (bridge + frontend
  + bundled Inter font), `app/doctor.py`, `tests/{fakes,test_pipeline,
  test_router_rules,test_audio,test_ollama,test_wakeword,test_conversation,
  test_bridge,test_voice_settings}.py`, `ARCHITECTURE.md`, `SKILL.md`,
  `HANDOFF.md`, `FINAL_REPORT.md`.
- **Rewritten:** `app/main.py` (controller + webview bootstrap),
  `app/llm/{ollama_client,prompt_builder}.py`, `README.md`,
  `config.example.json`.
- **Modified:** `app/config.py` (new keys + migration), `app/agent/router.py`
  (rules split plan_rule/plan_llm), `app/tools/{open_app,file_tools,
  screenshot}.py`, `app/voice/{recorder,stt_whisper,wake_word}.py`,
  `requirements.txt` (customtkinter → pywebview).
- **Deleted:** `app/gui/` (CustomTkinter).
- **Unchanged by policy:** `app/agent/safety.py` (validated by tests).

## Spec deviations (all reported at phase gates)

1. `format:"json"` on planning calls only — summaries/persona need prose.
2. `teams` alias `msteams:` (colon needed for URI launch).
3. Cleaned commands keep casing (needed for `type_text`); router matches
   case-insensitively.
4. Config keys keep `ollama_*` names; old values migrate automatically.
5. `ollama_num_gpu: 0` added (not in spec) — iGPU corruption fix.
6. Warm-up sends the real system prompt (spec said tiny request; kept
   num_predict=1 but with real messages — the cache is the point).
7. Native window frame (no custom min/max/close) — Windows snap UX.
8. Extra dispatch event types for a stateless frontend (devlog, setup_card,
   confirm_resolved, settings, history, prefs, test_result).
9. Piper volume not adjustable (winsound limitation); speed works via
   length_scale. Volume applies to the Windows voice.
10. Voice-approval ("say approve") not wired into web confirm cards —
    buttons only; controller support exists (`voice_confirm`).

## Needs a human (can't be automated)

- Real microphone end-to-end: speak a command, verify transcription quality;
  Test microphone button in Settings covers the plumbing.
- Piper: install + set paths + Test voice (machine has no Piper install).
- Wake word with `openwakeword` installed (package not installed here).
- Drop your `avatar.png` into `app/web/assets/` (particle sphere until then).
- Optional: set your name in Settings (greeting currently says "LENOVO").

## QA matrix (automated, hidden-window E2E unless noted)

| Case | Result |
|---|---|
| typed rule command | ✅ 304 ms end-to-end |
| screenshot | ✅ card + View path (2.1 s incl. polling overhead) |
| LLM chat | ✅ "The capital of France is Paris." in 13.4 s |
| confirmation request → cancel by id | ✅ pending cleared, nothing ran |
| confirmation approve / timeout / stale id | ✅ unit tests |
| Ollama offline | ✅ setup card + rule commands keep working (unit + live) |
| Ollama timeout mid-command | ✅ friendly message, state resets (unit) |
| empty/hallucinated STT, typed-cancels-voice, stale STT | ✅ unit tests |
| half-duplex + barge-in | ✅ unit tests (gate drops frames; PTT cancels TTS) |
| wake word disabled / missing package | ✅ unit tests (no import, one warning) |
| busy watchdog | ✅ unit test (force-clears) |
| doctor | ✅ green on target machine |
