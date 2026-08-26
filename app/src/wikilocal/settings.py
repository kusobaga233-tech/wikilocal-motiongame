from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    root: Path
    daily_time: str = "02:00"
    documents_enabled: bool = True
    chats_enabled: bool = True
    chat_history_start: str | None = None

    @property
    def database_path(self) -> Path:
        return self.root / "data" / "index" / "wikilocal.sqlite3"

    @property
    def settings_path(self) -> Path:
        return self.root / "config" / "settings.json"

    @classmethod
    def load(cls, root: Path) -> "Settings":
        root = Path(root)
        settings_path = root / "config" / "settings.json"
        if settings_path.exists():
            with settings_path.open("r", encoding="utf-8") as settings_file:
                values = json.load(settings_file)
            settings = cls(root=root, **values)
        else:
            settings = cls(root=root)
            settings.save()

        settings.ensure_directories()
        return settings

    def save(self) -> None:
        self.ensure_directories()
        values = {
            "daily_time": self.daily_time,
            "documents_enabled": self.documents_enabled,
            "chats_enabled": self.chats_enabled,
            "chat_history_start": self.chat_history_start,
        }
        with self.settings_path.open("w", encoding="utf-8", newline="\n") as settings_file:
            json.dump(values, settings_file, ensure_ascii=False, indent=2)
            settings_file.write("\n")

    def ensure_directories(self) -> None:
        for relative_path in (
            "data/documents",
            "data/index",
            "data/logs",
            "models",
            "config",
        ):
            (self.root / relative_path).mkdir(parents=True, exist_ok=True)
