"""Guided multi-step tasks (Phase 11D).

A task is a SHORT, ordered list of steps proposed up front. The executor runs
them ONE AT A TIME through the existing pipeline (resolve target -> local
safety validate -> confirm if needed -> execute -> observe), re-checking the
world before each step. It is deliberately NOT an autonomous agent loop:

  * state is re-validated before every step — the UI may have changed;
  * a hard cap (`max_steps_before_checkin`) pauses for a human check-in;
  * any step NOT in the originally-approved plan needs fresh confirmation;
  * a step failure halts the chain and reports honestly — no ploughing on.

The planner proposes; nothing here bypasses the safety validator. Each step
becomes an ordinary ActionPlan and takes the same path a single command does.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_CONFIRM = "needs_confirmation"


@dataclass
class TaskStep:
    intent: str                         # a tool name the validator knows
    arguments: dict = field(default_factory=dict)
    description: str = ""               # human summary for the check-in / card
    status: StepStatus = StepStatus.PENDING
    result: str = ""

    def signature(self) -> tuple:
        """Identity for 'was this in the approved plan?' — intent + the
        meaningful args, ignoring volatile resolver internals."""
        args = {k: v for k, v in (self.arguments or {}).items()
                if not str(k).startswith("_")}
        try:
            return (self.intent, json.dumps(args, sort_keys=True, default=str))
        except Exception:
            return (self.intent, str(args))


@dataclass
class TaskPlan:
    task: str                           # e.g. "draft_email"
    steps: List[TaskStep] = field(default_factory=list)
    requires_confirmation_before_final_submit: bool = True
    goal: str = ""                      # the user's original phrasing

    def approved_signatures(self) -> set:
        return {step.signature() for step in self.steps}

    def to_public(self) -> dict:
        return {"task": self.task, "goal": self.goal,
                "requires_confirmation_before_final_submit":
                    self.requires_confirmation_before_final_submit,
                "steps": [{"intent": s.intent, "arguments":
                           {k: v for k, v in (s.arguments or {}).items()
                            if not str(k).startswith("_")},
                           "description": s.description,
                           "status": s.status.value, "result": s.result}
                          for s in self.steps]}


def parse_task_plan(text) -> Optional[TaskPlan]:
    """Build a TaskPlan from a planner's JSON (dict or raw string). Returns
    None if it isn't a usable plan — the caller falls back to single commands,
    never to a blind chain."""
    if isinstance(text, str):
        from app.llm.intent_parser import extract_json
        data = extract_json(text)
    elif isinstance(text, dict):
        data = text
    else:
        data = None
    if not isinstance(data, dict):
        return None
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None

    steps = []
    for item in raw_steps:
        if isinstance(item, str):               # bare tool name
            steps.append(TaskStep(intent=item.strip().lower()))
            continue
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent") or item.get("tool")
                     or item.get("tool_name") or item.get("action") or "").strip().lower()
        if not intent:
            continue
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        steps.append(TaskStep(intent=intent, arguments=dict(args),
                              description=str(item.get("description") or "")))
    if not steps:
        return None
    return TaskPlan(
        task=str(data.get("task") or "task").strip().lower().replace(" ", "_"),
        steps=steps,
        requires_confirmation_before_final_submit=bool(
            data.get("requires_confirmation_before_final_submit", True)),
        goal=str(data.get("goal") or ""))
