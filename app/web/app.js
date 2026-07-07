/* Anastasia (Anna) — frontend renderer.
   Dumb by design (spec sec 3): all state lives in Python. This file only
   renders `ui.dispatch({type, payload})` events and forwards user input
   to `pywebview.api`. It never receives or executes raw commands. */

"use strict";

const $ = (sel) => document.querySelector(sel);

/* ------------------------------------------------------------------ utils */
function esc(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function api() {
  return (window.pywebview && window.pywebview.api) || null;
}

function call(method, ...args) {
  const a = api();
  if (a && typeof a[method] === "function") {
    return a[method](...args);
  }
  return Promise.resolve(null);
}

/* ------------------------------------------------------------- state map */
const STATES = {
  ready:                { word: "Ready", sub: "You can speak now or type a command.", card: "Ready 💙" },
  listening:            { word: "Listening", sub: "You can speak now.", card: "Listening" },
  transcribing:         { word: "Thinking", sub: "Catching what you said…", card: "Thinking" },
  thinking:             { word: "Thinking", sub: "Working on it.", card: "Thinking" },
  executing:            { word: "Working", sub: "On it.", card: "Working" },
  speaking:             { word: "Speaking", sub: "…", card: "Speaking" },
  waiting_confirmation: { word: "Waiting for approval", sub: "Check the conversation panel.", card: "Waiting", tone: "warn" },
  waiting_clarification:{ word: "Quick check", sub: "Did I hear you right?", card: "Waiting" },
  error:                { word: "Hmm.", sub: "Something went wrong — I'm still here.", card: "Ready 💙", tone: "error" },
};
const LIVE_STATES = new Set(["listening", "speaking"]);

let currentState = "ready";

/* -------------------------------------------------------------- messages */
const messagesEl = () => $("#messages");

function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}

function appendMessage(html, cls) {
  const box = messagesEl();
  const stick = nearBottom(box);
  const wrap = document.createElement("div");
  wrap.className = "msg " + cls;
  wrap.innerHTML = html;
  box.appendChild(wrap);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  if (stick) box.scrollTop = box.scrollHeight;
  return wrap;
}

function userMessageHtml(p) {
  return `<div class="msg-meta"><span class="who">You</span><span>${esc(p.ts || "")}</span></div>
          <div class="msg-bubble">${esc(p.text)}</div>`;
}

function annaMessageHtml(p) {
  return `<div class="msg-meta"><span class="anna-orb"></span>
            <span class="who">${esc(p.name || "Anna")}</span><span>${esc(p.ts || "")}</span></div>
          <div class="msg-bubble">${esc(p.text)}</div>`;
}

function resultCardHtml(action) {
  if (!action) return "";
  const data = action.data;
  const viewable = typeof data === "string" && data.length > 0;
  return `<div class="result-card">
            <span class="ok">✅</span>
            <span class="grow">${viewable ? esc(data) : esc(action.intent || "done")}</span>
            ${viewable ? `<button class="ghost-btn" data-open-path="${esc(data)}">View ↗</button>` : ""}
          </div>`;
}

function renderEntry(p, role) {
  if (role === "user") { appendMessage(userMessageHtml(p), "user"); return; }
  if (role === "info" || p.info) { appendMessage(annaMessageHtml(p), "info"); return; }
  if (role === "error" || p.error) { appendMessage(annaMessageHtml(p), "error"); return; }
  if (p.action) {
    appendMessage(annaMessageHtml(p) + resultCardHtml(p.action), "anna");
    return;
  }
  appendMessage(annaMessageHtml(p), "anna");
}

