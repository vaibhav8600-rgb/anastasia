"""App-control tools (11C) — UIA for native apps, Playwright for the browser.

The click/type tools do NOT resolve anything themselves: `agent/safety.py`
resolved the target while validating, stamped it on the plan, and only then
let execution proceed. Executing exactly that target is what makes the
destructive-target check meaningful — a second, later resolution could drift
to a different control after the user already approved.
"""

from app.control import ResolvedTarget, Scope
from app.tools import Tier, ToolContext, ToolResult, tool


def _resolver(ctx: ToolContext):
    from app.control.resolver import TargetResolver
    resolver = getattr(ctx, "resolver", None)
    if resolver is None:
        resolver = TargetResolver(ctx.config)
        ctx.resolver = resolver
    return resolver


def _target_from_args(args: dict):
    """Rebuild the target the VALIDATOR resolved. Its live handle is gone
    (it isn't serializable), so backends fall back to coordinates."""
    resolved = (args or {}).get("_resolved")
    if not isinstance(resolved, dict) or not resolved.get("bbox"):
        return None
    return ResolvedTarget(
        name=resolved.get("name", ""),
        control_type=resolved.get("control_type", ""),
        bbox=tuple(resolved.get("bbox") or (0, 0, 0, 0)),
        backend=resolved.get("backend", "vision"),
        confidence=float(resolved.get("confidence", 0.0)),
        app=resolved.get("app", ""),
        window_title=resolved.get("window_title", ""),
        is_password=bool(resolved.get("is_password")),
        selector=resolved.get("selector", ""),
        crop_data_url=resolved.get("crop_data_url", ""))


def _act(args: dict, ctx: ToolContext, text: str = None) -> ToolResult:
    target = _target_from_args(args)
    if target is None:
        return ToolResult(False, "I lost track of which control to use — "
                                 "ask me again and I'll look afresh.")
    resolver = _resolver(ctx)
    # Re-locate through the live backend when we can, so we act on a real
    # control object rather than stale coordinates.
    backend = resolver.backend_for(target)
    if backend is not None and target.backend != "vision":
        scope = Scope(app=target.app, window_title=target.window_title,
                      is_browser=target.backend == "playwright")
        fresh = backend.find_control(target.name or args.get("hint", ""), scope)
        if fresh is not None and fresh.name == target.name:
            target = fresh
    if backend is None:
        backend = resolver.vision

    result = (backend.click(target) if text is None
              else backend.type_into(target, text))
    payload = {k: v for k, v in target.to_public().items() if k != "crop_data_url"}
    return ToolResult(result.success, result.message, data=payload)


@tool("find_control", tier=Tier.SAFE, offline_ok=True,
      description="Read-only: locate an on-screen control (UIA/vision) and report what was found.",
      schema={"hint": ("string", "what to look for"),
              "app": ("string", "optional app to scope the search to")},
      required=("hint",))
def find_control(args: dict, ctx: ToolContext) -> ToolResult:
    """Read-only: locate a control and report what was found."""
    hint = str(args.get("hint") or args.get("target") or "")
    if not hint:
        return ToolResult(False, "What should I look for?")
    resolver = _resolver(ctx)
    scope = resolver.current_scope(app=str(args.get("app") or ""))
    target = resolver.resolve(hint, scope)
    if target is None:
        return ToolResult(False, f"I couldn't find “{hint}” on screen.")
    certainty = ("exactly" if target.certain
                 else f"only {target.confidence:.0%} sure — that's a guess")
    return ToolResult(True, f"Found {target.control_type} “{target.name}” "
                            f"via {target.backend} ({certainty}).",
                      data=target.to_public())


@tool("click_control", tier=Tier.SAFE, offline_ok=True,
      description="Click a resolved native control. Declared SAFE, but the "
                  "validator RAISES to a strong-phrase confirmation whenever the "
                  "resolved target is destructive (Send/Submit/Pay/…) or a vision guess.",
      schema={"hint": ("string", "what to click"),
              "app": ("string", "optional app to scope to")},
      required=("hint",))
def click_control(args: dict, ctx: ToolContext) -> ToolResult:
    return _act(args, ctx)


@tool("type_into_control", tier=Tier.SAFE, offline_ok=True,
      description="Type into a resolved native field (never into a password field). "
                  "SAFE floor; the validator raises on a destructive or guessed target.",
      schema={"hint": ("string", "the field to type into"),
              "text": ("string", "the text to type")},
      required=("hint", "text"))
def type_into_control(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text") or "")
    if not text:
        return ToolResult(False, "What should I type?")
    return _act(args, ctx, text=text)


@tool("read_window_text", tier=Tier.SAFE, offline_ok=True,
      description="Read the visible text of a window. Password fields are never read.",
      schema={"app": ("string", "optional app to scope to")})
def read_window_text(args: dict, ctx: ToolContext) -> ToolResult:
    """Visible text of a window. Password fields are never read."""
    resolver = _resolver(ctx)
    scope = resolver.current_scope(app=str(args.get("app") or ""))
    text = resolver.uia.read_window_text(scope)
    if not text:
        return ToolResult(False, "I couldn't read that window.")
    return ToolResult(True, text[:2000], data={"window": scope.window_title})


# ---- browser (Playwright over CDP) ------------------------------------------

@tool("browser_navigate", tier=Tier.SAFE, offline_ok=True,
      description="Navigate the attached browser (Playwright over CDP) to a URL.",
      schema={"url": ("string", "the URL to open")},
      required=("url",))
def browser_navigate(args: dict, ctx: ToolContext) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult(False, "Which page should I open?")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    result = _resolver(ctx).playwright.navigate(url)
    return ToolResult(result.success, result.message)


@tool("browser_find_and_click", tier=Tier.SAFE, offline_ok=True,
      description="Click an element in the attached browser page. SAFE floor; "
                  "the validator raises on a destructive or guessed target.",
      schema={"hint": ("string", "what to click")},
      required=("hint",))
def browser_find_and_click(args: dict, ctx: ToolContext) -> ToolResult:
    return _act(args, ctx)


@tool("browser_type_into", tier=Tier.SAFE, offline_ok=True,
      description="Type into a field in the attached browser page (never a password field). "
                  "SAFE floor; the validator raises on a destructive or guessed target.",
      schema={"hint": ("string", "the field to type into"),
              "text": ("string", "the text to type")},
      required=("hint", "text"))
def browser_type_into(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text") or "")
    if not text:
        return ToolResult(False, "What should I type?")
    return _act(args, ctx, text=text)


@tool("browser_read_page_text", tier=Tier.SAFE, offline_ok=True,
      description="Read the visible text of the attached browser page.",
      schema={})
def browser_read_page_text(args: dict, ctx: ToolContext) -> ToolResult:
    try:
        text = _resolver(ctx).playwright.read_page_text()
    except Exception as e:
        return ToolResult(False, " ".join(str(e).split())[:200])
    return ToolResult(True, text[:2000], data={"chars": len(text)})


@tool("browser_get_visible_links", tier=Tier.SAFE, offline_ok=True,
      description="List the visible links on the attached browser page.",
      schema={})
def browser_get_visible_links(args: dict, ctx: ToolContext) -> ToolResult:
    try:
        links = _resolver(ctx).playwright.get_visible_links()
    except Exception as e:
        return ToolResult(False, " ".join(str(e).split())[:200])
    if not links:
        return ToolResult(True, "I don't see any links on that page.", data=[])
    listed = "; ".join(link["text"] for link in links[:10])
    return ToolResult(True, f"{len(links)} links, including: {listed}",
                      data=links)
