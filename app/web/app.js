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
  // Screenshot: structured payload with an inline thumbnail + actions (9.1D).
  if (data && typeof data === "object" && data.type === "screenshot") {
    const p = esc(data.full_path || "");
    const thumb = data.thumb_data_url
      ? `<img class="shot-thumb" src="${esc(data.thumb_data_url)}" alt="screenshot"
             data-open-path="${p}">`
      : `<div class="shot-thumb shot-thumb-none">no preview</div>`;
    return `<div class="result-card shot-card">
              <div class="shot-head"><span class="ok">✅</span>
                <span class="grow">Screenshot · ${esc(data.timestamp || "")}</span></div>
              ${thumb}
              <div class="shot-actions">
                <button class="ghost-btn" data-open-path="${p}">View ↗</button>
                <button class="ghost-btn" data-copy-img="${p}">Copy</button>
                <button class="ghost-btn" data-save-img="${p}">Save as…</button>
                <button class="ghost-btn" data-reveal="${p}">Open folder</button>
              </div>
            </div>`;
  }
  // Generic result with an openable path (older payloads used a plain string).
  const pathStr = typeof data === "string" ? data
    : (data && typeof data === "object" ? data.full_path : "");
  const viewable = typeof pathStr === "string" && pathStr.length > 0;
  return `<div class="result-card">
            <span class="ok">✅</span>
            <span class="grow">${viewable ? esc(pathStr) : esc(action.intent || "done")}</span>
            ${viewable ? `<button class="ghost-btn" data-open-path="${esc(pathStr)}">View ↗</button>` : ""}
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

/* --------------------------------------------------- camera (11B, Mira) */
let cameraStream = null;

function stopCameraTracks() {
  if (!cameraStream) return;
  cameraStream.getTracks().forEach(t => t.stop());   // camera light goes out
  cameraStream = null;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* True when the canvas holds a single flat colour — a webcam that hasn't
   warmed up yet returns pure black, and drawing the very first available
   frame reliably captures exactly that. Sample a small grid; we only need to
   know whether *anything* varies. */
function canvasIsBlank(canvas) {
  const probe = document.createElement("canvas");
  probe.width = probe.height = 32;
  probe.getContext("2d").drawImage(canvas, 0, 0, 32, 32);
  const px = probe.getContext("2d").getImageData(0, 0, 32, 32).data;
  let min = 255, max = 0;
  for (let i = 0; i < px.length; i += 4) {
    const lum = (px[i] + px[i + 1] + px[i + 2]) / 3;
    if (lum < min) min = lum;
    if (lum > max) max = lum;
  }
  return max - min < 3;
}

/* Wait for a frame the compositor has actually decoded, not just "metadata
   is ready". requestVideoFrameCallback fires on a real presented frame. */
function nextVideoFrame(video) {
  if (typeof video.requestVideoFrameCallback === "function") {
    return new Promise(r => video.requestVideoFrameCallback(() => r()));
  }
  return new Promise(r => requestAnimationFrame(() => r()));
}

/* Open the camera, show a live self-preview, grab exactly one GOOD frame,
   stop it. The stream never outlives this function. `device` selects a
   specific webcam; `preview` shows the small self-view. */
async function captureCameraFrame(requestId, device, preview) {
  let dataUrl = "";
  const box = $("#camera-preview");
  const shown = preview !== false && box;
  try {
    const constraints = { video: device ? { deviceId: { ideal: device } } : true };
    cameraStream = await navigator.mediaDevices.getUserMedia(constraints);
    // Reuse the on-screen preview <video> as the capture source when showing
    // the self-view, else a detached element.
    const video = (shown && $("#camera-preview-video"))
      || document.createElement("video");
    video.srcObject = cameraStream;
    video.muted = true;
    video.playsInline = true;
    if (shown) box.classList.remove("hidden");
    await video.play();
    if (video.readyState < 2) {
      await new Promise(r => (video.onloadeddata = r));
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    const ctx = canvas.getContext("2d");

    // External/USB webcams can emit black for 1-3s after opening (auto-exposure
    // ramp). Give it a short warm-up, then keep pulling frames until one has
    // real content, up to ~6s. Only fall back to a black frame if it never
    // produces one — the Python side then retries with a fresh open.
    await sleep(350);
    const deadline = Date.now() + 6000;
    do {
      await nextVideoFrame(video);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      if (!canvasIsBlank(canvas)) break;
      await sleep(120);
    } while (Date.now() < deadline);

    dataUrl = canvas.toDataURL("image/jpeg", 0.85);
    // Linger a moment so you actually SEE the self-view, then tear it down.
    if (shown) await sleep(1000);
    video.pause();
    if (!shown) video.srcObject = null;
  } catch (err) {
    console.error("camera capture failed", err);
  } finally {
    if (box) box.classList.add("hidden");
    stopCameraTracks();          // ALWAYS — even if the draw threw
  }
  call("camera_frame", requestId, dataUrl);
}

/* Populate the Settings camera dropdown. Labels are only visible after camera
   permission has been granted once (a browser privacy rule). */
async function loadCameraDevices(selected) {
  const sel = $("#set-camera_device");
  if (!sel || !navigator.mediaDevices?.enumerateDevices) return;
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cams = devices.filter(d => d.kind === "videoinput");
    const opts = [`<option value="">Default camera</option>`];
    cams.forEach((c, i) => {
      const label = c.label || `Camera ${i + 1}`;
      const on = c.deviceId === selected ? "selected" : "";
      opts.push(`<option value="${esc(c.deviceId)}" ${on}>${esc(label)}</option>`);
    });
    sel.innerHTML = opts.join("");
  } catch (err) { /* enumerate can fail before permission — leave default */ }
}

let lastConfirm = null;   // payload of the card currently on screen (11A)

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
  // 11A: destructive-tier actions need the strong phrase; say so plainly.
  const phrase = p.strong_required
    ? `Say <b>“Anna approve”</b> or <b>“cancel”</b> — a plain “yes” won't do for this one.`
    : `You can also say <b>“approve”</b> or <b>“cancel”</b>.`;
  // 11C: name exactly what will be clicked, and show a picture of it when the
  // target was only guessed from pixels.
  const t = p.target;
  const targetHtml = !t ? "" : `
    <div class="confirm-target">
      <b>${esc(t.control_type || "control")} “${esc(t.name || "?")}”</b>
      <span class="dim">· ${esc(t.window_title || t.app || "foreground")}
      · via ${esc(t.backend)} · ${Math.round((t.confidence ?? 1) * 100)}% sure</span>
      ${t.confidence < 1 ? `<div class="confirm-guess">⚠ I had to guess this
         from what I can see. Check the picture below.</div>` : ""}
      ${t.crop_data_url ? `<img class="confirm-crop" src="${t.crop_data_url}" alt="target">` : ""}
    </div>`;
  return `<div class="confirm-card${p.strong_required ? " strong" : ""}" data-confirm-id="${esc(p.id)}">
            <h4>⚠ Approval required <span class="dim">· risk: ${esc(p.risk || "?")}</span></h4>
            <code>${esc(p.tool)} ${esc(args)}</code>
            ${targetHtml}
            <div class="confirm-msg">${esc(p.message || "Do you want me to go ahead?")}</div>
            <div class="confirm-hint">${phrase}</div>
            <div class="confirm-details hidden"></div>
            <div class="confirm-actions">
              <button class="ghost-btn approve" data-confirm="${esc(p.id)}" data-approved="1"
                      style="border-color:rgba(52,211,153,.5);color:#a7f3d0">Run it</button>
              <button class="ghost-btn" data-confirm="${esc(p.id)}" data-approved="0">Cancel</button>
              <button class="ghost-btn details-btn" data-details="${esc(p.id)}">Show details</button>
            </div>
          </div>`;
}

