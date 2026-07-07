"""Phase 9.1A: SQLite thread-safety + decoupled logging. Reproduces the
close-paint confirmation crash (approval thread logging on a closed handle)
and proves execution outcome is independent of logging outcome."""

import threading

from app.agent.history import History
from app.agent.pipeline import CommandPipeline
from app.llm.intent_parser import ActionPlan
from app.tools import ToolResult
from tests.fakes import (FakeAgent, FakeHistory, FakePipelineUI, FakeSpeech,
                         make_config)


# ---- thread-safety -----------------------------------------------------------

def test_history_log_from_multiple_threads_no_error(tmp_path):
    history = History(db_path=tmp_path / "h.sqlite")
    errors = []

    def worker(n):
        try:
            for i in range(20):
                history.log(transcript=f"t{n}-{i}", executed=True, result="ok")
        except Exception as e:  # must never happen
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    assert len(history.recent(500)) == 120


def test_confirmed_action_approval_thread_logs_cleanly(tmp_path):
    """The exact close-paint path: the original worker's DB is 'gone' when a
    separate approval thread logs. Per-call connections make this a non-issue."""
    history = History(db_path=tmp_path / "h.sqlite")
    history.close()   # simulate the original worker finishing/closing
    done = []

    def approval_thread():
        # a different thread logs the confirmed 'close paint' action
        history.log(transcript="close paint", executed=True, result="closed")
        done.append(True)

    t = threading.Thread(target=approval_thread, name="anna-approved")
    t.start(); t.join()
    assert done == [True]
    assert any(r["transcript"] == "close paint" for r in history.recent())


# ---- non-fatal logging -------------------------------------------------------

class ExplodingHistory:
    def log(self, *a, **k): raise RuntimeError("Cannot operate on a closed database.")
    def recent(self, limit=50): return []
    def clear(self): pass
    def close(self): pass


def test_history_log_failure_does_not_raise(tmp_path):
    from app.agent.devlog import devlog
    devlog.echo_to_stdout = False
    try:
        history = History(db_path=tmp_path / "h.sqlite")
        # corrupt the path so the connection can't be made
        history.db_path = tmp_path / "nonexistent-dir" / "sub" / "h.sqlite"
        history.log(transcript="x", executed=True)   # must not raise
    finally:
        devlog.echo_to_stdout = True


def make_pipeline(agent, history=None):
    return CommandPipeline(config=make_config(), agent=agent,
                           history=history or FakeHistory(), ui=FakePipelineUI(),
                           speech=FakeSpeech(), run_async=False)


def test_fail_handler_survives_history_error():
    agent = FakeAgent(make_config())
    pipeline = make_pipeline(agent, history=ExplodingHistory())
    trace = type("T", (), {})()
    # _fail must not raise even when history.log throws
    pipeline._fail("do something", "It failed.", trace)
    assert any("failed" in e.lower() for e in pipeline.ui.errors)


def test_successful_tool_reported_success_even_if_logging_fails():
    """The regression: window closed (success) but logging threw, and the
    user was told it failed. Now success is reported regardless of logging."""
    plan = ActionPlan(intent="window_control", tool_name="window_control",
                      arguments={"action": "close"}, risk_level="medium",
                      requires_confirmation=True)
    agent = FakeAgent(make_config(), execute_result=ToolResult(True, "Closed Paint."))
    pipeline = make_pipeline(agent, history=ExplodingHistory())
    trace = type("T", (), {"tool_ms": 0, "tts_ms": 0})()

    pipeline._run_tool(plan, None, "close paint", trace)
    ui = pipeline.ui
    assert ui.results and ui.results[0][1]["success"] is True   # reported success
    assert ui.errors == []                                      # NOT reported as failure
