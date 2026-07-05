"""browser_open — open a URL or web search. Never auto-submits forms."""

import re
import urllib.parse
import webbrowser

from app.tools import ToolContext, ToolResult, tool

_DOMAIN_RE = re.compile(r"^[\w\-]+(\.[\w\-]+)+(/\S*)?$")


def build_target(args: dict) -> str | None:
    url = str(args.get("url") or "").strip()
    query = str(args.get("query") or args.get("search") or args.get("text") or "").strip()
    if url:
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
