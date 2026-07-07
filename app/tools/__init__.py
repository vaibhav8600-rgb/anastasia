"""Tool registry. Every tool is a whitelisted function; nothing model-generated
is ever executed. run_tool() is only called after agent/safety.py approval."""

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


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


TOOL_REGISTRY: Dict[str, Callable] = {}
_LOADED = False

_TOOL_MODULES = [
    "open_app", "file_tools", "keyboard_mouse", "clipboard_tools",
    "screenshot", "browser", "terminal", "window_control",
]


def tool(name: str):
    def deco(fn):
        TOOL_REGISTRY[name] = fn
        return fn
    return deco


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