function confirmCardHtml(p) {
  const args = JSON.stringify(p.arguments || {});
  if (p.kind === "fuzzy") return `<div class="confirm-card fuzzy" data-confirm-id="${esc(p.id)}">
            <h4>Quick check</h4>
            <div class="confirm-msg">${esc(p.message || "Did I hear you right?")}</div>
            <div class="confirm-actions">
              <button class="ghost-btn approve" data-confirm="${esc(p.id)}" data-approved="1">Yes</button>
              <button class="ghost-btn" data-confirm="${esc(p.id)}" data-approved="0">No</button>
            </div>
          </div>`;
  return `<div class="confirm-card" data-confirm-id="${esc(p.id)}">
            <h4>⚠ Approval required <span class="dim">· risk: ${esc(p.risk || "?")}</span></h4>
            <code>${esc(p.tool)} ${esc(args)}</code>
            <div class="confirm-msg">${esc(p.message || "Do you want me to go ahead?")}</div>
            <div class="confirm-actions">
              <button class="ghost-btn approve" data-confirm="${esc(p.id)}" data-approved="1"
                      style="border-color:rgba(52,211,153,.5);color:#a7f3d0">Run it</button>
              <button class="ghost-btn" data-confirm="${esc(p.id)}" data-approved="0">Cancel</button>
            </div>
          </div>`;
}

/* --------------------------------------------------------------- devlog */
function appendDevlog(entry) {
  const log = $("#dev-log");
  const stick = nearBottom(log);
  const line = document.createElement("div");
  line.className = entry.category || "info";
  line.textContent = `[${entry.ts}] [${entry.category}] ${entry.message}`;
  log.appendChild(line);
  while (log.children.length > 350) log.removeChild(log.firstChild);
  if (stick) log.scrollTop = log.scrollHeight;
}

/* ---------------------------------------------------------------- chips */
function setChips(chips) {
  for (const key of ["brain", "model", "mic", "voice"]) {
    const chip = $("#chip-" + key);
    const data = chips && chips[key];
    if (!chip || !data) continue;
    chip.querySelector(".chip-label").textContent = data.label || "";
    chip.dataset.state = data.state || "ok";
  }
}

/* ------------------------------------------- animation quality + canvases */
let animQuality = "medium";
let avatarLoaded = false;
let avatarRAF = 0, haloRAF = 0;

function setAnimQuality(quality) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    quality = "low";
  }
  animQuality = ["low", "medium", "high"].includes(quality) ? quality : "medium";
  document.body.dataset.anim = animQuality;
  syncCanvases();
}

function syncCanvases() {
  cancelAnimationFrame(avatarRAF); avatarRAF = 0;
  cancelAnimationFrame(haloRAF); haloRAF = 0;
  const sphereOn = !document.hidden && !avatarLoaded && animQuality !== "low";
  $("#avatar-canvas").style.display = sphereOn ? "block" : "none";
  $("#avatar-fallback").style.display =
    (avatarLoaded || sphereOn) ? "none" : "block";
  if (sphereOn) startAvatarSphere();
  if (!document.hidden && animQuality === "high") startHalo();
}
document.addEventListener("visibilitychange", syncCanvases);

