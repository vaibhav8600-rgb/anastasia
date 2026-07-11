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


class PlaywrightBackend:
    name = "playwright"

    def __init__(self, config=None, cdp_url: str = None):
        self.config = config
        self.cdp_url = cdp_url or getattr(config, "browser_cdp_url",
                                          DEFAULT_CDP_URL)
        self._pw = None
        self._browser = None

    # ---------------------------------------------------------- lifecycle
    def available(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except Exception:
            return False

    def _connect(self):
        """Attach to the running browser. Cached; reconnects if it died."""
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise BackendUnavailable(f"playwright not installed: {e}") from e
        try:
            if self._pw is None:
                self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            raise BackendUnavailable(
                f"I can't reach your browser on {self.cdp_url}. {SETUP_HINT}"
            ) from e
        return self._browser

    def page(self):
        """The active tab of the attached browser."""
        browser = self._connect()
        contexts = browser.contexts
        if not contexts or not contexts[0].pages:
            raise BackendUnavailable("Your browser has no open tab.")
        pages = contexts[0].pages
        for page in pages:                    # prefer a visible, loaded tab
            try:
                if not page.is_closed():
                    return page
            except Exception:
                continue
        return pages[0]

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()      # detaches; does NOT close the user's browser
        except Exception:
            pass
        self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

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

    def find_control(self, hint: str, scope: Scope = None):
        """Role/text first (that's what a human sees), CSS selector last."""
        if not hint:
            return None
        try:
            page = self.page()
        except BackendUnavailable as e:
            devlog.log(f"Playwright: {e}")
            return None

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
                return self._target_from_locator(locator, hint, str(locator), role)
            except Exception:
                continue
        return None

    def read_page_text(self) -> str:
        return self.page().inner_text("body")

    def get_visible_links(self, limit: int = 40) -> list:
        page = self.page()
        links = []
        for link in page.get_by_role("link").all()[:limit]:
            try:
                text = (link.inner_text(timeout=500) or "").strip()
                href = link.get_attribute("href") or ""
                if text and href:
                    links.append({"text": text[:80], "href": href})
            except Exception:
                continue
        return links

    def navigate(self, url: str) -> ActionResult:
        try:
            self.page().goto(url, wait_until="domcontentloaded", timeout=20000)
            return ActionResult(True, f"Opened {url}.")
        except Exception as e:
            return ActionResult(False, f"I couldn't open that: "
                                       f"{' '.join(str(e).split())[:120]}")

    # ------------------------------------------------------------ actions
    def click(self, target: ResolvedTarget) -> ActionResult:
        try:
            target.handle.click(timeout=5000)
            return ActionResult(True, f"Clicked “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't click that: "
                                       f"{' '.join(str(e).split())[:120]}")

    def type_into(self, target: ResolvedTarget, text: str) -> ActionResult:
        try:
            target.handle.fill(text, timeout=5000)
            shown = "•" * len(text) if target.is_password else f"“{text[:40]}”"
            return ActionResult(True, f"Typed {shown} into “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't type there: "
                                       f"{' '.join(str(e).split())[:120]}")
