/* Anna transport layer (Phase 0, commit 5).

   The frontend stays "dumb": it renders `window.ui.dispatch({type, payload})`
   and forwards user input through `call(method, ...args)`. This module is the
   thing UNDER `call()`. Two implementations, one interface:

     * LegacyTransport — the in-process pywebview path, unchanged. `call` maps
       straight to `window.pywebview.api[method](...args)`. Always "open".
     * WsTransport — the split path: `call` becomes a protocol `request` frame
       to anna-core over a localhost WebSocket, and the reply resolves the
       returned promise. Events from core drive `window.ui.dispatch`.

   ARG_SPEC is the single source of truth for the JS↔Python call surface. It
   maps each method's POSITIONAL args (how app.js calls it) to NAMED args (how
   the WS request channel and JsApi expect them), and it is what
   docs/M0.1_PARITY.md is generated against — so a method that exists in one
   place and not the other is caught, not shipped.

   WsTransport's contract is "never lie":
     * a call while the socket is down REJECTS immediately — it never resolves
       null as if it had worked;
     * a confirmation is `approve()` (an `approval` frame naming the card id),
       and it too rejects while down — a click that can't reach core fails
       visibly, it does not pretend;
     * connection state is pushed to the UI as a synthetic `connection` event,
       so the window can show an honest "reconnecting" banner and refuse input;
     * on reconnect the client re-hydrates from the `full_state` core pushes on
       every hello_ok — that snapshot is authoritative, so nothing is missed
       and nothing is duplicated.

   Runs in the browser (attaches to `window`) and under node for tests
   (exports via module.exports). No dependencies either way. */

