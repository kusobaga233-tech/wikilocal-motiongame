from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_repository_default_settings_are_tracked(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        settings_path = repository_root / "config" / "settings.json"

        self.assertEqual(
            json.loads(settings_path.read_text(encoding="utf-8")),
            {
                "daily_time": "02:00",
                "documents_enabled": True,
                "chats_enabled": True,
                "chat_history_start": None,
            },
        )

    def test_load_creates_defaults_and_required_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            settings = Settings.load(root)

            self.assertEqual(settings.daily_time, "02:00")
            self.assertTrue(settings.documents_enabled)
            self.assertTrue(settings.chats_enabled)
            self.assertIsNone(settings.chat_history_start)
            self.assertEqual(
                settings.database_path,
                root / "data" / "index" / "wikilocal.sqlite3",
            )
            self.assertEqual(
                (root / "config" / "settings.json").read_text(encoding="utf-8"),
                "{\n  \"daily_time\": \"02:00\",\n  \"documents_enabled\": true,\n"
                "  \"chats_enabled\": true,\n  \"chat_history_start\": null\n}\n",
            )

            for relative_path in (
                "data/documents",
                "data/index",
                "data/logs",
                "models",
                "config",
            ):
                self.assertTrue((root / relative_path).is_dir())

    def test_save_persists_updated_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = Settings.load(root)
            settings.daily_time = "03:30"
            settings.documents_enabled = False
            settings.chats_enabled = False
            settings.chat_history_start = "2025-01-01"

            settings.save()
            loaded = Settings.load(root)

            self.assertEqual(loaded.daily_time, "03:30")
            self.assertFalse(loaded.documents_enabled)
            self.assertFalse(loaded.chats_enabled)
            self.assertEqual(loaded.chat_history_start, "2025-01-01")


if __name__ == "__main__":
    unittest.main()