/* procedural fallback: particle sphere in the palette (spec sec 4) */
function startAvatarSphere() {
  const canvas = $("#avatar-canvas");
  const size = canvas.parentElement.clientWidth || 280;
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const N = 240, R = size * 0.36, C = size / 2;
  const pts = [];
  for (let i = 0; i < N; i++) {           // fibonacci sphere distribution
    const y = 1 - (i / (N - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = i * 2.399963;
    pts.push({ x: Math.cos(th) * r, y, z: Math.sin(th) * r });
  }
  const colors = ["34,211,238", "59,130,246", "99,102,241", "168,85,247"];
  let angle = 0;
  (function frame() {
    angle += 0.004;
    const sin = Math.sin(angle), cos = Math.cos(angle);
    ctx.clearRect(0, 0, size, size);
    for (let i = 0; i < N; i++) {
      const p = pts[i];
      const x = p.x * cos - p.z * sin;
      const z = p.x * sin + p.z * cos;
      const depth = (z + 1) / 2;          // 0 back .. 1 front
      const px = C + x * R * (0.85 + depth * 0.15);
      const py = C + p.y * R * 0.92 + Math.sin(angle * 2 + i) * 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 0.6 + depth * 1.5, 0, 6.2832);
      ctx.fillStyle = `rgba(${colors[i % colors.length]},${0.12 + depth * 0.55})`;
      ctx.fill();
    }
    avatarRAF = requestAnimationFrame(frame);
  })();
}

/* high tier: tiny scattered particles drifting around the ring */
function startHalo() {
  const canvas = $("#halo-canvas");
  const size = canvas.parentElement.clientWidth * 1.16 || 420;
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const C = size / 2, R0 = size * 0.40;
  const dots = Array.from({ length: 36 }, (_, i) => ({
    a: Math.random() * 6.2832, r: R0 + (Math.random() - 0.3) * size * 0.07,
    s: 0.0004 + Math.random() * 0.0012, size: 0.5 + Math.random() * 1.3,
    tw: Math.random() * 6.2832,
  }));
  (function frame(t) {
    ctx.clearRect(0, 0, size, size);
    for (const d of dots) {
      d.a += d.s;
      const alpha = 0.25 + 0.35 * Math.sin(t / 900 + d.tw);
      ctx.beginPath();
      ctx.arc(C + Math.cos(d.a) * d.r, C + Math.sin(d.a) * d.r, d.size, 0, 6.2832);
      ctx.fillStyle = `rgba(34,211,238,${Math.max(alpha, 0.05)})`;
      ctx.fill();
    }
    haloRAF = requestAnimationFrame(frame);
  })(0);
}

/* ---------------------------------------------------------------- state */
function setState(state, detail = "") {
  currentState = state;
  const meta = STATES[state] || STATES.ready;
  $("#core").dataset.state = state;
  const word = $("#state-word");
  word.textContent = meta.word;
  word.dataset.tone = meta.tone || "";
  $("#state-sub").textContent = detail || meta.sub;
  $("#state-sub").classList.toggle("ellipsis",
    state === "thinking" || state === "transcribing");
  $("#status-word").innerHTML = meta.card === "Ready 💙"
    ? 'Ready <span class="heart">💙</span>' : esc(meta.card);
  $("#status-card").parentElement.dataset.live = LIVE_STATES.has(state);
  $("#mic-btn").classList.toggle("recording", state === "listening");
  document.querySelectorAll(".wave").forEach(w =>
    w.classList.toggle("live", LIVE_STATES.has(state)));
}

/* --------------------------------------------------------------- modals */
function openModal(title, bodyHtml) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  $("#modal-backdrop").classList.remove("hidden");
}
function closeModal() { $("#modal-backdrop").classList.add("hidden"); }

function shortcutsHtml() {
  return `<table>
    <tr><th>Action</th><th>Keys</th></tr>
    <tr><td>Push to talk (global)</td><td><kbd id="sc-hotkey">Ctrl + Alt + Space</kbd></td></tr>
    <tr><td>Send typed command</td><td><kbd>Enter</kbd></td></tr>
    <tr><td>Focus the input box</td><td><kbd>Ctrl + L</kbd></td></tr>
    <tr><td>Close dialogs</td><td><kbd>Esc</kbd></td></tr>
  </table>`;
}

function historyHtml(rows) {
  if (!rows || !rows.length) return "<p>No history yet.</p>";
  const body = rows.map(r => `<tr>
      <td class="dim">${esc((r.ts || "").replace("T", " "))}</td>
      <td>${esc(r.transcript || "")}</td>
      <td class="dim">${esc(r.tool || "")}</td>
      <td>${r.executed ? "✅" : (r.allowed ? "—" : "⛔")}</td>
    </tr>`).join("");
  return `<table><tr><th>When</th><th>Command</th><th>Tool</th><th></th></tr>${body}</table>`;
}

