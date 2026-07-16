"""Phase 0 commit 2: the tool registry as single source of truth, and its core
invariant — **permission_tier is a FLOOR, never a ceiling.**

The validator still computes risk at runtime from what is actually happening
(the resolved control's name, terminal patterns, password fields, vision
confidence) and may only RAISE above the declared tier, never trust it
downward. A registry that could lower risk would be a bypass around the
validator, which Protocol §4 forbids outright.

Pinned here, per the phase requirements:
  1. Floor-not-ceiling: a tool self-declaring tier-0 on a send-like action
     still demands the strong phrase; a declared CONFIRM floor lifts an
     otherwise-low action; a lying spec for run_terminal changes nothing.
  2. The registry owns cloud eligibility: blocked / never-declare tools cannot
     appear in ANY cloud-bound manifest — even handed a schema.
  3. SKILL.md regenerate-and-diff: doc drift from the @tool declarations
     fails CI, not ships.
"""

import pytest

from app.agent.safety import (BLOCKED_TOOLS, CONFIRM_TOOLS,
                              set_target_resolver, validate_action)
from app.llm.intent_parser import ActionPlan
from app.tools import (TOOL_REGISTRY, TOOL_SPECS, Tier, ToolSpec, _load_all,
                       all_specs, cloud_manifest, tool_spec)
from tests.fakes import make_config

CFG = make_config()
_load_all()


@pytest.fixture(autouse=True)
def _clean_resolver():
    """Never leave an injected resolver behind for other tests."""
    yield
    set_target_resolver(None)


def plan_for(tool, **args):
    return ActionPlan(intent=tool, tool_name=tool, arguments=dict(args))


# ---- the registry is complete and honest ------------------------------------

def test_every_registered_tool_declares_itself():
    """@tool now REQUIRES tier/offline_ok/description — nothing registers
    without saying what it is."""
    assert set(TOOL_REGISTRY) == set(TOOL_SPECS)
    assert len(TOOL_SPECS) >= 32
    for spec in all_specs():
        assert spec.description.strip(), spec.name
        assert isinstance(spec.tier, Tier), spec.name


def test_no_registered_tool_is_on_the_blocked_list():
    assert not set(TOOL_REGISTRY) & BLOCKED_TOOLS


def test_declared_tiers_never_undercut_the_validator_sets():
    """A CONFIRM_TOOLS member may declare stricter than the validator would
    compute, never looser — the floor must actually be a floor."""
    for spec in all_specs():
        if spec.name in CONFIRM_TOOLS:
            assert spec.tier >= Tier.CONFIRM, (
                f"{spec.name} is in CONFIRM_TOOLS but declares {spec.tier!r}")


# ---- 1. permission_tier is a floor, never a ceiling --------------------------

def test_tier0_self_declaration_on_a_send_like_action_still_demands_strong_phrase():
    """THE commit-2 invariant test. click_control self-declares tier-0 SAFE;
    pointed at a Send button it still comes back destructive_target=True —
    the tier 11A answers only to "Anna approve" — at high risk, confirmation
    required. The declared tier was a floor the validator raised, not a
    ceiling it trusted."""
    assert tool_spec("click_control").tier == Tier.SAFE   # self-declared tier-0
    set_target_resolver(lambda plan, config: {
        "name": "Send", "control_type": "Button",
        "backend": "uia", "confidence": 1.0})

    safety = validate_action(plan_for("click_control", hint="Send"), CFG)

    assert safety.allowed
    assert safety.requires_confirmation
    assert safety.risk_level == "high"
    assert safety.destructive_target      # 11A: strong phrase, not a tap


def test_a_lying_spec_for_run_terminal_lowers_nothing(monkeypatch):
    """Even if the registry ENTRY for run_terminal claimed tier-0 SAFE, the
    validator's own branch still demands confirmation and still refuses
    dangerous commands. The runtime rules never read the declared tier except
    as a minimum."""
    monkeypatch.setitem(TOOL_SPECS, "run_terminal", ToolSpec(
        name="run_terminal", description="totally safe, promise",
        tier=Tier.SAFE, offline_ok=True))

    safety = validate_action(plan_for("run_terminal", command="echo hi"), CFG)
    assert safety.requires_confirmation and safety.risk_level == "medium"

    blocked = validate_action(plan_for("run_terminal", command="rm -rf /"), CFG)
    assert not blocked.allowed


def test_declared_floor_raises_an_otherwise_low_action(monkeypatch):
    """The load-bearing half of 'floor': re-declaring open_app at CONFIRM
    lifts it to medium + confirmation even though every runtime rule says
    low. Raising works; only lowering is impossible."""
    base = validate_action(plan_for("open_app", app_name="notepad"), CFG)
    assert base.allowed and not base.requires_confirmation

    monkeypatch.setitem(TOOL_SPECS, "open_app", ToolSpec(
        name="open_app", description="now stricter", tier=Tier.CONFIRM,
        offline_ok=True))

    lifted = validate_action(plan_for("open_app", app_name="notepad"), CFG)
    assert lifted.allowed and lifted.requires_confirmation
    assert lifted.risk_level == "medium"


# ---- 2. the registry owns every cloud-bound manifest --------------------------

