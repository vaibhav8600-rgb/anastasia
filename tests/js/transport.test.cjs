/* Headless tests for app/web/transport.js (Phase 0, commit 5).

   Node, zero deps. Run directly (`node tests/js/transport.test.cjs`) or through
   pytest (tests/test_phase0_ui_client.py::test_js_transport_suite shells out and
   asserts exit 0, so this lives in the normal suite).

   A FakeSocket stands in for the browser WebSocket and is driven by hand, so
   drops, reconnects and races are deterministic — no timers, no real network. */

"use strict";

const path = require("path");
const T = require(path.join(__dirname, "..", "..", "app", "web", "transport.js"));

let passed = 0;
const failures = [];
function ok(cond, msg) { if (cond) passed++; else failures.push(msg); }
function eq(a, b, msg) { ok(JSON.stringify(a) === JSON.stringify(b), `${msg} — got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`); }

/* A hand-driven WebSocket double. */
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.closed = false;
    FakeSocket.last = this;
    FakeSocket.instances.push(this);
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { this.closed = true; }
  // driven by the "server" side of the test:
  accept() { if (this.onopen) this.onopen(); }
  deliver(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
  drop() { this.closed = true; if (this.onclose) this.onclose({}); }
  sentOfType(t) { return this.sent.filter(m => m.type === t); }
}
FakeSocket.instances = [];

/* A manual scheduler: callbacks queue instead of firing on a real timer. */
function makeScheduler() {
  const q = [];
  const fn = (cb, ms) => { q.push({ cb, ms }); return q.length; };
  fn.flush = () => { const items = q.splice(0); items.forEach(i => i.cb()); };
  fn.count = () => q.length;
  return fn;
}

function newTransport(extra) {
  const events = [];
  const states = [];
  FakeSocket.instances = [];
  const schedule = makeScheduler();
  const t = new T.WsTransport(Object.assign({
    url: "ws://127.0.0.1:9/",
    token: "tok-123",
    WebSocketImpl: FakeSocket,
    onEvent: (p) => events.push(p),
    onConnState: (s) => states.push(s),
    schedule: schedule,
    requestTimeoutMs: 1e9,           // never auto-fire in these tests
  }, extra || {}));
  return { t, events, states, schedule };
}

function handshake(ctx) {
  ctx.t.connect();
  FakeSocket.last.accept();
  FakeSocket.last.deliver({ type: "hello_ok", payload: { protocol: 1 } });
}

/* ---- 1. handshake sends the token, opens on hello_ok ---- */
(function () {
  const ctx = newTransport();
  ctx.t.connect();
  const hi = FakeSocket.last;
  hi.accept();
  const hellos = hi.sentOfType("hello");
  ok(hellos.length === 1, "handshake: exactly one hello sent");
  eq(hellos[0].payload.token, "tok-123", "handshake: hello carries the token");
  ok(ctx.t.connState() === "connecting", "handshake: still connecting before hello_ok");
  hi.deliver({ type: "hello_ok", payload: {} });
  ok(ctx.t.isOpen(), "handshake: open after hello_ok");
  ok(ctx.states.includes("open"), "handshake: 'open' pushed to UI");
})();

/* ---- 2. request/response round-trip, positional -> named ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  const p = ctx.t.call("get_history", 7);
  const req = FakeSocket.last.sentOfType("request").pop();
  eq(req.payload, { method: "get_history", args: { page: 7 } },
     "request: positional arg mapped to named via ARG_SPEC");
  FakeSocket.last.deliver({ type: "response", re: req.id,
                            payload: { ok: true, result: [{ id: 1 }] } });
  return p.then(rows => eq(rows, [{ id: 1 }], "request: promise resolves with result"));
})();

/* ---- 3. a request while the socket is down REJECTS (never resolves null) ---- */
(function () {
  const ctx = newTransport();
  ctx.t.connect();                    // connecting, not yet open
  return ctx.t.call("send_text", "hi").then(
    () => ok(false, "down-request: must not resolve"),
    (e) => eq(e.code, "disconnected", "down-request: rejects with 'disconnected'"));
})();

