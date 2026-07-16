"""anna-core — the headless daemon (Phase 0, commit 4).

    python -m app.core.daemon [--port N]
    python app\\main.py --core [--port N]

Hosts the EXISTING Controller (pipeline, engines, safety validator, vision)
behind the WebSocket server. No pywebview, no window: the `ui` is a WsFanout
that broadcasts the same dispatch events to connected clients, and requests
arrive through the whitelisted JsApi surface. Nothing about the command path
changes — a text command from a socket walks the exact route a typed command
walks today: pipeline → validator → confirmation → executor.

Startup order matters: the port is bound FIRST, before the Controller exists.
Two consequences, both deliberate:
  * a second instance fails loud and immediately (PortInUseError, exit 2)
    without ever touching the microphone, hotkeys or event log;
  * the port is the single-instance lock — no pidfiles to go stale.
"""

import asyncio
import sys

from app.core.approvals import ApprovalRouter
from app.core.auth import load_or_create_token
from app.core.eventlog import EventLog
from app.core.server import (DEFAULT_PORT, CoreServer, PortInUseError,
                             WsFanout, make_request_handler)


def wire_controller(server: CoreServer, *, config=None, memory=None,
                    history=None, autostart: bool = True):
    """Build the Controller onto a (bound) server. Split from run_daemon so
    tests can assemble the identical stack around an ephemeral port."""
    from app.main import Controller
    from app.web.bridge import JsApi

    fanout = WsFanout(server)
    controller = Controller(ui=fanout, autostart=autostart, config=config,
                            memory=memory, history=history)
    fanout.controller = controller
    server.on_request = make_request_handler(JsApi(fanout))
    server.approvals = ApprovalRouter(controller.pipeline.confirm,
                                      controller.pipeline.approve_pending,
                                      controller.pipeline.cancel_pending,
                                      eventlog=server.eventlog)
    server.on_client_ready = controller.send_full_state
    return controller


def _parse_port(argv) -> int:
    argv = list(argv or [])
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                pass
        print(f"anna-core: --port needs a number (e.g. --port {DEFAULT_PORT})")
        raise SystemExit(2)
    return DEFAULT_PORT


async def _main(port: int) -> int:
    # Bind before creating ANYTHING — a second instance must exit on
    # PortInUseError without having touched the token file, the event log,
    # the microphone or the hotkeys. The empty token rejects any connection
    # that races the next two lines (fail closed).
    server = CoreServer(token="", port=port)
    await server.start()
    server.token = load_or_create_token()
    eventlog = EventLog()
    server.eventlog = eventlog
    controller = wire_controller(server)
    print(f"anna-core: listening on ws://127.0.0.1:{server.port}")
    try:
        await server.serve_forever()
    finally:
        try:
            controller.shutdown()         # Live session, mic, camera, drivers
        except Exception:
            pass
        eventlog.close()
    return 0


def run_daemon(argv=None) -> int:
    try:
        port = _parse_port(argv)
    except SystemExit as e:
        return int(e.code or 2)
    try:
        return asyncio.run(_main(port))
    except PortInUseError as e:
        print(str(e))                     # loud, specific, non-zero — no re-port
        return 2
    except KeyboardInterrupt:
        print("anna-core: stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(run_daemon(sys.argv))