def test_blocked_tools_never_appear_in_any_cloud_bound_manifest():
    names = {s.name for s in
             cloud_manifest(make_config(allow_clipboard_to_cloud=True))}
    assert names, "empty manifest"
    assert not (names & BLOCKED_TOOLS)
    assert "delete_files" not in names            # cloud_declarable=False
    assert names <= set(TOOL_REGISTRY)


def test_a_schema_cannot_smuggle_a_blocked_tool_into_live_declarations(monkeypatch):
    """Worst case: someone registers a Tier.BLOCKED tool AND hands it prose in
    live_tools._DECLARATIONS. It still never reaches a cloud model — the
    eligible set is filtered from the registry, not from the schema dict."""
    from app.agent import live_tools
    monkeypatch.setitem(TOOL_SPECS, "format_disk", ToolSpec(
        name="format_disk", description="never", tier=Tier.BLOCKED,
        offline_ok=True))
    monkeypatch.setitem(live_tools._DECLARATIONS, "format_disk", {
        "description": "never", "parameters": {"type": "OBJECT",
                                               "properties": {}, "required": []}})

    assert "format_disk" not in {s.name for s in cloud_manifest(CFG)}
    declared = {d["name"] for d in
                live_tools.live_tool_declarations(CFG)[0]["function_declarations"]}
    assert "format_disk" not in declared


def test_live_declarations_are_a_subset_of_the_registry_manifest():
    """Gemini Live's declaration list can only ever narrow the registry's
    manifest, never extend it."""
    from app.agent.live_tools import live_tool_declarations
    for config in (make_config(allow_clipboard_to_cloud=False),
                   make_config(allow_clipboard_to_cloud=True)):
        manifest = {s.name for s in cloud_manifest(config)}
        declared = {d["name"] for d in
                    live_tool_declarations(config)[0]["function_declarations"]}
        assert declared <= manifest


def test_clipboard_exporters_leave_the_manifest_with_the_optin_off():
    off = {s.name for s in cloud_manifest(make_config(allow_clipboard_to_cloud=False))}
    on = {s.name for s in cloud_manifest(make_config(allow_clipboard_to_cloud=True))}
    assert not ({"clipboard_read", "summarize_clipboard"} & off)
    assert {"clipboard_read", "summarize_clipboard"} <= on


def test_live_tools_constants_are_pinned_to_the_registry():
    """The prose constants in live_tools and the machine-readable registry
    flags must agree — this is the drift alarm."""
    from app.agent.live_tools import CLIPBOARD_EXPORTING, NEVER_DECLARE
    assert {s.name for s in all_specs() if s.exports_clipboard} == CLIPBOARD_EXPORTING
    assert {s.name for s in all_specs() if not s.cloud_declarable} == NEVER_DECLARE


# ---- 3. SKILL.md regenerate-and-diff ------------------------------------------

def test_skill_md_is_current():
    """Regenerate and diff: change a @tool declaration without running
    `python -m app.tools.gen_skill` and THIS fails — drift breaks CI,
    not the docs."""
    from app.tools.gen_skill import SKILL_PATH, is_current
    assert SKILL_PATH.exists(), "SKILL.md missing"
    assert is_current(), (
        "SKILL.md tool inventory is stale — run: python -m app.tools.gen_skill")


def test_generator_touches_only_its_marked_region(tmp_path):
    from app.tools import gen_skill
    doc = ("# Hand-written title\n\nPrecious prose ABOVE.\n\n"
           f"{gen_skill.BEGIN}\nstale old table\n{gen_skill.END}\n\n"
           "Precious prose BELOW.\n")
    path = tmp_path / "SKILL.md"
    path.write_text(doc, encoding="utf-8")

    assert gen_skill.write(path) is True
    result = path.read_text(encoding="utf-8")

    assert "Precious prose ABOVE." in result
    assert "Precious prose BELOW." in result      # prose AFTER the region too
    assert "stale old table" not in result
    assert "| `run_terminal` |" in result


def test_generator_bootstraps_without_markers_preserving_prose(tmp_path):
    from app.tools import gen_skill
    path = tmp_path / "SKILL.md"
    path.write_text("# Original\n\nHand-written.\n", encoding="utf-8")

    assert gen_skill.write(path) is True
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Original\n\nHand-written.")
    assert gen_skill.BEGIN in text and gen_skill.END in text

    assert gen_skill.write(path) is False         # idempotent second run
    assert gen_skill.is_current(path)


# ---- log_gap is a doctor-level fact -------------------------------------------

def test_gap_summary_reads_persisted_markers_like_doctor_does(tmp_path):
    """--doctor runs in its own process: it cannot see live counters, only the
    durable log_gap ROWS. gap_summary() therefore reads the disk."""
    from app.core.eventlog import EventLog
    path = tmp_path / "events.sqlite"
    writer = EventLog(path)
    writer.emit("log_gap", salience=1.0, dropped=7, reason="queue overflow")
    writer.flush()
    writer.close()

    reader = EventLog(path, start=False)          # a separate "process"
    assert reader.gap_summary() == {"markers": 1, "dropped": 7}


def test_gap_summary_is_zero_on_a_clean_log(tmp_path):
    from app.core.eventlog import EventLog
    path = tmp_path / "events.sqlite"
    writer = EventLog(path)
    writer.emit("user_turn", text="hello", route="rule")
    writer.flush()
    writer.close()
    assert EventLog(path, start=False).gap_summary() == {"markers": 0, "dropped": 0}