/* ---- 4. confirm becomes an approval frame ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  const p = ctx.t.call("confirm", 5, true);
  const appr = FakeSocket.last.sentOfType("approval").pop();
  eq(appr.payload, { confirmation_id: 5, decision: "approve" },
     "confirm: routed to an approval frame with the card id");
  ok(FakeSocket.last.sentOfType("request").length === 0,
     "confirm: never sent as a generic request");
  FakeSocket.last.deliver({ type: "approval_result", re: appr.id,
                            payload: { outcome: "applied" } });
  return p.then(r => eq(r.outcome, "applied", "confirm: resolves with approval_result"));
})();

/* ---- 5. THE pin: an approve as the socket drops fails visibly ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  FakeSocket.last.drop();             // socket dies the instant before the click
  ok(ctx.t.connState() === "reconnecting", "approve-drop: state is reconnecting");
  return ctx.t.call("confirm", 9, true).then(
    () => ok(false, "approve-drop: must NOT pretend it landed"),
    (e) => eq(e.code, "disconnected", "approve-drop: rejects visibly with 'disconnected'"));
})();

/* ---- 6. a drop rejects every in-flight request (no hang, no false success) ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  const p = ctx.t.call("get_history", 0);
  FakeSocket.last.drop();
  return p.then(
    () => ok(false, "inflight-drop: pending request must not resolve"),
    (e) => eq(e.code, "disconnected", "inflight-drop: pending request rejected"));
})();

/* ---- 7. reconnect: backoff fires, a fresh hello is sent, reopens ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  const first = FakeSocket.last;
  first.drop();
  ok(ctx.schedule.count() === 1, "reconnect: a reconnect was scheduled");
  ctx.schedule.flush();               // backoff elapses
  const second = FakeSocket.last;
  ok(second !== first, "reconnect: a new socket was opened");
  second.accept();
  ok(second.sentOfType("hello").length === 1, "reconnect: re-authenticates with hello");
  second.deliver({ type: "hello_ok", payload: {} });
  ok(ctx.t.isOpen(), "reconnect: open again");
  eq(ctx.states.filter(s => s === "reconnecting").length >= 1, true,
     "reconnect: UI was told 'reconnecting'");
})();

/* ---- 8. fatal auth failure: no reconnect storm ---- */
(function () {
  const ctx = newTransport();
  ctx.t.connect();
  FakeSocket.last.accept();
  FakeSocket.last.deliver({ type: "auth_failed", payload: { reason: "bad-token" } });
  ok(ctx.t.connState() === "closed", "auth-fail: state closed");
  FakeSocket.last.drop();             // the socket also closes
  ok(ctx.schedule.count() === 0, "auth-fail: NO reconnect scheduled (no storm)");
})();

/* ---- 9. core events drive the frontend dispatcher (unwrapped) ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  FakeSocket.last.deliver({ type: "event",
    payload: { event: "state_change", data: { state: "thinking", detail: "" } } });
  const evs = ctx.events.filter(e => e.event === "state_change");
  eq(evs.length, 1, "event: forwarded once");
  eq(evs[0].data, { state: "thinking", detail: "" }, "event: data passed through");
})();

/* ---- 10. reconnect re-hydrates from the full_state core pushes on hello_ok ---- */
(function () {
  const ctx = newTransport();
  handshake(ctx);
  FakeSocket.last.drop();
  ctx.schedule.flush();
  const s2 = FakeSocket.last;
  s2.accept();
  s2.deliver({ type: "hello_ok", payload: {} });
  // core's on_client_ready pushes full_state on every (re)connect:
  s2.deliver({ type: "event", payload: { event: "full_state",
    data: { state: "ready", conversation: [{ role: "user", text: "hi" }] } } });
  const fs = ctx.events.filter(e => e.event === "full_state");
  eq(fs.length, 1, "rehydrate: full_state delivered after reconnect");
  eq(fs[0].data.conversation.length, 1, "rehydrate: snapshot carried the conversation");
})();

/* ---- 11. ARG_SPEC integrity: confirm is an approval, natives are flagged ---- */
(function () {
  ok(T.ARG_SPEC.confirm.approval === true, "ARG_SPEC: confirm is approval-routed");
  ["ready", "get_ws_config", "pick_voice_file"].forEach(m =>
    ok(T.ARG_SPEC[m].native === true, `ARG_SPEC: ${m} is native-only`));
  ok(T.ARG_SPEC.get_history.returns === true, "ARG_SPEC: get_history returns a value");
})();

/* ---- 12. create() picks legacy when there is no ws config ---- */
(async function () {
  const legacy = await T.create({ nativeApi: { send_text: () => "did-it" } });
  ok(legacy instanceof T.LegacyTransport, "create: legacy without ws config");
  const r = await legacy.call("send_text", "x");
  eq(r, "did-it", "create: legacy forwards straight to the native api");
})();

/* ---- report (after microtasks) ---- */
setTimeout(() => {
  if (failures.length) {
    console.error(`\n${failures.length} FAILED:`);
    failures.forEach(f => console.error("  ✗ " + f));
    process.exit(1);
  }
  console.log(`transport.js: ${passed} assertions passed`);
  process.exit(0);
}, 50);
