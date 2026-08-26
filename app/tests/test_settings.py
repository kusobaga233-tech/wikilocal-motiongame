from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal import settings as settings_module

Settings = settings_module.Settings
SettingsError = getattr(settings_module, "SettingsError", ValueError)


class SettingsTests(unittest.TestCase):
    def write_settings(self, root: Path, contents: str) -> Path:
        settings_path = root / "config" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(contents, encoding="utf-8")
        return settings_path

    def assert_settings_error(self, root: Path) -> None:
        settings_path = root / "config" / "settings.json"
        with self.assertRaises(SettingsError) as error:
            Settings.load(root)
        self.assertIn(str(settings_path), str(error.exception))

    def test_settings_error_is_dedicated(self) -> None:
        self.assertIsNot(SettingsError, ValueError)
        self.assertTrue(issubclass(SettingsError, ValueError))

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

    def test_loads_valid_preexisting_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_settings(
                root,
                """{
  \"daily_time\": \"23:59\",
  \"documents_enabled\": false,
  \"chats_enabled\": true,
  \"chat_history_start\": \"2025-01-01\"
}
""",
            )

            settings = Settings.load(root)

            self.assertEqual(settings.daily_time, "23:59")
            self.assertFalse(settings.documents_enabled)
            self.assertTrue(settings.chats_enabled)
            self.assertEqual(settings.chat_history_start, "2025-01-01")

    def test_load_rejects_invalid_daily_time(self) -> None:
        for daily_time in ("2:00", "02:0", "24:00", "12:60", 200):
            with self.subTest(daily_time=daily_time), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                self.write_settings(
                    root,
                    json.dumps(
                        {
                            "daily_time": daily_time,
                            "documents_enabled": True,
                            "chats_enabled": True,
                            "chat_history_start": None,
                        }
                    ),
                )

                self.assert_settings_error(root)

    def test_load_rejects_non_boolean_enable_switches(self) -> None:
        for field_name, value in (
            ("documents_enabled", 1),
            ("documents_enabled", "true"),
            ("chats_enabled", 0),
            ("chats_enabled", "false"),
        ):
            with self.subTest(field_name=field_name, value=value), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                values = {
                    "daily_time": "02:00",
                    "documents_enabled": True,
                    "chats_enabled": True,
                    "chat_history_start": None,
                }
                values[field_name] = value
                self.write_settings(root, json.dumps(values))

                self.assert_settings_error(root)

    def test_load_rejects_invalid_chat_history_start(self) -> None:
        for chat_history_start in ("2025/01/01", "2025-02-30", "01-01-2025", 20250101):
            with self.subTest(chat_history_start=chat_history_start), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                self.write_settings(
                    root,
                    json.dumps(
                        {
                            "daily_time": "02:00",
                            "documents_enabled": True,
                            "chats_enabled": True,
                            "chat_history_start": chat_history_start,
                        }
                    ),
                )

                self.assert_settings_error(root)

    def test_load_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_settings(root, "{\"daily_time\": \"02:00\"")

            self.assert_settings_error(root)

    def test_load_rejects_non_object_json_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_settings(root, "[]")

            self.assert_settings_error(root)

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

    def test_save_rejects_invalid_values(self) -> None:
        invalid_values = (
            {"daily_time": "9:00"},
            {"documents_enabled": 1},
            {"chats_enabled": "false"},
            {"chat_history_start": "2025-02-30"},
        )

        for values in invalid_values:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temporary_directory:
                settings = Settings(Path(temporary_directory), **values)

                with self.assertRaises(SettingsError) as error:
                    settings.save()
                self.assertIn(str(settings.settings_path), str(error.exception))


if __name__ == "__main__":
    unittest.main()