"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.AnnaTransport = api;
})(typeof window !== "undefined" ? window : null, function () {

  /* ---- the call surface: positional (app.js) -> named (JsApi / WS) -------- */
  /* [] means the method takes no arguments. `native: true` means it is served
     by the window process, never over the socket (a native dialog, or the
     bootstrap itself). `confirm` is special: it is not a request at all — it
     becomes an `approval` frame with a confirmation id. */
  const ARG_SPEC = {
    // bootstrap / window-local — never crosses the socket
    ready:                 { args: [], native: true },
    get_ws_config:         { args: [], native: true },
    pick_voice_file:       { args: ["kind"], native: true },

    // input
    send_text:             { args: ["text"] },
    start_ptt:             { args: [] },
    stop_ptt:              { args: [] },

    // confirmation — routed to an approval frame, NOT a request
    confirm:               { args: ["action_id", "approved"], approval: true },

    // toggles / settings
    set_toggle:            { args: ["name", "value"] },
    open_settings:         { args: [] },
    save_settings:         { args: ["settings"] },
    live_consent:          { args: ["accepted"] },

    // vision
    camera_frame:          { args: ["request_id", "data_url"] },
    privacy_mode:          { args: [] },

    // health / tests
    recheck:               { args: [] },
    test_voice:            { args: [] },
    validate_piper:        { args: [] },
    validate_kokoro:       { args: [] },
    validate_deepgram_tts: { args: [] },
    test_microphone:       { args: [] },
    test_model:            { args: [] },
    get_brain_info:        { args: [], returns: true },

    // data
    get_history:           { args: ["page"], returns: true },
    open_path:             { args: ["path"] },
    reveal_path:           { args: ["path"] },
    copy_image:            { args: ["path"] },
    save_image_as:         { args: ["path"] },
    clear_history:         { args: [] },
  };

  const PROTOCOL_VERSION = 1;

  function zip(names, values) {
    const out = {};
    names.forEach((name, i) => { out[name] = values[i]; });
    return out;
  }

  function hexId() {
    let s = "";
    for (let i = 0; i < 4; i++) s += Math.random().toString(16).slice(2, 10);
    return s.slice(0, 24);
  }

  function frame(type, payload, re) {
    const msg = { v: PROTOCOL_VERSION, id: hexId(),
                  ts: new Date().toISOString(), type: type,
                  payload: payload || {} };
    if (re != null) msg.re = re;
    return msg;
  }

  /* Route a raw event frame from core into the frontend's dispatcher. The wire
     wraps the 30 ui.dispatch types as {event, data}; unwrap to {type, payload}. */
  function dispatchEvent(payload) {
    if (typeof window !== "undefined" && window.ui && window.ui.dispatch) {
      window.ui.dispatch({ type: payload.event, payload: payload.data || {} });
    }
  }

  /* ------------------------------------------------------------ Legacy ----- */
  class LegacyTransport {
    constructor(nativeApi) { this.api = nativeApi || null; }
    isOpen() { return true; }
    connState() { return "open"; }

    call(method, ...args) {
      const a = this.api;
      if (a && typeof a[method] === "function") return Promise.resolve(a[method](...args));
      return Promise.resolve(null);
    }
    // In-process, an approval is just the confirm call; the card resolves on
    // the confirm_resolved event core dispatches, same as WS.
    approve(actionId, approved) { return this.call("confirm", actionId, approved); }
  }

  /* ------------------------------------------------------------ WS --------- */
  class WsTransport {
    constructor(opts) {
      this.url = opts.url;
      this.token = opts.token;
      this.client = opts.client || "anna-ui";
      this._WS = opts.WebSocketImpl ||
                 (typeof WebSocket !== "undefined" ? WebSocket : null);
      this._nativeApi = opts.nativeApi || null;   // for `native: true` methods
      this._onConn = opts.onConnState || function () {};
      this._onEvent = opts.onEvent || dispatchEvent;
      this._schedule = opts.schedule || ((fn, ms) => setTimeout(fn, ms));
      this._now = opts.now || (() => Date.now());
      this._requestTimeoutMs = opts.requestTimeoutMs || 15000;
      this._backoffMs = opts.backoffMs || [300, 700, 1500, 3000, 5000];

      this._ws = null;
      this._state = "connecting";     // connecting | open | reconnecting | closed
      this._fatal = null;             // set on auth/version failure — no reconnect
      this._attempt = 0;
      this._pending = new Map();      // request/approval id -> {resolve, reject, timer}
      this._closedByUs = false;
    }

    connState() { return this._state; }
    isOpen() { return this._state === "open"; }

    /* ---- public call surface ---- */
    call(method, ...args) {
      const spec = ARG_SPEC[method];
      if (!spec) return Promise.reject(this._err("unknown-method", method));
      if (spec.approval) return this.approve(args[0], args[1]);
      if (spec.native) {                      // window-local: never the socket
        const a = this._nativeApi;
        if (a && typeof a[method] === "function") return Promise.resolve(a[method](...args));
        return Promise.resolve(null);         // e.g. `ready` in WS mode: no-op
      }
      return this._request(method, zip(spec.args, args));
    }

    approve(actionId, approved) {
      // Never let a click pretend it landed while the socket is down.
      if (!this.isOpen()) return Promise.reject(this._err("disconnected", "approve"));
      const decision = approved ? "approve" : "cancel";
      return this._send(frame("approval",
        { confirmation_id: actionId, decision: decision }));
    }

    _request(method, argsObj) {
      if (!this.isOpen()) return Promise.reject(this._err("disconnected", method));
      return this._send(frame("request", { method: method, args: argsObj }));
    }

    /* ---- connection lifecycle ---- */
    connect() {
      if (this._fatal || !this._WS) return;
      this._setState(this._attempt === 0 ? "connecting" : "reconnecting");
      let ws;
      try {
        ws = new this._WS(this.url);
      } catch (e) {
        this._scheduleReconnect();
        return;
      }
      this._ws = ws;
      ws.onopen = () => ws.send(JSON.stringify(
        frame("hello", { token: this.token, client: this.client })));
      ws.onmessage = (ev) => this._onMessage(ev.data);
      ws.onclose = () => this._onClose();
      ws.onerror = () => {};   // close always follows; handle it there
    }

    close() {                  // deliberate shutdown — do not reconnect
      this._closedByUs = true;
      this._setState("closed");
      if (this._ws) { try { this._ws.close(); } catch (e) {} }
    }

    _onMessage(raw) {
      let msg;
      try { msg = JSON.parse(raw); } catch (e) { return; }
      const type = msg.type;
      if (type === "hello_ok") { this._attempt = 0; this._setState("open"); return; }
      if (type === "auth_failed" || type === "protocol_mismatch") {
        this._fatal = (msg.payload && msg.payload.reason) || type;
        this._setState("closed");
        return;
      }
      if (type === "event") { this._onEvent(msg.payload || {}); return; }
      if (type === "response" || type === "approval_result" || type === "error") {
        this._settle(msg);
        return;
      }
    }

    _onClose() {
      // Reject everything in flight: a request whose socket died did NOT
      // complete, and the caller must find out, not hang or assume success.
      this._rejectAllPending("disconnected");
      if (this._fatal || this._closedByUs) { this._setState("closed"); return; }
      this._setState("reconnecting");
      this._scheduleReconnect();
    }

    _scheduleReconnect() {
      const delay = this._backoffMs[Math.min(this._attempt, this._backoffMs.length - 1)];
      this._attempt += 1;
      this._schedule(() => { if (!this._closedByUs && !this._fatal) this.connect(); }, delay);
    }

    /* ---- request/response plumbing ---- */
    _send(msg) {
      return new Promise((resolve, reject) => {
        const timer = this._schedule(() => {
          this._pending.delete(msg.id);
          reject(this._err("timeout", msg.type));
        }, this._requestTimeoutMs);
        this._pending.set(msg.id, { resolve, reject, timer });
        try {
          this._ws.send(JSON.stringify(msg));
        } catch (e) {
          this._pending.delete(msg.id);
          this._clearTimer(timer);
          reject(this._err("send-failed", msg.type));
        }
      });
    }

    _settle(msg) {
      const waiter = this._pending.get(msg.re);
      if (!waiter) return;               // stale/duplicate reply — ignore
      this._pending.delete(msg.re);
      this._clearTimer(waiter.timer);
      const p = msg.payload || {};
      if (msg.type === "error") { waiter.reject(this._err(p.reason || "error", msg.re)); return; }
      if (msg.type === "approval_result") { waiter.resolve(p); return; }
      if (p.ok) waiter.resolve(p.result);
      else waiter.reject(this._err(p.error || "request-failed", msg.re));
    }

    _rejectAllPending(reason) {
      for (const waiter of this._pending.values()) {
        this._clearTimer(waiter.timer);
        try { waiter.reject(this._err(reason, "pending")); } catch (e) {}
      }
      this._pending.clear();
    }

    _clearTimer(timer) {
      if (typeof clearTimeout !== "undefined") clearTimeout(timer);
    }

    _setState(state) {
      if (state === this._state) return;
      this._state = state;
      // Push honest connection state to the frontend (banner + input gating).
      this._onEvent({ event: "connection",
                      data: { state: state, reason: this._fatal || "" } });
      try { this._onConn(state); } catch (e) {}
    }

    _err(code, where) {
      const e = new Error(code + (where ? " (" + where + ")" : ""));
      e.code = code;
      return e;
    }
  }

  /* Choose a transport. anna-ui exposes native get_ws_config(); its presence
     (with a url) is what puts us in WS mode. Everything else is legacy. */
  async function create(opts) {
    opts = opts || {};
    const nativeApi = opts.nativeApi ||
      (typeof window !== "undefined" && window.pywebview && window.pywebview.api) || null;
    let cfg = opts.wsConfig || null;
    if (!cfg && nativeApi && typeof nativeApi.get_ws_config === "function") {
      try { cfg = await nativeApi.get_ws_config(); } catch (e) { cfg = null; }
    }
    if (cfg && cfg.url) {
      const t = new WsTransport(Object.assign({ nativeApi: nativeApi }, cfg, opts));
      t.connect();
      return t;
    }
    return new LegacyTransport(nativeApi);
  }

  return { ARG_SPEC, WsTransport, LegacyTransport, create, frame, dispatchEvent,
           PROTOCOL_VERSION };
});
