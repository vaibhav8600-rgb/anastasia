"""Phase 11D: guided multi-step execution. One step at a time, state
re-validated before each, a hard cap that pauses for a check-in, unplanned
steps re-confirmed, and an honest halt on failure. Never an autonomous chain.
"""

from app.agent.task_executor import TaskExecutor, TaskRunResult
from app.agent.task_planner import (StepStatus, TaskPlan, TaskStep,
                                    parse_task_plan)
from app.tools import ToolResult
from tests.fakes import make_config

CFG = make_config()


def steps(*intents):
    return [TaskStep(intent=i, arguments={"n": idx})
            for idx, i in enumerate(intents)]


def plan_of(*intents, **kw):
    # These generic sequences don't end in a submit; the final-submit gate is
    # exercised on its own in test_final_submit_step_always_confirmed.
    kw.setdefault("requires_confirmation_before_final_submit", False)
    return TaskPlan(task="demo", steps=list(steps(*intents)), **kw)


def ok(_plan):
    from app.agent.safety import SafetyResult
    return SafetyResult(allowed=True, requires_confirmation=False,
                        risk_level="low")


def make_executor(*, validate=ok, execute=None, confirm=None, revalidate=None,
                  on_checkin=None, max_steps=5, log=None):
    log = log if log is not None else {}
    log.setdefault("validated", [])
    log.setdefault("executed", [])

    def logged_validate(plan):
        log["validated"].append(plan.intent)
        return validate(plan)

    inner_execute = execute or (lambda plan: ToolResult(True, f"did {plan.intent}"))

    def logged_execute(plan):
        # Log only once the inner execute RETURNS: a step that raises never
        # produced a result, so it isn't counted as executed.
        result = inner_execute(plan)
        log["executed"].append(plan.intent)
        return result

    executor = TaskExecutor(
        CFG, agent=None, validate=logged_validate,
        execute=logged_execute, confirm=confirm,
        revalidate=revalidate, on_checkin=on_checkin,
        max_steps_before_checkin=max_steps)
    return executor, log


# ---- 11D.1: parsing -------------------------------------------------------------

def test_parse_task_plan_from_planner_json():
    plan = parse_task_plan({
        "task": "draft email",
        "requires_confirmation_before_final_submit": True,
        "steps": [
            {"intent": "open_app", "arguments": {"app_name": "chrome"},
             "description": "open gmail"},
            "browser_find_and_click",       # bare tool name is allowed
            {"tool": "browser_type_into", "arguments": {"text": "hi"}},
        ]})
    assert plan.task == "draft_email"
    assert [s.intent for s in plan.steps] == \
        ["open_app", "browser_find_and_click", "browser_type_into"]
    assert plan.requires_confirmation_before_final_submit
    # junk never becomes a blind chain
    assert parse_task_plan("not json") is None
    assert parse_task_plan({"task": "x", "steps": []}) is None


# ---- 11D: one step at a time ----------------------------------------------------

def test_plan_executes_one_step_at_a_time():
    executor, log = make_executor()
    result = executor.run(plan_of("open_app", "type_into_control", "click_control"))
    assert result.completed
    assert result.steps_done == 3
    # validated AND executed in strict order, one after another
    assert log["validated"] == ["open_app", "type_into_control", "click_control"]
    assert log["executed"] == ["open_app", "type_into_control", "click_control"]
    assert all(s.status is StepStatus.DONE for s in result.steps)


def test_state_revalidated_before_each_step():
    order = []

    def revalidate(step):
        order.append(("revalidate", step.intent))
        return True

    log = {"validated": [], "executed": []}

    def execute(plan):
        order.append(("execute", plan.intent))
        return ToolResult(True, "ok")

    executor, _ = make_executor(execute=execute, revalidate=revalidate, log=log)
    executor.run(plan_of("a", "b"))
    # every step is re-validated (and re-safety-checked) BEFORE it executes
    assert order == [("revalidate", "a"), ("execute", "a"),
                     ("revalidate", "b"), ("execute", "b")]
    assert log["validated"] == ["a", "b"]

    # if the world changed, the step does NOT run
    def stale(step):
        return step.intent != "b"           # 'b' is no longer valid

    log2 = {"validated": [], "executed": []}
    executor2, _ = make_executor(revalidate=stale, log=log2)
    result = executor2.run(plan_of("a", "b", "c"))
    assert not result.completed
    assert result.steps_done == 1
    assert log2["executed"] == ["a"]        # b and c never ran
    assert "changed" in result.message


