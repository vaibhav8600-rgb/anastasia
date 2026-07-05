"""Simple local preference memory (memory.json). Convenience only —
never store passwords, secrets, tokens or financial data here."""

import getpass
import json
from pathlib import Path

from app.config import MEMORY_PATH


def _defaults() -> dict:
    home = str(Path.home()).replace("\\", "/")
    return {
        "assistant_name": "Anastasia",
        "nickname": "Anna",
        "user_name": getpass.getuser(),
        "preferred_browser": "",
        "favorite_project_folder": f"{home}/Projects",
        "work_apps": ["Chrome", "VS Code", "Notepad"],
    }


class Memory:
    def __init__(self, path: Path = MEMORY_PATH):
        self.path = path
        self.data = _defaults()
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    self.data.update(stored)
            except Exception:
                pass  # corrupt memory file -> keep defaults
        else:
            self.save()

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def replace(self, data: dict) -> None:
        self.data = data
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
