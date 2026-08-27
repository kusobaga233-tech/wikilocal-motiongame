from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.settings import Settings
from wikilocal.storage import SourceRecord, Storage


class StorageTests(unittest.TestCase):
    def make_storage(self) -> Storage:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        settings = Settings.load(Path(self.temporary_directory.name))
        storage = Storage(settings)
        storage.initialize()
        self.addCleanup(storage.close)
        return storage

    def test_upsert_replaces_an_existing_source(self) -> None:
        storage = self.make_storage()
        storage.upsert_source(
            SourceRecord(
                source_key="document:d1",
                source_type="document",
                title="First title",
                text_content="first body",
                metadata={"url": "https://example.test/d1"},
                active=True,
            )
        )
        storage.upsert_source(
            SourceRecord(
                source_key="document:d1",
                source_type="document",
                title="Updated title",
                text_content="second body",
                metadata={"url": "https://example.test/d1", "source_updated_at": "2026-08-26T02:00:00Z"},
                active=False,
            )
        )

        sources = storage.list_sources()

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source_key, "document:d1")
        self.assertEqual(sources[0].title, "Updated title")
        self.assertEqual(sources[0].text_content, "second body")
        self.assertFalse(sources[0].active)
        self.assertEqual(sources[0].metadata["source_updated_at"], "2026-08-26T02:00:00Z")

    def test_checkpoint_round_trips_json(self) -> None:
        storage = self.make_storage()
        cursor = {"message_id": "om_2", "page_token": None, "seen": ["om_1", "om_2"]}

        storage.set_checkpoint("chat:oc_1", cursor)

        self.assertEqual(storage.get_checkpoint("chat:oc_1"), cursor)
        self.assertIsNone(storage.get_checkpoint("chat:missing"))

    def test_initialize_creates_required_tables(self) -> None:
        storage = self.make_storage()

        with closing(sqlite3.connect(storage.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                )
            }
            source_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sources)")
            }
            checkpoint_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(checkpoints)")
            }

        self.assertTrue({"sources", "checkpoints", "chunks_fts"}.issubset(tables))
        self.assertEqual(
            source_columns,
            {
                "source_key",
                "source_type",
                "title",
                "text_content",
                "metadata_json",
                "active",
                "content_hash",
                "source_updated_at",
                "synced_at",
            },
        )
        self.assertEqual(checkpoint_columns, {"checkpoint_key", "cursor_json", "updated_at"})

    def test_chunks_fts_has_expected_column_order_and_unindexed_columns(self) -> None:
        storage = self.make_storage()

        with closing(sqlite3.connect(storage.database_path)) as connection:
            column_names = [row[1] for row in connection.execute("PRAGMA table_info(chunks_fts)")]
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?",
                ("chunks_fts",),
            ).fetchone()[0]

        self.assertEqual(column_names, ["chunk_id", "text_content", "title", "source_key"])
        self.assertIn("chunk_id UNINDEXED", definition)
        self.assertIn("source_key UNINDEXED", definition)

    def test_metadata_json_is_stable_regardless_of_mapping_key_order(self) -> None:
        storage = self.make_storage()
        first = {"z": 3, "nested": {"b": 2, "a": 1}, "a": 0}
        second = {"a": 0, "nested": {"a": 1, "b": 2}, "z": 3}

        storage.upsert_source(
            SourceRecord("document:d1", "document", "Title", "Body", first, True)
        )
        with closing(sqlite3.connect(storage.database_path)) as connection:
            first_json = connection.execute(
                "SELECT metadata_json FROM sources WHERE source_key = ?", ("document:d1",)
            ).fetchone()[0]

        storage.upsert_source(
            SourceRecord("document:d1", "document", "Title", "Body", second, True)
        )
        with closing(sqlite3.connect(storage.database_path)) as connection:
            second_json = connection.execute(
                "SELECT metadata_json FROM sources WHERE source_key = ?", ("document:d1",)
            ).fetchone()[0]

        self.assertEqual(first_json, '{"a":0,"nested":{"a":1,"b":2},"z":3}')
        self.assertEqual(second_json, first_json)

    def test_search_fts_excludes_chunks_owned_by_inactive_sources(self) -> None:
        storage = self.make_storage()
        storage.upsert_source(SourceRecord("document:active", "document", "Active", "body", {}, True))
        storage.upsert_source(SourceRecord("document:inactive", "document", "Inactive", "body", {}, False))
        storage.replace_fts_chunks(
            "document:active", [("document:active:0:x", "keyword active", "Active")]
        )
        storage.replace_fts_chunks(
            "document:inactive", [("document:inactive:0:x", "keyword inactive", "Inactive")]
        )

        results = storage.search_fts("keyword", limit=20)

        self.assertEqual([chunk.source_key for chunk in results], ["document:active"])