function selectHtml(id, value, options) {
  return `<select id="set-${id}">${(options || []).map((option) => {
    const opt = (typeof option === "string")
      ? { value: option, label: option }
      : { value: String(option.id ?? option.value ?? ""),
          label: String(option.label ?? option.name ?? option.id ?? option.value ?? "") };
    return `<option value="${esc(opt.value)}" ${opt.value === String(value ?? "") ? "selected" : ""}>${esc(opt.label)}</option>`;
  }).join("")}</select>`;
}

function settingsHtml(s) {
  return `
    <h4 class="settings-section">General</h4>
    <div class="form-row"><label>Your name (Anna greets you with it)</label>
      <input id="set-user_name" value="${esc(s.user_name || "")}"></div>
    <div class="form-row"><label>Animation quality</label>
      ${selectHtml("animation_quality", s.animation_quality, ["low", "medium", "high"])}</div>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-hands_free_followup"
             ${s.hands_free_followup ? "checked" : ""} style="width:auto">
      <label for="set-hands_free_followup" style="margin:0">
        Hands-free follow-up: after Anna answers by voice, reopen the mic for
        ~6s so you can reply without pressing the hotkey.</label>
    </div>

    <h4 class="settings-section">Cloud brain</h4>
    <div class="form-row"><label>Cloud brain (faster, needs internet)</label>
      ${selectHtml("brain_mode", s.brain_mode, ["hybrid", "local_only"])}</div>
    <p class="settings-hint">Hybrid sends transcribed text to Groq for fast
      replies. Audio and files never leave this PC. Local-only keeps
      everything on this machine.</p>
    <div class="form-row"><label>Groq API key
      ${s.groq_key_set ? `(saved: ${esc(s.groq_key_masked)})` : ""}</label>
      <input id="set-groq_api_key" type="password" value=""
             placeholder="${s.groq_key_set ? "leave empty to keep the saved key"
                          : "gsk_... — free key at console.groq.com"}"></div>
    ${s.groq_key_set ? "" : `<p class="settings-hint">No key yet — hybrid mode
      is inactive. Get a free key at console.groq.com, paste it here, save.</p>`}
    <div class="form-row"><label>Cloud model</label>
      <input id="set-cloud_model" value="${esc(s.cloud_model || "")}"></div>
    <div class="form-row"><label>Cloud timeout (seconds)</label>
      <input id="set-cloud_timeout_s" type="number" step="0.5" min="2" max="30"
             value="${esc(s.cloud_timeout_s || 8)}"></div>

    <h4 class="settings-section">Privacy</h4>
    <ul class="settings-hint" style="margin:0 0 10px 18px">
      <li>Instant commands (open apps, screenshots…) never use any AI model.</li>
      <li>Hybrid mode sends only your transcribed/typed text and recent chat
          turns to Groq.</li>
      <li>Files, screenshots and raw microphone audio NEVER leave this PC.</li>
      <li>Clipboard text stays local unless you enable the toggle below.</li>
      <li>Local-only mode sends nothing anywhere, ever.</li>
    </ul>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-allow_clipboard_to_cloud"
             ${s.allow_clipboard_to_cloud ? "checked" : ""} style="width:auto">
      <label for="set-allow_clipboard_to_cloud" style="margin:0">
        Allow clipboard text to be summarized by the cloud brain (faster).
        Off = clipboard never leaves this PC.</label>
    </div>

    <h4 class="settings-section">Local model</h4>
    <div class="form-row"><label>Ollama URL</label>
      <input id="set-ollama_url" value="${esc(s.ollama_url || "")}"></div>
    <div class="form-row"><label>Model name</label>
      <input id="set-ollama_model" value="${esc(s.ollama_model || "")}"></div>
    <div class="form-row"><label>Chat model</label>
      <input id="set-chat_model" value="${esc(s.chat_model || s.ollama_model || "")}"></div>
    <p class="settings-hint">For faster chat on slower CPUs, run
      <code>ollama pull llama3.2:1b</code> and use <code>llama3.2:1b</code> here.
      Command planning stays on the main model.</p>
    <div class="form-row"><label>Model timeout (seconds)</label>
      <input id="set-ollama_timeout" type="number" min="5" max="120"
             value="${esc(s.ollama_timeout || 20)}"></div>
    <button class="ghost-btn" id="test-model">Test model</button>

    <h4 class="settings-section">Voice input (microphone &amp; speech-to-text)</h4>
    <div class="form-row"><label>Microphone</label>
      ${selectHtml("microphone_device", s.microphone_device || "",
        s.microphone_options || [{ id: "", label: "System default microphone" }])}</div>
    <p class="settings-hint">Pick a specific mic if you want, or leave this on the system default.</p>
    ${s.microphone_note ? `<p class="settings-hint">${esc(s.microphone_note)}</p>` : ""}
    <div class="form-row"><label>Whisper model (bigger = slower, more accurate)</label>
      ${selectHtml("faster_whisper_model", s.faster_whisper_model,
        ["tiny", "base", "base.en", "small", "small.en"])}</div>
    <p class="settings-hint">If Anna often mishears you, try small.en (slower but more accurate).</p>
    <div class="form-row"><label>Language</label>
      ${selectHtml("stt_language", s.stt_language, ["auto", "en", "hi", "mr"])}</div>
    <div class="form-row"><label>Silence timeout (seconds of quiet that ends a recording)</label>
      <input id="set-silence_seconds" type="number" step="0.1" min="0.5" max="5"
             value="${esc(s.silence_seconds)}"></div>
    <div class="form-row"><label>Max recording length (seconds)</label>
      <input id="set-max_record_seconds" type="number" min="3" max="60"
             value="${esc(s.max_record_seconds)}"></div>
    <button class="ghost-btn" id="test-mic">Test microphone</button>

    <h4 class="settings-section">Voice output (Anna's voice)</h4>
    <div class="form-row"><label>Voice engine</label>
      ${selectHtml("tts_backend", s.tts_backend,
        ["auto", "piper", "kokoro", "windows", "off"])}</div>

    <div class="voice-setup-card">
      <h5>Set up Piper</h5>
      <ol><li>Install the official runtime from
        <a href="https://github.com/OHF-Voice/piper1-gpl" target="_blank">OHF-Voice/piper1-gpl</a>
        with <code>pip install piper-tts</code>.</li>
      <li>Download a voice plus its matching <code>.onnx.json</code> config.
        The official docs support <code>python -m piper.download_voices en_US-lessac-medium</code>.</li></ol>
      <div class="form-row"><label>Piper executable (optional legacy fallback)</label><div class="file-row">
        <input id="set-piper_exe" value="${esc(s.piper_exe || "")}" placeholder="C:\\tools\\piper\\piper.exe">
        <button class="ghost-btn pick-voice-file" data-pick="piper_exe">Browse</button></div></div>
      <div class="form-row"><label>Piper voice model (.onnx; matching JSON auto-detected)</label><div class="file-row">
        <input id="set-piper_voice" value="${esc(s.piper_voice || "")}" placeholder="en_US-lessac-medium.onnx">
        <button class="ghost-btn pick-voice-file" data-pick="piper_voice">Browse</button></div></div>
      <div class="form-row"><label>Voice speed / Piper length scale (1.08 = relaxed)</label>
        <input id="set-piper_length_scale" type="number" step="0.01" min="0.5" max="2"
               value="${esc(s.piper_length_scale || 1.08)}"></div>
      <button class="ghost-btn" id="validate-piper">Validate Piper</button>
    </div>

    <div class="voice-setup-card">
      <h5>Optional: set up Kokoro</h5>
      <p>Install <code>kokoro-onnx</code>, then download
        <a href="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" target="_blank">kokoro-v1.0.onnx</a>
        and <a href="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" target="_blank">voices-v1.0.bin</a>.</p>
      <div class="form-row"><label>Kokoro model</label><div class="file-row">
        <input id="set-kokoro_model" value="${esc(s.kokoro_model || "")}" placeholder="kokoro-v1.0.onnx">
        <button class="ghost-btn pick-voice-file" data-pick="kokoro_model">Browse</button></div></div>
      <div class="form-row"><label>Kokoro voices</label><div class="file-row">
        <input id="set-kokoro_voices" value="${esc(s.kokoro_voices || "")}" placeholder="voices-v1.0.bin">
        <button class="ghost-btn pick-voice-file" data-pick="kokoro_voices">Browse</button></div></div>
      <div class="form-row"><label>Warm female voice</label>
        ${selectHtml("kokoro_voice", s.kokoro_voice || "af_heart", ["af_heart", "af_bella"])}</div>
      <button class="ghost-btn" id="validate-kokoro">Validate Kokoro</button>
    </div>

    <div class="form-row"><label>General voice speed (0.5 slow — 2.0 fast)</label>
      <input id="set-tts_rate" type="number" step="0.1" min="0.5" max="2"
             value="${esc(s.tts_rate)}"></div>
    <div class="form-row"><label>Volume (0–100, Windows voice only)</label>
      <input id="set-tts_volume" type="number" min="0" max="100"
             value="${esc(s.tts_volume)}"></div>
    <button class="ghost-btn" id="test-voice">Test voice</button>
    <p class="dim" style="margin:8px 0 0">"auto" prefers Piper when configured and
      falls back to the built-in Windows voice.</p>

    <div id="settings-status" class="dim" style="margin:12px 0"></div>
    <button class="primary-btn" id="settings-save">Save</button>`;
}

