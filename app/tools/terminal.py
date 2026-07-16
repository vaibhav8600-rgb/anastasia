"""run_terminal — only ever reached AFTER safety validation (dangerous
patterns blocked) AND explicit user confirmation in the GUI."""

import subprocess
from pathlib import Path

from app.tools import Tier, ToolContext, ToolResult, tool

_TIMEOUT = 90
_MAX_OUTPUT = 1500


@tool("run_terminal", tier=Tier.CONFIRM, offline_ok=True,
      description="Run a PowerShell command. Dangerous patterns are blocked "
                  "outright; every run requires explicit confirmation.",
      schema={"command": ("string", "the PowerShell command to run"),
              "cwd": ("string", "optional working directory (must be a safe folder)")},
      required=("command",))
def run_terminal(args: dict, ctx: ToolContext) -> ToolResult:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolResult(False, "There's no command to run.")

    # Optional working directory — must be inside a safe folder.
    cwd = None
    raw_cwd = str(args.get("cwd") or args.get("folder") or "").strip()
    if raw_cwd:
        from app.tools.file_tools import resolve_safe_folder
        folder = resolve_safe_folder(raw_cwd, ctx.config)
        if folder is None or not folder.exists():
            return ToolResult(False, f"'{raw_cwd}' isn't one of your safe folders, so I won't run there.")
        cwd = str(folder)

    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=_TIMEOUT,
            cwd=cwd, creationflags=creation)
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"The command timed out after {_TIMEOUT} seconds.")

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n… (output truncated)"
    ok = proc.returncode == 0
    status = "finished" if ok else f"failed (exit code {proc.returncode})"
    return ToolResult(ok, f"Command {status}.\n{output}".strip(), data=output)
