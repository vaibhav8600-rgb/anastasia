"""Playwright backend — real DOM, not pixels (11C.2).

**Attach, don't launch.** We connect over the Chrome DevTools Protocol to a
browser the user already has open, so Anna acts on the *real logged-in* Gmail
or WhatsApp Web tab. A separate automation profile would be logged out of
everything and useless for 11E, and `playwright install` would drag in a
~150 MB bundled Chromium we'd never use.

The cost of that choice: the browser must be started with a debugging port.
Anna says so plainly instead of silently doing nothing:

    chrome.exe --remote-debugging-port=9222

Targets come back with `confidence == 1.0` because they are DOM elements
located by role/text/selector, never a guessed coordinate.
"""

import queue
import threading

from app.agent.devlog import devlog
from app.control import (ActionResult, BackendUnavailable, ResolvedTarget,
                         Scope)

DEFAULT_CDP_URL = "http://localhost:9222"
BROWSER_HINTS = ("chrome", "edge", "msedge", "firefox", "brave", "chromium",
                 "opera")

SETUP_HINT = ("To let me work inside your browser, start it with a debugging "
              "port once:  chrome.exe --remote-debugging-port=9222  (close "
              "Chrome first). I attach to your real, logged-in session — I "
              "never open a separate profile.")


def looks_like_browser(window_title: str, app: str = "") -> bool:
    text = f"{window_title} {app}".lower()
    return any(hint in text for hint in BROWSER_HINTS)


class _PlaywrightWorker:
    """Owns the Playwright driver on ONE dedicated thread.

    Two hard constraints, both verified end-to-end against a real Chrome:
      * Playwright's sync API is bound to the thread that CREATED it — calling
        it from another thread silently fails.
      * A SECOND driver in the same process conflicts with the first.

    Anna trips both: the safety validator resolves a target on one thread while
    the tools execute on a worker thread, and each was building its own backend.
    The result was that every call after the first quietly returned None and Anna
    fell back to guessing at pixels — the browser backend never really ran.

    So there is exactly one driver, on one thread, and every call is marshalled
    to it.
    """

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self._jobs = queue.Queue()
        self._ready = threading.Event()
        self._start_error = None
        self._pw = None
        self._browser = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="anna-playwright")
        self._thread.start()

    # -- runs ON the worker thread ---------------------------------------
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        except Exception as e:
            self._start_error = e
        finally:
            self._ready.set()
        if self._start_error is not None:
            return
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, box, done = job
            try:
                box.append(("ok", fn(self)))
            except Exception as e:
                box.append(("err", e))
            done.set()
        try:
            if self._browser is not None:
                self._browser.close()   # detaches; does NOT close the user's browser
            self._pw.stop()
        except Exception:
            pass

    def browser(self):
        """Worker-thread only. Cached; reconnects if the browser went away."""
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            raise BackendUnavailable(
                f"I can't reach your browser on {self.cdp_url}. {SETUP_HINT}"
            ) from e
        return self._browser

    def page(self):
        """Worker-thread only. The active tab of the attached browser."""
        contexts = self.browser().contexts
        if not contexts or not contexts[0].pages:
            raise BackendUnavailable("Your browser has no open tab.")
        pages = contexts[0].pages
        for page in pages:                    # prefer a live tab
            try:
                if not page.is_closed():
                    return page
            except Exception:
                continue
        return pages[0]

    # -- callable from ANY thread ----------------------------------------
    def call(self, fn, timeout: float = 30.0):
        if not self._ready.wait(15):
            raise BackendUnavailable("The browser driver didn't start.")
        if self._start_error is not None:
            raise BackendUnavailable(
                f"playwright isn't usable: {self._start_error}")
        box, done = [], threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise BackendUnavailable("The browser didn't respond in time.")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def shutdown(self) -> None:
        self._jobs.put(None)


_worker = None
_worker_lock = threading.Lock()


def _get_worker(cdp_url: str) -> _PlaywrightWorker:
    """One driver per process, shared by every backend instance."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _PlaywrightWorker(cdp_url)
        return _worker


def shutdown_browser() -> None:
    """Release the driver (app exit). Does not close the user's browser."""
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.shutdown()
            _worker = None


