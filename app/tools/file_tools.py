"""open_folder / search_files — restricted to user-approved safe folders.
delete_files is a stub: destructive deletion is disabled in the MVP."""

import os
from pathlib import Path

from app.tools import ToolContext, ToolResult, tool

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "AppData", "$RECYCLE.BIN", ".cache"}
_MAX_SCAN = 20000
_MAX_RESULTS = 25


def resolve_safe_folder(raw: str, config) -> Path | None:
    """Resolve a folder name ('downloads') or path to a safe folder, else None."""
    if not raw:
        return None
    safe = [Path(f) for f in config.safe_folders]
    candidate = Path(str(raw)).expanduser()
    if candidate.is_absolute():
        for f in safe:
            try:
                if candidate.resolve().is_relative_to(f.resolve()):
                    return candidate
            except (OSError, ValueError):
                continue
        return None
    name = str(raw).lower().strip().removesuffix(" folder").strip()
    for f in safe:
        if f.name.lower() == name:
            return f
    return None


@tool("open_folder")
def open_folder(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("folder") or args.get("path") or args.get("target_path") or "")
    folder = resolve_safe_folder(raw, ctx.config)
    if folder is None:
        safe = ", ".join(Path(f).name for f in ctx.config.safe_folders)
        return ToolResult(False, f"'{raw}' isn't one of your safe folders. I can open: {safe}.")
    if not folder.exists():
        return ToolResult(False, f"Hmm, {folder} doesn't exist on this machine.")
    os.startfile(str(folder))  # noqa: S606 — safe-folder whitelist enforced above
    return ToolResult(True, f"Opened your {folder.name} folder.")


@tool("search_files")
def search_files(args: dict, ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or args.get("name") or "").strip()
    if not query:
        return ToolResult(False, "What file name should I look for?")
    raw = str(args.get("folder") or args.get("path") or "")
    if raw and raw.lower() not in ("", "all", "everywhere"):
        folder = resolve_safe_folder(raw, ctx.config)
        if folder is None:
            return ToolResult(False, f"'{raw}' isn't one of your safe folders.")
        roots = [folder]
    else:
        roots = [Path(f) for f in ctx.config.safe_folders]

    q = query.casefold()
    matches, scanned = [], 0
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                scanned += 1
                if q in fname.casefold():
                    matches.append(str(Path(dirpath) / fname))
                if scanned >= _MAX_SCAN or len(matches) >= _MAX_RESULTS:
                    break
            if scanned >= _MAX_SCAN or len(matches) >= _MAX_RESULTS:
                break

    if not matches:
        return ToolResult(True, f"I couldn't find anything matching '{query}'.", data=[])
    listing = "\n".join(matches[:_MAX_RESULTS])
    return ToolResult(
        True,
        f"Found {len(matches)} file(s) matching '{query}':\n{listing}",
        data=matches,
    )


@tool("delete_files")
def delete_files(args: dict, ctx: ToolContext) -> ToolResult:
    # Deliberate stub — per MVP safety policy, deletion is never executed.
    return ToolResult(
        False,
        "Destructive file deletion isn't enabled in this version — I'd rather keep "
        "your files safe. I can open the folder for you so you can review it yourself.",
    )
