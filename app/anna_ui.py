"""anna-ui — the thin window client (Phase 0, commit 5).

    python -m app.anna_ui [--port N]
    python app\\main.py --ui [--port N]

A pywebview window that renders the SAME frontend and connects to a running
anna-core over the localhost WebSocket. The window process is deliberately
thin: it owns only what a browser context cannot do itself —

  * `get_ws_config()` — hands the page the socket URL and the per-install
    token, so `transport.js` can authenticate. This is the one place the token
    enters the UI process, and it never leaves the machine.
  * `pick_voice_file()` — a native file-open dialog, which needs a real window.

EVERYTHING else — every command, toggle, confirmation, screenshot, history
read — travels the WebSocket to anna-core through `transport.js`. getUserMedia
(the camera) runs in the WebView as it always has. There is no Controller here,
no pipeline, no validator: those live in the daemon, reached only through the
authenticated, whitelisted protocol.

If the daemon isn't up, the window still opens and shows its honest
"Reconnecting to Anna…" banner (WsTransport retries with backoff); it starts
working the moment core is reachable. Start the daemon with
`python app\\main.py --core`.
"""

import sys

from app.agent.devlog import devlog
from app.core.auth import load_or_create_token
from app.core.server import DEFAULT_PORT


class NativeApi:
    """The minimal `pywebview.api` surface — window-local operations only.
    Its method names are the ARG_SPEC entries flagged `native: true`; anything
    not here does not exist to the page except over the socket.

    CRITICAL: every non-method attribute here is PRIVATE (`_`-prefixed).
    pywebview's API-exposure walker (`inject_pywebview` → `get_functions`)
    recurses into any *public, non-callable* attribute of the js_api object
    hunting for nested functions. A public `window` reference therefore sends
    it walking `window.native.CoreWebView2 / ZoomFactor / AccessibilityObject.
    Empty.Empty…` — off the UI thread — which is the E_NOINTERFACE flood and
    the recursion crash. Underscore-prefixing keeps the walker out (it skips
    `_`-names), which is exactly why the legacy `JsApi._bridge` never tripped
    this. `test_native_api_never_exposes_the_window_to_pywebview` pins it."""

    _FILE_TYPES = {
        "piper_exe": ("Piper executable (*.exe)",),
        "piper_voice": ("Piper voice (*.onnx)",),
        "kokoro_model": ("Kokoro model (*.onnx)",),
        "kokoro_voices": ("Kokoro voices (*.bin)",),
    }

    def __init__(self, port: int):
        self._port = int(port)
        self._window = None           # PRIVATE — never public (see class docstring)

    def get_ws_config(self) -> dict:
        return {"url": f"ws://127.0.0.1:{self._port}",
                "token": load_or_create_token(),
                "client": "anna-ui"}

    def ready(self) -> None:
        # Legacy handshake hook. In the split build the WS hello_ok replaces it;
        # kept so the page's boot code has something to call.
        pass

    def pick_voice_file(self, kind) -> str:
        kind = str(kind or "")
        if kind not in self._FILE_TYPES or self._window is None:
            return ""
        try:
            import webview
            # `create_file_dialog` is pywebview's own API (it owns its native
            # threading); we never touch `_window.native` ourselves.
            picked = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=self._FILE_TYPES[kind])
            return str(picked[0]) if picked else ""
        except Exception as exc:
            devlog.exception(exc, context="anna-ui voice file picker")
            return ""


def _parse_port(argv) -> int:
    argv = list(argv or [])
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                pass
    return DEFAULT_PORT


def run_ui(argv=None) -> int:
    import webview
    from pathlib import Path

    from app import ui_thread

    port = _parse_port(argv)
    api = NativeApi(port)
    index = Path(__file__).resolve().parent / "web" / "index.html"
    window = webview.create_window(
        "Anastasia (Anna)", url=str(index), js_api=api,
        width=1280, height=820, min_size=(900, 650),
        background_color="#05060f")
    api._window = window          # PRIVATE attr — pywebview must not walk it
    print(f"anna-ui: window up; connecting to ws://127.0.0.1:{port}")
    # This thread runs the GUI message loop; register it so assert_ui_thread()
    # can catch any future native access that strays off it.
    ui_thread.set_ui_thread()
    try:
        webview.start(gui="edgechromium", debug=("--debug" in (argv or [])))
    except Exception as e:
        from app.main import _webview2_error_box
        _webview2_error_box(str(e))
        return 1
    finally:
        ui_thread.clear_ui_thread()
    return 0


if __name__ == "__main__":
    sys.exit(run_ui(sys.argv))
