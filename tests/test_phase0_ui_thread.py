"""Phase 0 commit 5 hardfix — the UI-thread / native-exposure regression guards.

Two classes of bug flooded a real M0.1 run and must never return silently:

  A. The js_api object leaked a public `window` reference, so pywebview's
     API-exposure walker recursed into `window.native.CoreWebView2 /
     ZoomFactor / AccessibilityObject.Empty.Empty…` — off the UI thread —
     giving the E_NOINTERFACE flood and a RecursionError. Guard: replicate
     pywebview's EXACT exposure predicate and prove our js_api objects expose
     only their intended methods and never descend into a native tree.

  B. Native/COM access from a non-UI thread. Guard: assert_ui_thread() fires
     loudly off the UI thread, so a future socket/worker-thread native touch
     trips immediately instead of corrupting COM state.
"""

import inspect
import threading

import pytest

from app import ui_thread
from app.anna_ui import NativeApi
from app.web.bridge import JsApi, UIBridge


# ---- a faithful stand-in for the WebView2 / WinForms native object tree -------
# Real property NAMES via __dir__ (so `dir()` sees them, as it does on a live
# COM object), each yielding a FRESH node (new id, so de-dup can't hide the
# depth) — i.e. AccessibilityObject.Empty.Empty… and CoreWebView2/ZoomFactor.
class _NativeTree:
    __module__ = "System.Windows.Forms"

    def __dir__(self):
        return ["AccessibilityObject", "Empty", "Parent",
                "CoreWebView2", "ZoomFactor", "AllowExternalDrop"]

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _NativeTree()


class _FakeWindow:
    __module__ = "webview.window"

    def __init__(self):
        self.native = _NativeTree()

    def create_file_dialog(self, *a, **k):
        return None


def _pywebview_exposure_walk(obj, base="", seen=None, out=None):
    """pywebview's get_functions predicate, verbatim (webview/util.py). If this
    ever recurses into a window, so does the real thing at page load."""
    if seen is None:
        seen, out = set(), {}
    if id(obj) in seen:
        return out
    seen.add(id(obj))
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(obj, name)
        except Exception:
            continue
        if not getattr(attr, "_serializable", True):
            continue
        full = f"{base}.{name}" if base else name
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            out[full] = True
        elif inspect.isclass(attr) or (
                isinstance(attr, object) and not callable(attr)
                and hasattr(attr, "__module__")):
            _pywebview_exposure_walk(attr, full, seen, out)
    return out


# ---- Guard A: the js_api object never leaks the window to the walker ----------

def test_native_api_never_exposes_the_window_to_pywebview():
    """THE regression test for the flood. A window is attached exactly as
    run_ui does; pywebview's walker must still terminate and expose only our
    methods — never a `_window.*` / `native.*` path."""
    api = NativeApi(8765)
    api._window = _FakeWindow()          # exactly run_ui's `api._window = window`

    import sys
    sys.setrecursionlimit(700)           # if it recurses, this makes it fail fast
    exposed = sorted(_pywebview_exposure_walk(api))

    assert exposed == ["get_ws_config", "pick_voice_file", "ready"]
    assert not any("window" in name or "native" in name or "CoreWebView2" in name
                   for name in exposed), exposed


def test_a_public_window_attr_WOULD_have_recursed():
    """Proves the guard has teeth: the same walker DOES blow up when the window
    is public — so the test above is really catching something."""
    api = NativeApi(8765)
    api.window = _FakeWindow()           # the old bug: a PUBLIC attribute

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(400)
    try:
        with pytest.raises(RecursionError):
            _pywebview_exposure_walk(api)
    finally:
        sys.setrecursionlimit(old)


def test_legacy_jsapi_also_hides_its_bridge():
    """The in-process path never had this bug because JsApi holds `_bridge`
    (underscore). Pin that so a rename can't reintroduce the flood there."""
    bridge = UIBridge()
    bridge.window = _FakeWindow()
    api = JsApi(bridge)
    exposed = sorted(_pywebview_exposure_walk(api))
    assert exposed, "JsApi exposed nothing at all?"
    assert not any("native" in n or "window" in n for n in exposed), exposed


def test_no_public_nonmethod_attribute_holds_a_window(monkeypatch):
    """General invariant: no public, non-callable attribute of the js_api may
    carry an object with a `.native` — that is the shape pywebview walks."""
    api = NativeApi(8765)
    api._window = _FakeWindow()
    for name in dir(api):
        if name.startswith("_"):
            continue
        attr = getattr(api, name)
        if callable(attr):
            continue
        assert not hasattr(attr, "native"), (
            f"public attribute {name!r} exposes a native-bearing object")


# ---- Guard B: native access off the UI thread fails loudly --------------------

@pytest.fixture(autouse=True)
def _reset_ui_thread():
    ui_thread.clear_ui_thread()
    yield
    ui_thread.clear_ui_thread()


def test_assert_ui_thread_passes_on_the_registered_thread():
    ui_thread.set_ui_thread()            # this thread is "the UI thread"
    assert ui_thread.on_ui_thread()
    ui_thread.assert_ui_thread("native access")   # must not raise


def test_assert_ui_thread_raises_from_a_worker_thread():
    ui_thread.set_ui_thread()            # UI thread = this one
    boom = {}

    def worker():
        try:
            ui_thread.assert_ui_thread("touch window.native")
            boom["result"] = "NO RAISE"      # the bug would reach here silently
        except RuntimeError as e:
            boom["result"] = str(e)

    t = threading.Thread(target=worker, name="anna-live-tool")
    t.start()
    t.join()
    assert "touch window.native" in boom["result"]
    assert "UI thread" in boom["result"]
    assert "NO RAISE" not in boom["result"]


def test_native_accessor_gate_blocks_off_thread():
    ui_thread.set_ui_thread()
    win = _FakeWindow()
    assert ui_thread.native(win) is win.native   # on-thread: allowed

    err = {}

    def worker():
        try:
            ui_thread.native(win, "read .native")
        except RuntimeError as e:
            err["msg"] = str(e)

    t = threading.Thread(target=worker, name="socket-rx")
    t.start(); t.join()
    assert "read .native" in err["msg"]


def test_assert_is_a_noop_when_no_ui_thread_registered():
    """Headless processes (the daemon, tests) register no UI thread, so the
    guard costs nothing and never false-positives there."""
    ui_thread.clear_ui_thread()
    assert not ui_thread.on_ui_thread()
    ui_thread.assert_ui_thread("anything")       # must be a no-op, not a raise
