"""open_app — launch installed apps via user-approved aliases only."""

import os
import re
import subprocess
from pathlib import Path

from app.tools import ToolContext, ToolResult, tool

# Normalized spoken name -> canonical alias key
SYNONYMS = {
    "code": "vscode", "visualstudiocode": "vscode", "vsc": "vscode",
    "googlechrome": "chrome", "microsoftedge": "edge",
    "explorer": "file explorer", "files": "file explorer",
    "windowsexplorer": "file explorer", "fileexplorer": "file explorer",
    "calc": "calculator", "windowsterminal": "terminal",
    "cmd": "terminal", "commandprompt": "terminal",
    "microsoftteams": "teams", "msteams": "teams",
    "windowspowershell": "powershell",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s.\-_]+", "", s.lower())


def resolve_app(name: str, config) -> str | None:
    """Map a spoken app name to its configured launch command, or None."""
    norm = _norm(name or "")
    if not norm:
        return None
    aliases = {_norm(k): v for k, v in config.app_aliases.items()}
    if norm in aliases:
        return aliases[norm]
    canonical = SYNONYMS.get(norm)
    if canonical:
        return aliases.get(_norm(canonical))
    return None


def _launch(command: str) -> None:
    p = Path(command)
    if p.is_absolute():
        if p.exists():
            os.startfile(str(p))  # noqa: S606 — path comes from user config only
            return
        # Configured absolute path doesn't exist on this machine (e.g. Chrome
        # installed elsewhere) — fall back to `start <stem>`, which resolves
        # Windows App Paths and PATH entries.
        from app.agent.devlog import devlog
        devlog.warn(f"Alias path missing ({command}); falling back to 'start {p.stem}'.")
        command = p.stem
    # `start` resolves App Paths, PATH entries and URI protocols (msteams:)
    subprocess.Popen(f'start "" {command}', shell=True)


@tool("open_app")
def open_app(args: dict, ctx: ToolContext) -> ToolResult:
    name = str(args.get("app_name") or args.get("name") or args.get("app") or "").strip()
    if not name:
        return ToolResult(False, "Which app should I open?")
    command = resolve_app(name, ctx.config)
    if command is None:
        known = ", ".join(sorted(ctx.config.app_aliases))
        return ToolResult(False, f"I don't know an app called '{name}'. I can open: {known}.")
    try:
        _launch(command)
    except OSError as e:
        from app.agent.devlog import devlog
        devlog.exception(e, context=f"open_app '{name}' ({command})")
        return ToolResult(False, f"I couldn't launch {name} — is it installed?")
    return ToolResult(True, f"Done — I opened {name.title()} for you.")
