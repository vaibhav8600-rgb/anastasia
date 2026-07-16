"""Tool registry (formalized in Phase 0).

Every tool is a whitelisted function; nothing model-generated is ever executed,
and `run_tool()` is only reached after `agent/safety.py` approval.

Each tool now DECLARES itself: `{name, description, permission_tier, offline_ok,
schema}`. Three things hang off that declaration:

  * **SKILL.md is generated from it**, so the docs cannot drift from the code.
  * **The cloud manifest is derived from it** — a blocked tool can never be
    named to a cloud model, because the manifest is built by filtering the
    registry rather than by maintaining a second, hand-kept list.
  * **`permission_tier` is a FLOOR, never a ceiling.**

That last one is the load-bearing invariant, and it is worth being blunt about:

    A tool's declared tier can only ever RAISE the risk the validator computes.
    It can never lower it.

The validator re-derives risk at runtime from what is actually happening — the
resolved control's name, the confidence of the resolution, whether the field is
a password, whether the terminal command matches a dangerous pattern — and takes
the MAXIMUM of that and the declared tier. A tool that declares itself harmless
is simply believed about its floor and ignored about everything else. Otherwise
the registry would be a bypass around the validator, and §4 forbids bypasses.
"""

import importlib
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, Optional


class Tier(IntEnum):
    """A tool's DECLARED minimum. Ordered, so `max()` means "at least this bad"."""

    SAFE = 0         # runs without asking
    CONFIRM = 1      # needs an explicit OK (card or voice)
    DESTRUCTIVE = 2  # needs the STRONG phrase ("Anna approve")
    BLOCKED = 3      # never runs, whatever anyone says


# What each tier means to the safety validator, as a FLOOR.
TIER_RISK = {Tier.SAFE: "low", Tier.CONFIRM: "medium", Tier.DESTRUCTIVE: "high"}


@dataclass
class ToolResult:
    success: bool
    message: str
    data: Any = None


@dataclass
class ToolContext:
    config: Any
    memory: Any = None
    llm: Any = None    # OllamaClient (local), for summarize_clipboard
    brain: Any = None  # BrainRouter — privacy-aware provider routing
    vision: Any = None  # VisionService (11B) — screen/camera perception
    resolver: Any = None  # TargetResolver (11C) — UIA/Playwright/vision


@dataclass(frozen=True)
class ToolSpec:
    """What a tool says about itself. The validator trusts only `tier`, and only
    as a floor."""

    name: str
    description: str
    tier: Tier
    offline_ok: bool
    schema: Dict[str, tuple] = field(default_factory=dict)  # param -> (TYPE, doc)
    required: tuple = ()
    # May a cloud model even be TOLD this tool exists? Blocked tools never can.
    cloud_declarable: bool = True
    # Its RESULT carries clipboard text, so shipping that result to a cloud model
    # is governed by the 8C clipboard opt-in.
    exports_clipboard: bool = False

    @property
    def blocked(self) -> bool:
        return self.tier >= Tier.BLOCKED


TOOL_REGISTRY: Dict[str, Callable] = {}
TOOL_SPECS: Dict[str, ToolSpec] = {}
_LOADED = False

_TOOL_MODULES = [
    "open_app", "file_tools", "keyboard_mouse", "clipboard_tools",
    "screenshot", "browser", "terminal", "window_control", "vision_tools",
    "control_tools", "email_tools",
]


def tool(name: str, *, tier: Tier, offline_ok: bool, description: str,
         schema: dict = None, required: tuple = (),
         cloud_declarable: bool = True, exports_clipboard: bool = False):
    """Register a tool. `tier`, `offline_ok` and `description` are REQUIRED —
    a tool cannot silently forget to declare what it is."""
    def deco(fn):
        TOOL_REGISTRY[name] = fn
        TOOL_SPECS[name] = ToolSpec(
            name=name, description=description, tier=Tier(tier),
            offline_ok=bool(offline_ok), schema=dict(schema or {}),
            required=tuple(required), cloud_declarable=bool(cloud_declarable),
            exports_clipboard=bool(exports_clipboard))
        return fn
    return deco


def _load_all() -> None:
    global _LOADED
    if _LOADED:
        return
    for mod in _TOOL_MODULES:
        importlib.import_module(f"app.tools.{mod}")
    _LOADED = True


def tool_spec(name: str) -> Optional[ToolSpec]:
    """The declaration for a tool, or None if it isn't registered."""
    _load_all()
    return TOOL_SPECS.get((name or "").lower().strip())


def all_specs() -> list:
    _load_all()
    return [TOOL_SPECS[n] for n in sorted(TOOL_SPECS)]


def cloud_manifest(config) -> list:
    """The tools a CLOUD model may be told exist.

    Derived by filtering the registry — never a hand-kept second list, because a
    second list is a list that drifts. A blocked tool cannot appear here: not
    because someone remembered to exclude it, but because the filter starts from
    the registry and drops `Tier.BLOCKED` and `cloud_declarable=False`.
    """
    from app.llm.providers import DataClass, cloud_allowed

    clipboard_ok, _ = cloud_allowed({DataClass.CLIPBOARD}, config)
    manifest = []
    for spec in all_specs():
        if spec.blocked or not spec.cloud_declarable:
            continue
        if spec.exports_clipboard and not clipboard_ok:
            continue           # its result would carry clipboard text to the cloud
        manifest.append(spec)
    return manifest


def _load_all() -> None:
    global _LOADED
    if _LOADED:
        return
    for mod in _TOOL_MODULES:
        importlib.import_module(f"app.tools.{mod}")
    _LOADED = True


def run_tool(name: str, arguments: Optional[dict], ctx: ToolContext) -> ToolResult:
    _load_all()
    fn = TOOL_REGISTRY.get((name or "").lower().strip())
    if fn is None:
        return ToolResult(False, f"I don't have a tool called '{name}'.")
    try:
        return fn(arguments or {}, ctx)
    except Exception as e:  # tools must never crash the app
        from app.agent.devlog import devlog
        devlog.exception(e, context=f"tool:{name}")
        return ToolResult(False, "Hmm, my local brain tripped on that one. Try me again?")
