"""browser_open — open a URL or web search. Never auto-submits forms."""

import re
import urllib.parse
import webbrowser

from app.tools import ToolContext, ToolResult, tool

_DOMAIN_RE = re.compile(r"^[\w\-]+(\.[\w\-]+)+(/\S*)?$")

# Per-site search templates: when the model gives a bare site root AND a
# search query (e.g. url=youtube.com, query="funny videos"), it wants to
# SEARCH that site, not open its homepage.
_SITE_SEARCH = {
    "youtube.com": "https://www.youtube.com/results?search_query={q}",
    "google.com": "https://www.google.com/search?q={q}",
    "bing.com": "https://www.bing.com/search?q={q}",
    "duckduckgo.com": "https://duckduckgo.com/?q={q}",
    "amazon.com": "https://www.amazon.com/s?k={q}",
    "amazon.in": "https://www.amazon.in/s?k={q}",
}


def _root_host(url: str):
    """Host of a bare ROOT url (no meaningful path), else None."""
    try:
        parsed = urllib.parse.urlparse(
            url if re.match(r"^https?://", url, re.I) else "https://" + url)
    except Exception:
        return None
    if parsed.path.strip("/") or parsed.query:
        return None                       # has a real path/query -> not a root
    return parsed.netloc.lower().removeprefix("www.")


def build_target(args: dict) -> str | None:
    url = str(args.get("url") or "").strip()
    query = str(args.get("query") or args.get("search") or args.get("text") or "").strip()
    if url:
        # Both a site root and a query -> search that site (or the web).
        if query:
            host = _root_host(url)
            if host in _SITE_SEARCH:
                return _SITE_SEARCH[host].format(q=urllib.parse.quote_plus(query))
            if host is not None:          # unknown site root + query -> web search
                return "https://www.google.com/search?q=" + \
                    urllib.parse.quote_plus(query)
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            url = "https://" + url
        return url
    if query:
        if _DOMAIN_RE.match(query):
            return "https://" + query
        return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    return None


@tool("browser_open")
def browser_open(args: dict, ctx: ToolContext) -> ToolResult:
    target = build_target(args)
    if not target:
        return ToolResult(False, "What should I open or search for?")

    browser_alias = ctx.config.default_browser.strip()
    if browser_alias:
        from app.tools.open_app import resolve_app
        command = resolve_app(browser_alias, ctx.config)
        if command:
            import subprocess
            subprocess.Popen(f'start "" {command} "{target}"', shell=True)
            return ToolResult(True, f"Opened {target} in {browser_alias}.")
    webbrowser.open(target)
    return ToolResult(True, f"Opened {target} in your browser.")