function collectSettings() {
  const val = (id) => $("#set-" + id)?.value;
  return {
    user_name: val("user_name"),
    animation_quality: val("animation_quality"),
    ollama_url: val("ollama_url"),
    ollama_model: val("ollama_model"),
    chat_model: val("chat_model"),
    ollama_timeout: parseInt(val("ollama_timeout"), 10) || 20,
    brain_mode: val("brain_mode"),
    cloud_model: val("cloud_model"),
    cloud_timeout_s: parseFloat(val("cloud_timeout_s")) || 8,
    allow_clipboard_to_cloud: !!$("#set-allow_clipboard_to_cloud")?.checked,
    hands_free_followup: !!$("#set-hands_free_followup")?.checked,
    // key only travels when the user actually typed one (never the mask)
    ...(val("groq_api_key") ? { groq_api_key: val("groq_api_key") } : {}),
    faster_whisper_model: val("faster_whisper_model"),
    microphone_device: val("microphone_device"),
    stt_language: val("stt_language"),
    silence_seconds: parseFloat(val("silence_seconds")) || 1.2,
    max_record_seconds: parseInt(val("max_record_seconds"), 10) || 8,
    tts_backend: val("tts_backend"),
    piper_exe: val("piper_exe"),
    piper_voice: val("piper_voice"),
    piper_length_scale: parseFloat(val("piper_length_scale")) || 1.08,
    kokoro_model: val("kokoro_model"),
    kokoro_voices: val("kokoro_voices"),
    kokoro_voice: val("kokoro_voice"),
    tts_rate: parseFloat(val("tts_rate")) || 1.0,
    tts_volume: parseInt(val("tts_volume"), 10),
  };
}

