from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

_DAILY_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class SettingsError(ValueError):
    """Raised when the settings configuration is invalid."""


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
    def load(cls, root: Path) -> Settings:
        root = Path(root)
        settings_path = root / "config" / "settings.json"
        if settings_path.exists():
            try:
                with settings_path.open("r", encoding="utf-8") as settings_file:
                    values = json.load(settings_file)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SettingsError(
                    f"Unable to parse settings at {settings_path}: {error}"
                ) from error

            if not isinstance(values, dict):
                raise SettingsError(
                    f"Settings at {settings_path} must have a JSON object root."
                )

            try:
                settings = cls(root=root, **values)
            except TypeError as error:
                raise SettingsError(
                    f"Invalid settings object at {settings_path}: {error}"
                ) from error
        else:
            settings = cls(root=root)

        settings.validate()
        settings.ensure_directories()
        if not settings_path.exists():
            settings.save()
        return settings

    def save(self) -> None:
        self.validate()
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

    def validate(self) -> None:
        config_path = self.settings_path
        if (
            type(self.daily_time) is not str
            or _DAILY_TIME_PATTERN.fullmatch(self.daily_time) is None
        ):
            raise SettingsError(
                f"Invalid daily_time in {config_path}: expected HH:MM in 24-hour time."
            )
        try:
            time.fromisoformat(self.daily_time)
        except ValueError as error:
            raise SettingsError(
                f"Invalid daily_time in {config_path}: expected HH:MM in 24-hour time."
            ) from error

        for field_name in ("documents_enabled", "chats_enabled"):
            if type(getattr(self, field_name)) is not bool:
                raise SettingsError(
                    f"Invalid {field_name} in {config_path}: expected a boolean."
                )

        if self.chat_history_start is None:
            return
        if (
            type(self.chat_history_start) is not str
            or _ISO_DATE_PATTERN.fullmatch(self.chat_history_start) is None
        ):
            raise SettingsError(
                f"Invalid chat_history_start in {config_path}: expected YYYY-MM-DD."
            )
        try:
            date.fromisoformat(self.chat_history_start)
        except ValueError as error:
            raise SettingsError(
                f"Invalid chat_history_start in {config_path}: expected YYYY-MM-DD."
            ) from error

    def ensure_directories(self) -> None:
        for relative_path in (
            "data/documents",
            "data/index",
            "data/logs",
            "models",
            "config",
        ):
            (self.root / relative_path).mkdir(parents=True, exist_ok=True)
