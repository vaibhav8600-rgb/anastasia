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
  for (const key of ["model", "mic", "voice"]) {
    const chip = $("#chip-" + key);
    const data = chips && chips[key];
    if (!chip || !data) continue;
    chip.querySelector(".chip-label").textContent = data.label || "";
    chip.dataset.state = data.state || "ok";
  }
}

/* ---------------------------------------------------------------- state */
function setState(state) {
  currentState = state;
  const meta = STATES[state] || STATES.ready;
  $("#core").dataset.state = state;
  const word = $("#state-word");
  word.textContent = meta.word;
  word.dataset.tone = meta.tone || "";
  $("#state-sub").textContent = meta.sub;
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

function settingsHtml(s) {
  return `
    <div class="form-row"><label>Your name (Anna greets you with it)</label>
      <input id="set-user_name" value="${esc(s.user_name || "")}"></div>
    <div class="form-row"><label>Ollama URL</label>
      <input id="set-ollama_url" value="${esc(s.ollama_url || "")}"></div>
    <div class="form-row"><label>Model name</label>
      <input id="set-ollama_model" value="${esc(s.ollama_model || "")}"></div>
    <div class="form-row"><label>Model timeout (seconds)</label>
      <input id="set-ollama_timeout" type="number" min="5" max="120"
             value="${esc(s.ollama_timeout || 20)}"></div>
    <div class="form-row"><label>Animation quality</label>
      <select id="set-animation_quality">
        ${["low", "medium", "high"].map(q =>
          `<option value="${q}" ${q === s.animation_quality ? "selected" : ""}>${q}</option>`).join("")}
      </select></div>
    <p class="dim" style="margin:4px 0 12px">Voice input/output settings arrive in a later update.</p>
    <button class="primary-btn" id="settings-save">Save</button>`;
}

/* ------------------------------------------------------- event handlers */
const handlers = {
  state_change: (p) => setState(p.state),
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

  clear_conversation: () => { messagesEl().innerHTML = ""; },

  history: (p) => openModal("Command history", historyHtml(p.rows)),
  settings: (p) => {
    openModal("Settings", settingsHtml(p));
    $("#settings-save").addEventListener("click", () => {
      call("save_settings", {
        user_name: $("#set-user_name").value,
        ollama_url: $("#set-ollama_url").value,
        ollama_model: $("#set-ollama_model").value,
        ollama_timeout: parseInt($("#set-ollama_timeout").value, 10) || 20,
        animation_quality: $("#set-animation_quality").value,
      });
      closeModal();
    });
  },

  full_state: (p) => {
    setState(p.state || "ready");
    setChips(p.chips || {});
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
  // avatar drop-in slot: use assets/avatar.png when present, else fallback
  const avatar = $("#avatar");
  avatar.addEventListener("load", () => {
    avatar.style.display = "block";
    $("#avatar-fallback").style.display = "none";
  });

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