/* ------------------------------------------------------- event handlers */
const handlers = {
  state_change: (p) => setState(p.state, p.detail || ""),
  status: (p) => setChips(p.chips),
  user_message: (p) => renderEntry(p, "user"),
  anna_message: (p) => renderEntry(p, "anna"),
  action_result: (p) => renderEntry(p, "anna"),

  confirm_request: (p) => appendMessage(confirmCardHtml(p), "anna"),
  confirm_resolved: () => {
    document.querySelectorAll(".confirm-card:not(.resolved)")
      .forEach(c => c.classList.add("resolved"));
  },

  toggle_sync: (p) => {
    const el = p.name === "wake_word" ? $("#toggle-wake") : $("#toggle-voice");
    if (el) el.checked = !!p.value;
  },

  setup_card: (p) => {
    const issues = (p && p.issues) || [];
    $("#setup-card").classList.toggle("hidden", issues.length === 0);
    $("#setup-issues").innerHTML = issues.map(i => `<li>${esc(i)}</li>`).join("");
  },

  devlog: (p) => appendDevlog(p),
  latency: (p) => {
    const last = String(p.summary || "").split("\n").pop();
    $("#dev-latency").textContent = last;
  },

  mic: () => {},  // covered by state_change; kept for protocol completeness

  prefs: (p) => setAnimQuality(p.animation_quality),

  clear_conversation: () => { messagesEl().innerHTML = ""; },

  history: (p) => openModal("Command history", historyHtml(p.rows)),
  settings: (p) => {
    openModal("Settings", settingsHtml(p));
    $("#settings-save").addEventListener("click", () => {
      call("save_settings", collectSettings());
      closeModal();
    });
    // Test buttons save the current form first so tests use what you typed.
    $("#test-model").addEventListener("click", () => {
      call("save_settings", collectSettings()).then(() => call("test_model"));
    });
    $("#test-mic").addEventListener("click", () => {
      call("save_settings", collectSettings()).then(() => call("test_microphone"));
    });
    $("#test-voice").addEventListener("click", () => {
      call("save_settings", collectSettings()).then(() => call("test_voice"));
    });
    document.querySelectorAll(".pick-voice-file").forEach(button => {
      button.addEventListener("click", () => {
        const kind = button.dataset.pick;
        call("pick_voice_file", kind).then(path => {
          const input = $("#set-" + kind);
          if (input && path) input.value = path;
        });
      });
    });
    $("#validate-piper").addEventListener("click", () => {
      call("save_settings", collectSettings()).then(() => call("validate_piper"));
    });
    $("#validate-kokoro").addEventListener("click", () => {
      call("save_settings", collectSettings()).then(() => call("validate_kokoro"));
    });
  },

  test_result: (p) => {
    const status = $("#settings-status");
    if (status) {
      status.textContent = `${p.ok ? "✅" : "⚠"} ${p.message}`;
      status.style.color = p.ok ? "var(--success)" : "var(--warn)";
    }
  },

  full_state: (p) => {
    setState(p.state || "ready");
    setChips(p.chips || {});
    if (p.prefs) setAnimQuality(p.prefs.animation_quality);
    if (p.toggles) {
      $("#toggle-wake").checked = !!p.toggles.wake_word;
      $("#toggle-voice").checked = !!p.toggles.voice;
    }
    if (p.hotkey) {
      const pretty = p.hotkey.split("+").map(s =>
        s.trim().replace(/^./, c => c.toUpperCase())).join(" + ");
      $("#hint-hotkey").textContent = pretty;
    }
    messagesEl().innerHTML = "";
    (p.conversation || []).forEach(m => renderEntry(m, m.role));
    if (p.pending) appendMessage(confirmCardHtml(p.pending), "anna");
    $("#dev-log").innerHTML = "";
    (p.devlog || []).forEach(appendDevlog);
    messagesEl().scrollTop = messagesEl().scrollHeight;
  },
};