class PlaywrightBackend:
    name = "playwright"

    def __init__(self, config=None, cdp_url: str = None):
        self.config = config
        self.cdp_url = cdp_url or getattr(config, "browser_cdp_url",
                                          DEFAULT_CDP_URL)

    # ---------------------------------------------------------- lifecycle
    def available(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except Exception:
            return False

    def _call(self, fn, timeout: float = 30.0):
        return _get_worker(self.cdp_url).call(fn, timeout)

    def page_title(self) -> str:
        return self._call(lambda w: w.page().title())

    def close(self) -> None:
        shutdown_browser()

    # ------------------------------------------------------------- lookup
    def _target_from_locator(self, locator, hint: str, selector: str,
                             role: str) -> ResolvedTarget:
        box = locator.bounding_box() or {}
        left, top = int(box.get("x", 0)), int(box.get("y", 0))
        right = left + int(box.get("width", 0))
        bottom = top + int(box.get("height", 0))
        try:
            name = (locator.inner_text(timeout=1000) or "").strip()
        except Exception:
            name = ""
        if not name:
            name = (locator.get_attribute("aria-label")
                    or locator.get_attribute("value") or hint or "").strip()
        try:
            is_password = (locator.get_attribute("type") or "").lower() == "password"
        except Exception:
            is_password = False
        return ResolvedTarget(name=name.splitlines()[0][:80] if name else hint,
                              control_type=role or "Element",
                              bbox=(left, top, right, bottom),
                              backend="playwright", confidence=1.0,
                              is_password=is_password, selector=selector,
                              handle=locator)

    @staticmethod
    def _locate(page, hint: str):
        """Worker-thread only. Role/text first (what a human sees), CSS last."""
        attempts = (
            ("button", lambda: page.get_by_role("button", name=hint, exact=False)),
            ("link", lambda: page.get_by_role("link", name=hint, exact=False)),
            ("textbox", lambda: page.get_by_role("textbox", name=hint, exact=False)),
            ("Element", lambda: page.get_by_placeholder(hint)),
            ("Element", lambda: page.get_by_label(hint, exact=False)),
            ("Element", lambda: page.get_by_text(hint, exact=False)),
            ("Element", lambda: page.locator(hint)),   # raw CSS/XPath
        )
        for role, build in attempts:
            try:
                locator = build().first
                if locator.count() == 0:
                    continue
                locator.wait_for(state="visible", timeout=1200)
                return locator, role
            except Exception:
                continue
        return None, ""

    def find_control(self, hint: str, scope: Scope = None):
        if not hint:
            return None

        def job(w):
            locator, role = self._locate(w.page(), hint)
            if locator is None:
                return None
            # Build the WHOLE target here — every one of these reads touches
            # the driver and so must happen on the worker thread.
            return self._target_from_locator(locator, hint, str(locator), role)

        try:
            return self._call(job)
        except BackendUnavailable as e:
            devlog.log(f"Playwright: {e}")
            return None
        except Exception as e:
            devlog.warn(f"Playwright: lookup failed "
                        f"({' '.join(str(e).split())[:100]})")
            return None

    def read_page_text(self) -> str:
        return self._call(lambda w: w.page().inner_text("body"))

    def get_visible_links(self, limit: int = 40) -> list:
        def job(w):
            links = []
            for link in w.page().get_by_role("link").all()[:limit]:
                try:
                    text = (link.inner_text(timeout=500) or "").strip()
                    href = link.get_attribute("href") or ""
                    if text and href:
                        links.append({"text": text[:80], "href": href})
                except Exception:
                    continue
            return links
        return self._call(job)

    def navigate(self, url: str) -> ActionResult:
        def job(w):
            w.page().goto(url, wait_until="domcontentloaded", timeout=20000)
            return True
        try:
            self._call(job, timeout=35.0)
            return ActionResult(True, f"Opened {url}.")
        except Exception as e:
            return ActionResult(False, f"I couldn't open that: "
                                       f"{' '.join(str(e).split())[:120]}")

    # ------------------------------------------------------------ actions
    def _locator_for(self, w, target: ResolvedTarget):
        """Worker-thread only. Prefer the live handle; re-find it if the target
        was rebuilt from a serialized plan (the handle can't survive that)."""
        if target.handle is not None:
            return target.handle
        locator, _role = self._locate(w.page(), target.name or target.selector)
        if locator is None:
            raise BackendUnavailable(f"“{target.name}” isn't on the page any more.")
        return locator

    def click(self, target: ResolvedTarget) -> ActionResult:
        def job(w):
            self._locator_for(w, target).click(timeout=5000)
            return True
        try:
            self._call(job)
            return ActionResult(True, f"Clicked “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't click that: "
                                       f"{' '.join(str(e).split())[:120]}")

    def type_into(self, target: ResolvedTarget, text: str) -> ActionResult:
        def job(w):
            self._locator_for(w, target).fill(text, timeout=5000)
            return True
        try:
            self._call(job)
            shown = "•" * len(text) if target.is_password else f"“{text[:40]}”"
            return ActionResult(True, f"Typed {shown} into “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't type there: "
                                       f"{' '.join(str(e).split())[:120]}")
