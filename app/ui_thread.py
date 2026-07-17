"""UI-thread discipline for the windowed process (Phase 0, commit 5 hardfix).

The rule the WebView2 flood taught us: **native/COM objects — the window's
`.native`, `CoreWebView2`, `ZoomFactor`, WinForms controls — may only be
touched on the UI thread** (the thread that ran `webview.start()`). Touch them
from a worker thread and WebView2 raises "CoreWebView2 can only be accessed
from the UI thread" (E_NOINTERFACE); walk their object graph and you hit the
`AccessibilityObject.Empty.Empty…` recursion.

pywebview's own safe channels already respect this: `window.evaluate_js`
marshals via `webview.Invoke`, and js_api results cross the bridge as JSON.
Our code therefore has exactly one duty: **never touch `.native` off the UI
thread.** This module makes that duty enforceable instead of remembered:

    set_ui_thread()               # called once, on the UI thread, at startup
    assert_ui_thread("op name")   # raises if called from any other thread

`assert_ui_thread` is a no-op when no UI thread was ever registered (headless
tests, the daemon), so it costs nothing where there is no window to protect.
"""

import threading

_ui_thread_ident = None
_lock = threading.Lock()


def set_ui_thread() -> None:
    """Record the calling thread as THE UI thread. Call once, on the thread
    that will run the GUI message loop (i.e. right before `webview.start()`)."""
    global _ui_thread_ident
    with _lock:
        _ui_thread_ident = threading.get_ident()


def clear_ui_thread() -> None:
    global _ui_thread_ident
    with _lock:
        _ui_thread_ident = None


def ui_thread_ident():
    return _ui_thread_ident


def on_ui_thread() -> bool:
    ident = _ui_thread_ident
    return ident is not None and threading.get_ident() == ident


def assert_ui_thread(op: str) -> None:
    """Fail loudly if native/UI access is happening off the UI thread.

    This is the regression guard: a future change that touches the window from
    a socket/worker thread (the class of bug that flooded the console) trips
    here immediately and by name, instead of silently corrupting COM state or
    recursing. Marshal onto the UI thread first (pywebview's `evaluate_js` for
    the DOM; the WinForms `Invoke` for native) — see the module docstring."""
    ident = _ui_thread_ident
    if ident is None:
        return                      # no window in this process — nothing to guard
    if threading.get_ident() != ident:
        current = threading.current_thread().name
        raise RuntimeError(
            f"{op}: native/UI access from thread '{current}' (id "
            f"{threading.get_ident()}), but the UI thread is id {ident}. "
            "Native COM/WebView2 objects may only be touched on the UI thread; "
            "marshal via evaluate_js (DOM) or the window's Invoke (native).")


def native(window, op: str = "native access"):
    """The ONE sanctioned way to reach `window.native`. Asserts the UI thread
    first, so raw `.native` reads can never silently happen off it. Our code
    should almost never need this — the frontend and pywebview's own channels
    do the rendering — but when native truly must be touched, it goes here."""
    assert_ui_thread(op)
    return window.native
