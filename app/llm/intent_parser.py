"""Parse LLM output into a validated ActionPlan (Pydantic).

The local LLM never executes anything — it only returns JSON, which is
parsed and validated here, then checked by agent/safety.py.
"""

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

VALID_INTENTS = {
    "open_app", "open_folder", "type_text", "press_hotkey", "search_files",
    "take_screenshot", "summarize_clipboard", "clipboard_read", "clipboard_write",
    "run_terminal", "browser_open", "window_control", "delete_files",
    "ask_clarification", "no_action",
}


class ActionPlan(BaseModel):
    model_config = {"extra": "ignore"}

    assistant_message: str = ""
    intent: str = "no_action"
    tool_name: str = ""
    arguments: dict = Field(default_factory=dict)
    risk_level: str = "low"
    requires_confirmation: bool = False
    confirmation_message: str = ""

    @field_validator("risk_level", mode="before")
    @classmethod
    def _norm_risk(cls, v: Any) -> str:
        v = str(v or "low").lower().strip()
        return v if v in ("low", "medium", "high", "blocked") else "low"

    @field_validator("intent", "tool_name", mode="before")
    @classmethod
    def _norm_name(cls, v: Any) -> str:
        return str(v or "").lower().strip().replace(" ", "_")

    @field_validator("arguments", mode="before")
    @classmethod
    def _norm_args(cls, v: Any) -> dict:
        return v if isinstance(v, dict) else {}

    def model_post_init(self, __context) -> None:
        if not self.tool_name:
            self.tool_name = self.intent


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks (qwen3 and friends emit these)."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)


def _balanced_json(text: str) -> Optional[str]:
    """Extract the first balanced {...} object, string-aware."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of raw model output (fences, prose, etc.)."""
    if not text:
        return None
    text = strip_thinking(text).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    raw = _balanced_json(text)
    if raw:
        candidates.append(raw)
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def parse_action_plan(text: str) -> Optional[ActionPlan]:
    """Return a validated ActionPlan, or None if the output is unusable."""
    data = extract_json(text)
    if not isinstance(data, dict):
        return None
    try:
        return ActionPlan(**data)
    except Exception:
        return None
