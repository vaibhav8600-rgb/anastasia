"""Generate the machine-checkable tool inventory inside SKILL.md (Phase 0).

SKILL.md is mostly HAND-WRITTEN prose, and stays that way. This generator owns
exactly ONE region — the bytes between the BEGIN/END markers below — and never
touches anything outside it. The region is rebuilt from the tool registry, so
the inventory in the docs cannot drift from the `@tool` declarations in code.

    python -m app.tools.gen_skill        # rewrite the region in place

A test (`test_phase0_registry.py`) regenerates and diffs, so a stale table — or
a new tool nobody documented — fails CI rather than shipping.
"""

from pathlib import Path

from app.tools import Tier, all_specs

SKILL_PATH = Path(__file__).resolve().parents[2] / "SKILL.md"

BEGIN = "<!-- BEGIN GENERATED: tool-registry -->"
END = "<!-- END GENERATED: tool-registry -->"

_TIER_LABEL = {
    Tier.SAFE: "runs freely",
    Tier.CONFIRM: "asks first",
    Tier.DESTRUCTIVE: "strong phrase",
    Tier.BLOCKED: "blocked",
}


def render() -> str:
    """The generated region, deterministic (tools sorted by name)."""
    lines = [
        BEGIN,
        "",
        "<!-- Generated from app/tools by `python -m app.tools.gen_skill`.",
        "     Do not edit this region by hand — edit the @tool declarations. -->",
        "",
        "## Tool inventory (generated)",
        "",
        "Every tool Anna can run, straight from the registry. **Tier is a "
        "floor** — the safety validator computes the real risk at runtime and "
        "can only *raise* it above this, never lower it. \"Cloud-visible\" is "
        "whether a cloud model may even be told the tool exists.",
        "",
        "| Tool | Tier (floor) | Offline | Cloud-visible | What it does |",
        "|---|---|---|---|---|",
    ]
    for spec in all_specs():
        cloud = "yes" if (spec.cloud_declarable and not spec.blocked) else "no"
        lines.append(
            f"| `{spec.name}` | {_TIER_LABEL[spec.tier]} | "
            f"{'yes' if spec.offline_ok else 'no'} | {cloud} | {spec.description} |")
    lines += ["", END]
    return "\n".join(lines)


def apply(text: str, block: str) -> str:
    """`text` with the generated region replaced by `block`.

    If the markers aren't present yet, the block is appended after the existing
    prose (first-run bootstrap). Everything outside the markers is preserved
    byte-for-byte. Idempotent: applying twice yields the same string.
    """
    if BEGIN in text and END in text:
        head = text[:text.index(BEGIN)]
        tail = text[text.index(END) + len(END):]
        return head + block + tail
    body = text.rstrip("\n")
    return f"{body}\n\n{block}\n" if body else f"{block}\n"


def _expected(path: Path) -> str:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    return apply(current, render())


def write(path: Path = None) -> bool:
    """Regenerate the region in place. Returns True if the file changed."""
    path = path or SKILL_PATH
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = apply(current, render())
    if updated != current:
        path.write_text(updated, encoding="utf-8")
    return updated != current


def is_current(path: Path = None) -> bool:
    """True if the on-disk file already matches a fresh generation."""
    path = path or SKILL_PATH
    return path.exists() and _expected(path) == path.read_text(encoding="utf-8")


if __name__ == "__main__":
    print("SKILL.md updated." if write() else "SKILL.md already current.")