/* 11A: expand a pending card with the full target/argument detail. Called by
   the "Show details" button and by the voice phrase "show details". */
function expandConfirmDetails(p) {
  const card = document.querySelector(
    `.confirm-card[data-confirm-id="${CSS.escape(String(p.id))}"]`);
  if (!card) return;
  const box = card.querySelector(".confirm-details");
  if (!box) return;
  const rows = Object.entries(p.arguments || {})
    .map(([k, v]) => `<tr><td class="dim">${esc(k)}</td><td>${esc(String(v))}</td></tr>`)
    .join("");
  const extra = Object.entries((p.details || {}))
    .map(([k, v]) => `<tr><td class="dim">${esc(k)}</td><td>${esc(String(v))}</td></tr>`)
    .join("");
  box.innerHTML = `
    <table>
      <tr><td class="dim">heard</td><td>${esc(p.transcript || "")}</td></tr>
      <tr><td class="dim">tool</td><td>${esc(p.tool || "")}</td></tr>
      <tr><td class="dim">risk</td><td>${esc(p.risk || "?")}</td></tr>
      ${rows}${extra}
    </table>`;
  box.classList.remove("hidden");
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
  for (const key of ["engine", "brain", "model", "mic", "voice"]) {
    const chip = $("#chip-" + key);
    const data = chips && chips[key];
    if (!chip || !data) continue;
    chip.querySelector(".chip-label").textContent = data.label || "";
    chip.dataset.state = data.state || "ok";
  }
  // STT chip is dynamic: shown only when streaming mode is chosen (9.1B).
  const sttChip = $("#chip-stt");
  const sttData = chips && chips.stt;
  if (sttChip) {
    sttChip.classList.toggle("hidden", !sttData);
    if (sttData) {
      sttChip.querySelector(".chip-label").textContent = sttData.label || "";
      sttChip.dataset.state = sttData.state || "ok";
    }
  }
}

