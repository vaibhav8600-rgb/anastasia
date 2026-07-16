"""Vision tools (11B). Every one goes through the safety validator like any
other tool — capture is never a shortcut around the pipeline.

`look_at_screen` / `camera_look` accept `allow_sensitive`, which the validator
escalates to a confirmation: the user has to explicitly approve before Anna
analyzes a screen that appears to hold credentials or payment data.
"""

from app.tools import Tier, ToolContext, ToolResult, tool
from app.vision import VisionUnavailable


def _service(ctx: ToolContext):
    vision = getattr(ctx, "vision", None)
    if vision is None:
        raise VisionUnavailable("Vision isn't available in this session.")
    return vision


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _result(vision_result) -> ToolResult:
    if vision_result.needs_ack:
        return ToolResult(
            False,
            f"{vision_result.ack_reason}, so I didn't analyze it. "
            "If you want me to look anyway, say “look at my screen anyway”.",
            data={"needs_ack": True, "reason": vision_result.ack_reason})
    payload = {"source": vision_result.source, "scope": vision_result.scope,
               "window": vision_result.window_title,
               "used_cloud": vision_result.used_cloud,
               "saved_path": vision_result.saved_path}
    return ToolResult(True, vision_result.summary or "I couldn't make that out.",
                      data=payload)


def _look(args: dict, ctx: ToolContext, scope: str) -> ToolResult:
    try:
        screen = int(args.get("screen") or 0)
    except (TypeError, ValueError):
        screen = 0
    region = args.get("region")
    result = _service(ctx).look(
        scope=scope, region=region, screen=screen,
        question=str(args.get("question") or ""),
        allow_sensitive=_truthy(args.get("allow_sensitive")),
        save=_truthy(args.get("save")))
    return _result(result)


_LOOK_SCHEMA = {"question": ("string", "what to look for in the frame"),
                "screen": ("integer", "monitor number"),
                "allow_sensitive": ("boolean", "proceed even if the frame looks sensitive")}


@tool("look_at_screen", tier=Tier.SAFE, offline_ok=True,
      description="Capture one frame of the whole desktop and describe it. "
                  "Sensitive-looking screens need a separate explicit OK.",
      schema=_LOOK_SCHEMA)
def look_at_screen(args: dict, ctx: ToolContext) -> ToolResult:
    """Mode A: one frame of the whole desktop, on demand."""
    return _look(args, ctx, scope="full")


@tool("screen_capture", tier=Tier.SAFE, offline_ok=True,
      description="Capture one frame of the whole desktop and describe it.",
      schema=_LOOK_SCHEMA)
def screen_capture(args: dict, ctx: ToolContext) -> ToolResult:
    return _look(args, ctx, scope="full")


@tool("active_window_capture", tier=Tier.SAFE, offline_ok=True,
      description="Capture and describe just the active window.",
      schema=_LOOK_SCHEMA)
def active_window_capture(args: dict, ctx: ToolContext) -> ToolResult:
    return _look(args, ctx, scope="window")


@tool("region_capture", tier=Tier.SAFE, offline_ok=True,
      description="Capture and describe a region around the cursor (or a named region).",
      schema={**_LOOK_SCHEMA, "region": ("object", "optional bounding box")})
def region_capture(args: dict, ctx: ToolContext) -> ToolResult:
    scope = "cursor" if not args.get("region") else "region"
    return _look(args, ctx, scope=scope)


@tool("camera_look", tier=Tier.SAFE, offline_ok=True,
      description="Open the camera, take ONE frame, describe it, stop the camera. "
                  "Requires the window; sensitive content needs a separate OK.",
      schema={"question": ("string", "what to look for"),
              "allow_sensitive": ("boolean", "proceed even if the frame looks sensitive")})
def camera_look(args: dict, ctx: ToolContext) -> ToolResult:
    """11B.3: open the camera, take ONE frame, stop it immediately."""
    result = _service(ctx).camera_look(
        question=str(args.get("question") or ""),
        allow_sensitive=_truthy(args.get("allow_sensitive")),
        save=_truthy(args.get("save")))
    return _result(result)


@tool("start_screen_watch", tier=Tier.SAFE, offline_ok=True,
      description="Start watching the screen — one frame every interval — until told to stop.",
      schema={})
def start_screen_watch(args: dict, ctx: ToolContext) -> ToolResult:
    vision = _service(ctx)
    if not vision.start_watching():
        return ToolResult(True, "I'm already watching your screen.")
    interval = getattr(ctx.config, "screen_watch_interval_s", 1.5)
    return ToolResult(True, f"Watching your screen — one frame every "
                            f"{interval:.0f} second(s). Say “stop looking” "
                            f"whenever you want me to stop.")


@tool("stop_screen_watch", tier=Tier.SAFE, offline_ok=True,
      description="Stop watching the screen.",
      schema={})
def stop_screen_watch(args: dict, ctx: ToolContext) -> ToolResult:
    if _service(ctx).stop_watching("user asked"):
        return ToolResult(True, "Stopped watching your screen.")
    return ToolResult(True, "I wasn't watching your screen.")


@tool("privacy_mode", tier=Tier.SAFE, offline_ok=True,
      description="Kill switch: stop screen watching, the camera, and any Live audio session.",
      schema={})
def privacy_mode(args: dict, ctx: ToolContext) -> ToolResult:
    """The kill switch: screen watching, camera, and any Live audio session."""
    stopped = _service(ctx).privacy_mode()
    parts = [name for name, was_on in stopped.items() if was_on]
    detail = (", ".join(parts) + " stopped") if parts else "nothing was running"
    return ToolResult(True, f"Privacy mode on — {detail}. "
                            "Nothing is being captured now.",
                      data={"stopped": stopped})