def test_chain_pauses_for_checkin_after_step_cap():
    checkins = []

    def on_checkin(done_steps, remaining):
        checkins.append((len(done_steps), len(remaining)))
        return False                        # decline to continue

    executor, log = make_executor(max_steps=2, on_checkin=on_checkin)
    result = executor.run(plan_of("a", "b", "c", "d", "e"))
    assert not result.completed
    assert result.steps_done == 2           # stopped at the cap
    assert log["executed"] == ["a", "b"]
    assert checkins == [(2, 3)]             # asked after 2, with 3 remaining
    assert "keep going" in result.message

    # approving the check-in lets it continue through the rest
    executor2, log2 = make_executor(max_steps=2,
                                    on_checkin=lambda d, r: True)
    result2 = executor2.run(plan_of("a", "b", "c", "d", "e"))
    assert result2.completed and result2.steps_done == 5
    assert log2["executed"] == ["a", "b", "c", "d", "e"]

    # no check-in handler at all = pause (never barrel ahead)
    executor3, _ = make_executor(max_steps=2, on_checkin=None)
    assert not executor3.run(plan_of("a", "b", "c")).completed


def test_unplanned_step_requires_fresh_confirmation():
    """A step whose signature wasn't in the approved plan needs a fresh OK,
    even though it's otherwise a low-risk step."""
    asked = []

    def confirm(step, safety, *, unplanned, final):
        asked.append((step.intent, unplanned, final))
        return False                        # the user declines the surprise step

    plan = plan_of("open_app", "click_control", "type_into_control")
    # approve only the first two — the third was NOT in the approved plan
    approved = {plan.steps[0].signature(), plan.steps[1].signature()}

    executor, log = make_executor(confirm=confirm)
    result = executor.run(plan, approved_signatures=approved)

    assert not result.completed
    assert log["executed"] == ["open_app", "click_control"]   # third never ran
    assert asked == [("type_into_control", True, False)]      # unplanned=True
    assert result.steps_done == 2

    # a plan whose steps are all approved runs without extra prompts
    executor2, log2 = make_executor(
        confirm=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not ask")))
    plan2 = plan_of("open_app", "click_control",
                    requires_confirmation_before_final_submit=False)
    assert executor2.run(plan2).completed


def test_step_failure_halts_chain_and_reports_honestly():
    def execute(plan):
        if plan.intent == "b":
            return ToolResult(False, "the compose box wasn't there")
        return ToolResult(True, "ok")

    executor, log = make_executor(execute=execute)
    result = executor.run(plan_of("a", "b", "c", "d"))

    assert not result.completed
    assert result.stopped_reason == "a step failed"
    assert result.steps_done == 1
    assert log["executed"] == ["a", "b"]            # c and d never attempted
    assert result.steps[1].status is StepStatus.FAILED
    assert "step 2 didn't work" in result.message
    assert "compose box" in result.message          # the real reason, honestly

    # an exception mid-step halts just as cleanly
    def boom(plan):
        if plan.intent == "b":
            raise RuntimeError("crash")
        return ToolResult(True, "ok")

    executor2, log2 = make_executor(execute=boom)
    result2 = executor2.run(plan_of("a", "b", "c"))
    assert not result2.completed and result2.steps_done == 1
    assert log2["executed"] == ["a"]


# ---- 11D: the validator is never skipped ------------------------------------------

def test_every_step_passes_the_safety_validator():
    """A blocked step halts the chain — a task can't smuggle a blocked action
    past the validator by wrapping it in a plan."""
    from app.agent.safety import SafetyResult

    def validate(plan):
        if plan.intent == "delete_files":
            return SafetyResult(allowed=False, requires_confirmation=False,
                                risk_level="blocked", reason="not allowed")
        return SafetyResult(allowed=True, requires_confirmation=False,
                            risk_level="low")

    executor, log = make_executor(validate=validate)
    result = executor.run(plan_of("open_app", "delete_files", "click_control"))
    assert not result.completed
    assert result.stopped_reason == "a step was blocked"
    assert log["executed"] == ["open_app"]          # the blocked step never ran


def test_final_submit_step_always_confirmed():
    """requires_confirmation_before_final_submit gates the LAST step even when
    it's low-risk — the send/submit at the end of an email/message task."""
    asked = []

    def confirm(step, safety, *, unplanned, final):
        asked.append((step.intent, final))
        return True

    plan = TaskPlan(task="draft_email", steps=list(steps("open_app", "click_send")),
                    requires_confirmation_before_final_submit=True)
    approved = plan.approved_signatures()      # both steps are in the plan
    executor, log = make_executor(confirm=confirm)
    result = executor.run(plan, approved_signatures=approved)

    assert result.completed
    assert asked == [("click_send", True)]     # only the final step was gated
    # ...and declining the final submit stops it
    executor2, log2 = make_executor(confirm=lambda *a, **k: False)
    result2 = executor2.run(
        TaskPlan(task="draft_email", steps=list(steps("open_app", "click_send")),
                 requires_confirmation_before_final_submit=True))
    assert not result2.completed
    assert log2["executed"] == ["open_app"]    # never submitted


def test_confirmation_needed_but_no_handler_stops_safely():
    from app.agent.safety import SafetyResult

    def validate(plan):
        return SafetyResult(allowed=True, requires_confirmation=True,
                            risk_level="high")

    executor, log = make_executor(validate=validate, confirm=None)
    result = executor.run(plan_of("run_terminal"))
    assert not result.completed
    assert log["executed"] == []                    # fail closed, nothing ran
    assert result.steps[0].status is StepStatus.NEEDS_CONFIRM
