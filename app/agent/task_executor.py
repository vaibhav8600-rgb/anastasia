"""Checkpointed multi-step executor (Phase 11D.2).

Runs a TaskPlan ONE step at a time through the existing safety path. Never an
autonomous chain:

  * re-validate before EVERY step (re-resolve targets, re-read state), so a
    plan made 20 seconds ago can't act on a window that has since changed;
  * a hard cap pauses for a human check-in after N steps;
  * any step whose signature is NOT in the originally-approved plan needs
    fresh confirmation, even if it is otherwise low risk;
  * a failed step HALTS the chain and reports honestly.

Everything is injected (validate / confirm / execute / revalidate / check-in),
so this is fully testable without a GUI, and — crucially — `execute` is the
same whitelisted executor a single command uses. No step skips the validator.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from app.agent.devlog import devlog
from app.agent.safety import validate_action
from app.agent.task_planner import StepStatus, TaskPlan, TaskStep
from app.llm.intent_parser import ActionPlan


@dataclass
class TaskRunResult:
    completed: bool
    stopped_reason: str = ""
    steps_done: int = 0
    message: str = ""
    steps: List[TaskStep] = field(default_factory=list)


class TaskExecutor:
    def __init__(self, config, agent, *, validate=None, execute=None,
                 confirm=None, revalidate=None, on_step=None, on_checkin=None,
                 max_steps_before_checkin: int = None):
        self.config = config
        self.agent = agent
        self._validate = validate or (lambda plan: validate_action(plan, config))
        self._execute = execute or (lambda plan: agent.execute(plan))
        # confirm(step, safety, *, unplanned, final) -> bool. None = DENY:
        # a task must never self-approve a risky or unplanned step.
        self._confirm = confirm
        # revalidate(step) -> bool. Re-check the world before the step; False
        # aborts (the UI changed out from under us). Default: allow.
        self._revalidate = revalidate or (lambda step: True)
        self._on_step = on_step or (lambda step: None)
        # on_checkin(done_steps, remaining_steps) -> bool. None = PAUSE (stop).
        self._on_checkin = on_checkin
        self.max_steps_before_checkin = int(
            max_steps_before_checkin if max_steps_before_checkin is not None
            else getattr(config, "task_max_steps_before_checkin", 5))

    def run(self, plan: TaskPlan, approved_signatures=None) -> TaskRunResult:
        approved = (set(approved_signatures) if approved_signatures is not None
                    else plan.approved_signatures())
        done = 0
        since_checkin = 0
        total = len(plan.steps)
        devlog.log(f"Task '{plan.task}': {total} steps, cap "
                   f"{self.max_steps_before_checkin} before check-in.")

        for index, step in enumerate(plan.steps):
            is_final = index == total - 1

            # --- hard cap: pause for a human before running past the cap
            if since_checkin >= self.max_steps_before_checkin:
                remaining = plan.steps[index:]
                if self._on_checkin is None or not self._on_checkin(
                        plan.steps[:index], remaining):
                    return self._stop(plan, done,
                                      "paused for check-in", index)
                since_checkin = 0

            # --- re-validate the world BEFORE the step (11D.2)
            step.status = StepStatus.RUNNING
            self._on_step(step)
            try:
                fresh = self._revalidate(step)
            except Exception as e:
                devlog.exception(e, context="task revalidate")
                fresh = False
            if not fresh:
                step.status = StepStatus.FAILED
                step.result = "the screen/app changed, so I stopped to be safe"
                return self._stop(plan, done, "state changed before a step", index,
                                  step=step)

            action = ActionPlan(intent=step.intent, tool_name=step.intent,
                                arguments=dict(step.arguments or {}))
            safety = self._validate(action)          # SAME validator, every step
            # the validator may have resolved a target onto the args — keep it
            step.arguments = dict(action.arguments or {})

            if not safety.allowed:
                step.status = StepStatus.FAILED
                step.result = safety.reason or "blocked by the safety policy"
                return self._stop(plan, done, "a step was blocked", index,
                                  step=step)

            unplanned = step.signature() not in approved
            final_gate = is_final and plan.requires_confirmation_before_final_submit
            needs_confirm = (safety.requires_confirmation or unplanned or final_gate)

            if needs_confirm:
                if self._confirm is None:
                    step.status = StepStatus.NEEDS_CONFIRM
                    return self._stop(plan, done,
                                      "a step needed confirmation and none was "
                                      "available", index, step=step)
                if unplanned:
                    devlog.warn(f"Task '{plan.task}': step {index + 1} "
                                f"({step.intent}) was NOT in the approved plan "
                                "— asking fresh.")
                approved_now = False
                try:
                    approved_now = bool(self._confirm(
                        step, safety, unplanned=unplanned, final=final_gate))
                except Exception as e:
                    devlog.exception(e, context="task confirm")
                if not approved_now:
                    step.status = StepStatus.SKIPPED
                    step.result = "you didn't approve this step"
                    return self._stop(plan, done, "a step was not approved",
                                      index, step=step)
                if unplanned:
                    approved.add(step.signature())   # approved now; don't re-ask

            # --- execute, then OBSERVE
            try:
                result = self._execute(action)
            except Exception as e:
                devlog.exception(e, context="task execute")
                step.status = StepStatus.FAILED
                step.result = "that step errored out"
                return self._stop(plan, done, "a step failed", index, step=step)

            step.result = getattr(result, "message", "") or ""
            if not getattr(result, "success", False):
                step.status = StepStatus.FAILED
                return self._stop(plan, done, "a step failed", index, step=step)

            step.status = StepStatus.DONE
            self._on_step(step)
            done += 1
            since_checkin += 1

        devlog.log(f"Task '{plan.task}': all {total} steps done.")
        return TaskRunResult(completed=True, steps_done=done,
                             message=f"Done — finished all {total} steps.",
                             steps=plan.steps)

    def _stop(self, plan, done, reason, index, step=None) -> TaskRunResult:
        detail = f" at step {index + 1} ({step.intent})" if step is not None else ""
        devlog.log(f"Task '{plan.task}': stopped ({reason}){detail}; "
                   f"{done} of {len(plan.steps)} steps done.")
        message = self._honest_message(reason, done, len(plan.steps), step)
        return TaskRunResult(completed=False, stopped_reason=reason,
                             steps_done=done, message=message, steps=plan.steps)

    @staticmethod
    def _honest_message(reason, done, total, step) -> str:
        if reason == "a step failed":
            what = f" ({step.result})" if step and step.result else ""
            return (f"I got {done} of {total} steps done, then step {done + 1} "
                    f"didn't work{what}, so I stopped there.")
        if reason == "a step was not approved":
            return f"Okay, I stopped — I did {done} of {total} steps."
        if reason == "paused for check-in":
            return (f"I've done {done} steps. Want me to keep going with the "
                    "rest?")
        if reason == "state changed before a step":
            return (f"The screen changed after {done} steps, so I paused rather "
                    "than click the wrong thing.")
        return f"I stopped after {done} of {total} steps ({reason})."