window.ui = {
  dispatch(event) {
    try {
      const handler = handlers[event.type];
      if (handler) handler(event.payload || {});
    } catch (err) {
      console.error("dispatch failed", event, err);
    }
  },
};

/* ---------------------------------------------------------------- wiring */
function sendInput() {
  const input = $("#cmd-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  call("send_text", text);
}

document.addEventListener("DOMContentLoaded", () => {
  // avatar drop-in slot: use assets/avatar.png when present, else the
  // procedural particle sphere (swapping the image in needs zero code changes)
  const avatar = $("#avatar");
  avatar.addEventListener("load", () => {
    avatarLoaded = true;
    avatar.style.display = "block";
    syncCanvases();
  });
  setAnimQuality("medium");   // default until full_state arrives

  $("#send-btn").addEventListener("click", sendInput);
  $("#cmd-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendInput();
  });

  $("#mic-btn").addEventListener("click", () => {
    call(currentState === "listening" ? "stop_ptt" : "start_ptt");
  });

  $("#toggle-wake").addEventListener("change", (e) =>
    call("set_toggle", "wake_word", e.target.checked));
  $("#toggle-voice").addEventListener("change", (e) =>
    call("set_toggle", "voice", e.target.checked));

  $("#clear-btn").addEventListener("click", () => call("clear_history"));
  $("#recheck-btn").addEventListener("click", () => call("recheck"));

  $("#settings-btn").addEventListener("click", () => call("open_settings"));
  $("#history-btn").addEventListener("click", () => call("get_history", 0)
    .then(rows => handlers.history({ rows })));
  $("#shortcuts-btn").addEventListener("click", () =>
    openModal("Keyboard shortcuts", shortcutsHtml()));

  document.querySelectorAll(".nav-item").forEach(item => {
    item.addEventListener("click", () => {
      const nav = item.dataset.nav;
      if (nav === "settings") call("open_settings");
      else if (nav === "conversations") call("get_history", 0)
        .then(rows => handlers.history({ rows }));
      // Skills / Automations / Memory are clean placeholders for now
    });
  });

  $("#chip-brain").addEventListener("click", () => {
    call("get_brain_info").then((info) => {
      if (!info) return;
      const rows = (info.calls || []).slice().reverse().map(c => `<tr>
          <td class="dim">${esc(c.ts)}</td><td>${esc(c.provider)}</td>
          <td>${esc(c.kind)}</td><td>${c.latency_ms} ms</td>
          <td>${c.error ? "⚠ " + esc(c.error) : (c.failover ? "failover" : "✅")}</td>
        </tr>`).join("");
      openModal("Brain", `
        <p>Mode: <b>${esc(info.mode)}</b> · Cloud model:
           <b>${esc(info.cloud_model || "—")}</b> · Circuit:
           <b>${esc(info.circuit)}</b></p>
        <table><tr><th>When</th><th>Provider</th><th>Kind</th><th>Latency</th>
          <th></th></tr>${rows || ""}</table>
        ${rows ? "" : "<p class='dim'>No LLM calls yet this session.</p>"}`);
    });
  });

  $("#devtools-btn").addEventListener("click", () =>
    $("#devtools").classList.toggle("hidden"));
  $("#dev-close").addEventListener("click", () =>
    $("#devtools").classList.add("hidden"));
  $("#dev-clear").addEventListener("click", () => {
    $("#dev-log").innerHTML = "";
  });

  $("#modal-close").addEventListener("click", closeModal);
  $("#modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
  });

  // delegated clicks: confirm cards + result-card View buttons
  document.body.addEventListener("click", (e) => {
    const confirmBtn = e.target.closest("[data-confirm]");
    if (confirmBtn) {
      call("confirm", parseInt(confirmBtn.dataset.confirm, 10),
           confirmBtn.dataset.approved === "1");
      confirmBtn.closest(".confirm-card").classList.add("resolved");
      return;
    }
    const viewBtn = e.target.closest("[data-open-path]");
    if (viewBtn) call("open_path", viewBtn.dataset.openPath);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      $("#devtools").classList.add("hidden");
    }
    if (e.ctrlKey && e.key.toLowerCase() === "l") {
      e.preventDefault();
      $("#cmd-input").focus();
    }
  });
});

/* handshake: tell Python the dispatcher exists, receive full_state */
window.addEventListener("pywebviewready", () => call("ready"));
if (window.pywebview && window.pywebview.api) call("ready");