/* ------------------------------------------- animation quality + canvases */
let animQuality = "medium";
let avatarLoaded = false;
let avatarRAF = 0, haloRAF = 0;
let annaLevel = 0, annaLevelTarget = 0;   // TTS amplitude -> orb reactivity (9D)

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
    // ease the orb toward the live audio level; decays to rest in pauses
    annaLevel += (annaLevelTarget - annaLevel) * 0.25;
    annaLevelTarget *= 0.9;
    const pulse = 1 + annaLevel * 0.22;        // expand with voice amplitude
    const glow = annaLevel * 0.35;             // brighten with voice amplitude
    const sin = Math.sin(angle), cos = Math.cos(angle);
    ctx.clearRect(0, 0, size, size);
    for (let i = 0; i < N; i++) {
      const p = pts[i];
      const x = p.x * cos - p.z * sin;
      const z = p.x * sin + p.z * cos;
      const depth = (z + 1) / 2;          // 0 back .. 1 front
      const px = C + x * R * pulse * (0.85 + depth * 0.15);
      const py = C + p.y * R * pulse * 0.92 + Math.sin(angle * 2 + i) * 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 0.6 + depth * 1.5, 0, 6.2832);
      ctx.fillStyle = `rgba(${colors[i % colors.length]},${Math.min(1, 0.12 + depth * 0.55 + glow)})`;
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

    <h4 class="settings-section">Conversation engine</h4>
    <div class="form-row"><label>Engine for voice conversations</label>
      ${selectHtml("engine_mode", s.engine_mode || "pipeline", [
        { value: "pipeline", label: "Pipeline — reliable default (speech-to-text → brain → voice)" },
        { value: "gemini_live", label: "Gemini Live — premium: native speech-to-speech (cloud, metered)" },
        { value: "local", label: "Local — everything on this PC, works offline" }])}</div>
    <ul class="settings-hint" style="margin:0 0 10px 18px">
      <li><b>Pipeline</b>: ~1.5–2.5s per turn, layered cloud/local fallback,
          audio leaves only if you chose streaming STT / Aura voice.</li>
      <li><b>Gemini Live</b>: sub-second, emotion-aware voice with natural
          interruptions — but streams your microphone to Google continuously
          while the mic is open, and bills per audio minute. Falls back to
          the pipeline automatically on any failure.</li>
      <li><b>Local</b>: Whisper → Ollama → Piper. Slower, fully offline,
          nothing ever leaves this PC.</li>
    </ul>
    ${s.engine_mode === "gemini_live" && s.live_state_reason ? `<p class="settings-hint" style="color:var(--warn)">
      ⚠ Live not active: ${esc(s.live_state_reason)}</p>` : ""}
    <div class="form-row"><label>Gemini API key
      ${s.gemini_key_set ? `(saved: ${esc(s.gemini_key_masked)})` : ""}</label>
      <input id="set-gemini_api_key" type="password" value=""
             placeholder="${s.gemini_key_set ? "leave empty to keep the saved key"
                          : "key from aistudio.google.com (AIza... or AQ...)"}"></div>
    <div class="form-row"><label>Gemini Live model (preview tier — may change)</label>
      <input id="set-gemini_live_model" value="${esc(s.gemini_live_model || "")}"></div>
    <div class="form-row"><label>Live voice (HD)</label>
      ${selectHtml("gemini_live_voice", s.gemini_live_voice || "Sulafat", [
        { value: "Sulafat", label: "Sulafat — warm (default)" },
        { value: "Aoede", label: "Aoede — breezy" },
        { value: "Leda", label: "Leda — youthful" },
        { value: "Vindemiatrix", label: "Vindemiatrix — gentle" },
        { value: "Achernar", label: "Achernar — soft" },
        { value: "Autonoe", label: "Autonoe — bright" },
        { value: "Zephyr", label: "Zephyr — bright" },
        { value: "Kore", label: "Kore — firm" },
        { value: "Callirrhoe", label: "Callirrhoe — easy-going" },
        { value: "Despina", label: "Despina — smooth" }])}</div>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-live_affective_dialog"
             ${s.live_affective_dialog !== false ? "checked" : ""} style="width:auto">
      <label for="set-live_affective_dialog" style="margin:0">
        Emotion-aware dialog where the model supports it (2.5 Live models;
        the 3.1 voice is already emotion-aware natively).</label>
    </div>
    <div class="form-row"><label>Live pricing — $/min audio in (editable; pricing changes)</label>
      <input id="set-live_price_in_per_min" type="number" step="0.001" min="0"
             value="${esc(s.live_price_in_per_min ?? 0.005)}"></div>
    <div class="form-row"><label>Live pricing — $/min audio out</label>
      <input id="set-live_price_out_per_min" type="number" step="0.001" min="0"
             value="${esc(s.live_price_out_per_min ?? 0.018)}"></div>
    <div class="form-row"><label>Auto-close an idle Live session after (seconds, 0 = never)</label>
      <input id="set-live_idle_close_s" type="number" min="0" max="600"
             value="${esc(s.live_idle_close_s ?? 60)}"></div>
    <div class="form-row"><label>Monthly soft cap in $ (warn only, never blocks; 0 = off)</label>
      <input id="set-live_monthly_cap_usd" type="number" step="0.5" min="0"
             value="${esc(s.live_monthly_cap_usd ?? 0)}"></div>
    <p class="settings-hint">Estimated Gemini Live spend this month:
      <b>$${Number(s.live_month_spend || 0).toFixed(2)}</b> (local estimate from
      session audio minutes — check Google's billing console for the real number).</p>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-live_audio_consent"
             ${s.live_audio_consent ? "checked" : ""} style="width:auto">
      <label for="set-live_audio_consent" style="margin:0">
        I understand Gemini Live streams my microphone audio to Google
        continuously while the mic is open, and is billed per audio minute.
        Without this, Anna stays on the pipeline.</label>
    </div>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-engine_rules_first"
             ${s.engine_rules_first !== false ? "checked" : ""} style="width:auto">
      <label for="set-engine_rules_first" style="margin:0">
        Instant commands stay local (recommended): "open paint", screenshots
        etc. run through Anna's local rules in every engine — instant, offline,
        no cloud round-trip.</label>
    </div>

    <h4 class="settings-section">Vision (screen &amp; camera)</h4>
    <ul class="settings-hint" style="margin:0 0 10px 18px">
      <li>Anna never watches silently. A single frame is captured only when you
          ask ("look at my screen", "read this error", "what do you see").</li>
      <li>"Watch my screen" takes <b>one frame every couple of seconds</b> —
          each read once and thrown away. It is never a video stream, shows a
          badge the whole time, and stops itself when idle.</li>
      <li>The camera opens for <b>exactly one frame</b>, then stops. No
          recording, no face recognition.</li>
      <li>Say <b>"privacy mode"</b> to stop screen watching, the camera, and any
          Live audio session at once.</li>
      <li>Screens that look like they hold passwords, keys or banking details
          are <b>not analyzed at all</b> until you explicitly allow it.</li>
    </ul>
    <p class="settings-hint" style="color:${s.ocr_ready ? "var(--success)" : "var(--warn)"}">
      ${s.ocr_ready ? "✅ Local OCR ready — Anna reads screen text on this PC."
        : "⚠ No local OCR. Install Tesseract (winget install UB-Mannheim.TesseractOCR, then pip install pytesseract) so Anna can read text without any cloud."}</p>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-cloud_vision_consent"
             ${s.cloud_vision_consent ? "checked" : ""} style="width:auto">
      <label for="set-cloud_vision_consent" style="margin:0">
        Allow screen/camera <b>frames to be sent to Gemini</b> for a deeper
        description. Off = local OCR only, images never leave this PC.</label>
    </div>
    <div class="form-row"><label>Cloud vision model (preview tier — may change)</label>
      <input id="set-vision_cloud_model" value="${esc(s.vision_cloud_model || "")}"></div>
    <div class="form-row"><label>Camera</label>
      <select id="set-camera_device"><option value="">Default camera</option></select></div>
    <p class="settings-hint">Camera names appear after you've used the camera
      once (a browser privacy rule).</p>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-camera_preview"
             ${s.camera_preview !== false ? "checked" : ""} style="width:auto">
      <label for="set-camera_preview" style="margin:0">
        Show a live self-view while the camera is on, so you can see what it
        captures.</label>
    </div>
    <div class="form-row"><label>Watching mode: seconds between frames</label>
      <input id="set-screen_watch_interval_s" type="number" step="0.5" min="0.5" max="30"
             value="${esc(s.screen_watch_interval_s ?? 1.5)}"></div>
    <div class="form-row"><label>Watching mode: auto-stop after idle (seconds)</label>
      <input id="set-screen_watch_idle_timeout_s" type="number" min="10" max="3600"
             value="${esc(s.screen_watch_idle_timeout_s ?? 120)}"></div>
    <div class="form-row" style="flex-direction:row;align-items:center;gap:10px">
      <input type="checkbox" id="set-vision_save_captures"
             ${s.vision_save_captures ? "checked" : ""} style="width:auto">
      <label for="set-vision_save_captures" style="margin:0">
        Save every capture to disk. Off = frames are processed once and
        discarded (only the extracted text is kept).</label>
    </div>
    <button class="ghost-btn" id="privacy-mode-btn">🔒 Privacy mode — stop all capture now</button>

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
      <li>Files, screenshots and batch recordings NEVER leave this PC.</li>
      <li>Streaming speech (below) sends live mic audio to Deepgram while the
          mic is open; local mode keeps all audio on-device.</li>
      <li>Deepgram voice (if chosen) sends Anna's reply text to Deepgram to
          synthesize speech; Piper/Kokoro voices synthesize fully on this PC.</li>
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
    ${s.stt_mode === "streaming" ? `<p class="settings-hint" style="color:${
      s.stt_stream_state === "streaming" ? "var(--success)" : "var(--warn)"}">
      ${s.stt_stream_state === "streaming" ? "✅" : "⚠"} ${esc(s.stt_stream_reason || "")}</p>` : ""}
    <div class="form-row"><label>Speech recognition</label>
      ${selectHtml("stt_mode", s.stt_mode, ["streaming", "local"])}</div>
    <p class="settings-hint">Streaming (Deepgram) is much faster — a final
      transcript ~0.3s after you stop, vs several seconds locally — but sends
      live mic audio to Deepgram while the mic is open. Local (Whisper) keeps
      all audio on this PC and is the automatic fallback if streaming fails.</p>
    <div class="form-row"><label>Deepgram API key
      ${s.deepgram_key_set ? `(saved: ${esc(s.deepgram_key_masked)})` : ""}</label>
      <input id="set-deepgram_api_key" type="password" value=""
             placeholder="${s.deepgram_key_set ? "leave empty to keep the saved key"
                          : "needed for streaming — free key at deepgram.com"}"></div>
    ${s.deepgram_key_set ? "" : `<p class="settings-hint">No key yet — streaming
      is inactive and Anna uses local Whisper. Get a free key at deepgram.com.</p>`}
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
        ["auto", "piper", "kokoro", "deepgram", "windows", "off"])}</div>
    <p class="settings-hint">Piper and Kokoro synthesize fully on this PC (the
      privacy default). <b>Deepgram (Aura)</b> is a warm cloud voice with very
      low latency, but sends Anna's reply text to Deepgram to synthesize.</p>

    <div class="voice-setup-card">
      <h5>Deepgram Aura (cloud voice)</h5>
      <p class="settings-hint">Uses your Deepgram key (same one as streaming
        speech). Sends Anna's reply text to Deepgram; nothing else.</p>
      <div class="form-row"><label>Aura voice</label>
        ${selectHtml("tts_deepgram_model", s.tts_deepgram_model || "aura-2-delia-en",
          ["aura-2-delia-en", "aura-2-luna-en", "aura-asteria-en", "aura-luna-en"])}</div>
      <button class="ghost-btn" id="validate-deepgram-tts">Validate Deepgram voice</button>
    </div>

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
    cloud_vision_consent: !!$("#set-cloud_vision_consent")?.checked,
    vision_cloud_model: val("vision_cloud_model"),
    camera_device: val("camera_device") || "",
    camera_preview: !!$("#set-camera_preview")?.checked,
    screen_watch_interval_s: parseFloat(val("screen_watch_interval_s")) || 1.5,
    screen_watch_idle_timeout_s: parseFloat(val("screen_watch_idle_timeout_s")) || 120,
    vision_save_captures: !!$("#set-vision_save_captures")?.checked,
    engine_mode: val("engine_mode"),
    gemini_live_model: val("gemini_live_model"),
    gemini_live_voice: val("gemini_live_voice"),
    live_audio_consent: !!$("#set-live_audio_consent")?.checked,
    engine_rules_first: !!$("#set-engine_rules_first")?.checked,
    live_affective_dialog: !!$("#set-live_affective_dialog")?.checked,
    live_price_in_per_min: parseFloat(val("live_price_in_per_min")) || 0.005,
    live_price_out_per_min: parseFloat(val("live_price_out_per_min")) || 0.018,
    live_idle_close_s: Math.max(0, parseFloat(val("live_idle_close_s")) || 0),
    live_monthly_cap_usd: Math.max(0, parseFloat(val("live_monthly_cap_usd")) || 0),
    ...(val("gemini_api_key") ? { gemini_api_key: val("gemini_api_key") } : {}),
    stt_mode: val("stt_mode"),
    ...(val("deepgram_api_key") ? { deepgram_api_key: val("deepgram_api_key") } : {}),
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
    tts_deepgram_model: val("tts_deepgram_model"),
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

  confirm_request: (p) => {
    lastConfirm = p;
    appendMessage(confirmCardHtml(p), "anna");
  },
  // 11A: "show details" (voice) expands the same card the button expands.
  confirm_details: (p) => {
    lastConfirm = p;
    expandConfirmDetails(p);
  },
  confirm_resolved: () => {
    lastConfirm = null;
    document.querySelectorAll(".confirm-card:not(.resolved)")
      .forEach(c => c.classList.add("resolved"));
  },

  toggle_sync: (p) => {
    const map = { wake_word: "#toggle-wake", voice: "#toggle-voice",
                  hands_free: "#toggle-hands-free" };
    const el = $(map[p.name]);
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

  // Streaming STT (9A): unmistakable indication that live mic audio is
  // leaving the machine, plus the live interim transcript.
  // Gemini Live (10C): unmistakable indication that continuous mic audio
  // is streaming to Google for the whole conversation.
  live_streaming: (p) => {
    const on = !!(p && p.active);
    document.body.classList.toggle("live-streaming", on);
    const badge = $("#live-badge");
    if (badge) {
      badge.classList.toggle("hidden", !on);
      if (!on) badge.textContent = "● Live — audio streaming to Google";
    }
  },

  // 11B: persistent indicators. Screen watching = cyan badge + border for
  // its whole life. Camera = red badge, on for exactly one frame.
  screen_vision: (p) => {
    const on = !!(p && p.active);
    document.body.classList.toggle("screen-vision", on);
    const badge = $("#screen-vision-badge");
    if (badge) badge.classList.toggle("hidden", !on);
  },

  camera_active: (p) => {
    const on = !!(p && p.active);
    document.body.classList.toggle("camera-on", on);
    const badge = $("#camera-badge");
    if (badge) badge.classList.toggle("hidden", !on);
  },

  privacy_mode: () => {
    document.body.classList.remove("screen-vision", "camera-on", "live-streaming");
    ["#screen-vision-badge", "#camera-badge", "#live-badge"]
      .forEach(sel => $(sel) && $(sel).classList.add("hidden"));
    stopCameraTracks();
  },

  // Mira's pattern: open getUserMedia, draw ONE frame, stop every track
  // immediately. No recording, nothing retained.
  camera_capture: (p) => captureCameraFrame(p && p.id, p && p.device,
                                            p && p.preview),
  camera_stop: () => stopCameraTracks(),

  // 10D.2: running session cost estimate in the Live badge + dev tools.
  live_cost: (p) => {
    const badge = $("#live-badge");
    if (badge && p && !badge.classList.contains("hidden")) {
      const mins = ((p.in_s || 0) + (p.out_s || 0)) / 60;
      badge.textContent = `● Live — audio streaming to Google · ` +
        `${mins.toFixed(1)} min · ~$${(p.usd || 0).toFixed(3)}`;
    }
  },

  // 10D.1: first-run consent card — picking the engine is NOT consent.
  live_consent: (p) => {
    openModal("Enable Gemini Live?", `
      <p>Gemini Live is Anna's premium voice engine — instant, natural
      speech-to-speech. Before turning it on, know exactly what changes:</p>
      <ul class="settings-hint" style="margin:10px 0 10px 18px">
        <li><b>Your microphone streams to Google continuously</b> while the
            mic is open — the whole conversation, not just single commands.</li>
        <li><b>It's metered</b>: roughly $${(p && p.price_in || 0.005).toFixed(3)}/min
            of your audio in and $${(p && p.price_out || 0.018).toFixed(3)}/min of
            Anna's audio out (editable in Settings). The running cost shows
            on screen, and idle sessions auto-close after
            ${Math.round(p && p.idle_s || 60)}s.</li>
        <li><b>Actions stay leashed</b>: every tool call is validated by
            Anna's local safety rules and confirmations, same as always.</li>
        <li><b>Pipeline and Local modes are unchanged</b> and keep audio
            on-device — you can switch back anytime.</li>
      </ul>
      <div style="display:flex;gap:10px;margin-top:14px">
        <button class="primary-btn" id="live-consent-yes">I understand — enable Live</button>
        <button class="ghost-btn" id="live-consent-no">Not now</button>
      </div>`);
    $("#live-consent-yes").addEventListener("click", () => {
      call("live_consent", true); closeModal();
    });
    $("#live-consent-no").addEventListener("click", () => {
      call("live_consent", false); closeModal();
    });
  },

  stt_streaming: (p) => {
    const on = !!(p && p.active);
    document.body.classList.toggle("stt-streaming", on);
    const badge = $("#stt-streaming-badge");
    if (badge) badge.classList.toggle("hidden", !on);
    if (!on) { const i = $("#stt-interim"); if (i) i.textContent = ""; }
  },
  stt_interim: (p) => {
    const el = $("#stt-interim");
    if (el) el.textContent = (p && p.text) || "";
  },

  // Continuous hands-free (9C): always-visible "conversation mode" state.
  hands_free: (p) => {
    const on = !!(p && p.active);
    document.body.classList.toggle("hands-free", on);
    const badge = $("#hands-free-badge");
    if (badge) badge.classList.toggle("hidden", !on);
    const toggle = $("#toggle-hands-free");
    if (toggle) toggle.checked = on;
  },

  // Audio-reactive orb (9D): TTS amplitude envelope pulses the sphere.
  speaking_level: (p) => { annaLevelTarget = Math.max(annaLevelTarget, (p && p.level) || 0); },
  turn_latency: () => {},   // consolidated metric lives in Developer Tools

  prefs: (p) => setAnimQuality(p.animation_quality),

  clear_conversation: () => { messagesEl().innerHTML = ""; },

  history: (p) => openModal("Command history", historyHtml(p.rows)),
  settings: (p) => {
    openModal("Settings", settingsHtml(p));
    loadCameraDevices(p.camera_device || "");   // 11B: list webcams
    $("#settings-save").addEventListener("click", () => {
      call("save_settings", collectSettings());
      closeModal();
    });
    // 11B kill switch — must work without saving anything first.
    $("#privacy-mode-btn").addEventListener("click", () => {
      call("privacy_mode");
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
    const vdg = $("#validate-deepgram-tts");
    if (vdg) vdg.addEventListener("click", () => {
      call("save_settings", collectSettings()).then(() => call("validate_deepgram_tts"));
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
      handlers.hands_free({ active: !!p.toggles.hands_free });
    }
    if (p.hotkey) {
      const pretty = p.hotkey.split("+").map(s =>
        s.trim().replace(/^./, c => c.toUpperCase())).join(" + ");
      $("#hint-hotkey").textContent = pretty;
    }
    messagesEl().innerHTML = "";
    (p.conversation || []).forEach(m => renderEntry(m, m.role));
    lastConfirm = p.pending || null;
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
  $("#toggle-hands-free").addEventListener("change", (e) =>
    call("set_toggle", "hands_free", e.target.checked));

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
    const detailsBtn = e.target.closest("[data-details]");
    if (detailsBtn) {                       // 11A: same expansion as voice
      if (lastConfirm) expandConfirmDetails(lastConfirm);
      return;
    }
    const viewBtn = e.target.closest("[data-open-path]");
    if (viewBtn) { call("open_path", viewBtn.dataset.openPath); return; }
    const copyBtn = e.target.closest("[data-copy-img]");
    if (copyBtn) { call("copy_image", copyBtn.dataset.copyImg); return; }
    const saveBtn = e.target.closest("[data-save-img]");
    if (saveBtn) { call("save_image_as", saveBtn.dataset.saveImg); return; }
    const revealBtn = e.target.closest("[data-reveal]");
    if (revealBtn) { call("reveal_path", revealBtn.dataset.reveal); return; }
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
