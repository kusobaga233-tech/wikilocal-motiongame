from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.indexing import Indexer, LanceDBVectorStore, chunk_text
from wikilocal.settings import Settings
from wikilocal.storage import SourceRecord, Storage


class FakeOllama:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


class FailingOllama:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding model unavailable")


class FakeVectors:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.rows: list[dict[str, object]] = []

    def delete_source(self, source_key: str) -> None:
        self.deleted.append(source_key)
        self.rows = [row for row in self.rows if row["source_key"] != source_key]

    def add(self, rows: list[dict[str, object]]) -> None:
        self.rows.extend(rows)


class FakeLanceTable:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def add(self, rows: list[dict[str, object]]) -> None:
        self.rows.extend(rows)

    def delete(self, predicate: str) -> None:
        source_key = predicate.split(" = ", 1)[1].strip("'").replace("''", "'")
        self.rows = [row for row in self.rows if row["source_key"] != source_key]

    def search(self, embedding: list[float]) -> "FakeLanceSearch":
        return FakeLanceSearch(self.rows)


class FakeLanceSearch:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def limit(self, value: int) -> "FakeLanceSearch":
        self._limit = value
        return self

    def to_list(self) -> list[dict[str, object]]:
        return self._rows[: self._limit]


class FakeLanceDatabase:
    def __init__(self) -> None:
        self.table: FakeLanceTable | None = None

    def table_names(self) -> list[str]:
        return [] if self.table is None else ["chunks"]

    def create_table(self, name: str, data: list[dict[str, object]]) -> FakeLanceTable:
        self.table = FakeLanceTable()
        self.table.add(data)
        return self.table

    def open_table(self, name: str) -> FakeLanceTable:
        assert self.table is not None
        return self.table


class IndexingTests(unittest.TestCase):
    def make_storage(self) -> Storage:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        storage = Storage(Settings.load(Path(self.temporary_directory.name)))
        storage.initialize()
        self.addCleanup(storage.close)
        return storage

    def test_chunk_text_uses_800_character_windows_with_120_character_overlap(self) -> None:
        text = "x" * 1000

        chunks = chunk_text(text)

        self.assertEqual(chunks, [text[:800], text[680:]])

    def test_index_source_replaces_stale_fts_and_vector_chunks(self) -> None:
        storage = self.make_storage()
        text = "alpha " * 200
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", text, {"url": "https://x"}, True)
        )
        vectors = FakeVectors()
        indexer = Indexer(storage, FakeOllama(), vectors)

        self.assertEqual(indexer.index_source("document:d1"), 2)
        self.assertEqual(indexer.index_source("document:d1"), 2)

        expected_first_id = "document:d1:0:" + hashlib.sha256(text[:800].encode("utf-8")).hexdigest()[:12]
        self.assertEqual(vectors.deleted, ["document:d1", "document:d1"])
        self.assertEqual(len(vectors.rows), 2)
        self.assertEqual(vectors.rows[0]["chunk_id"], expected_first_id)
        self.assertEqual([row.chunk_id for row in storage.search_fts("alpha", limit=10)], [
            expected_first_id,
            "document:d1:1:" + hashlib.sha256(text[680:].encode("utf-8")).hexdigest()[:12],
        ])

    def test_lance_vector_store_replaces_source_rows_and_searches_locally(self) -> None:
        database = FakeLanceDatabase()
        vectors = LanceDBVectorStore(database)
        vectors.add([
            {"chunk_id": "document:d1:0:a", "source_key": "document:d1", "title": "D1", "text_content": "one", "vector": [1.0]},
            {"chunk_id": "document:d2:0:b", "source_key": "document:d2", "title": "D2", "text_content": "two", "vector": [2.0]},
        ])

        vectors.delete_source("document:d1")

        self.assertEqual(vectors.search([1.0], 10), [
            {"chunk_id": "document:d2:0:b", "source_key": "document:d2", "title": "D2", "text_content": "two", "vector": [2.0]}
        ])

    def test_embed_failure_preserves_existing_fts_and_vector_chunks(self) -> None:
        storage = self.make_storage()
        storage.upsert_source(SourceRecord("document:d1", "document", "Release", "old keyword", {}, True))
        vectors = FakeVectors()
        Indexer(storage, FakeOllama(), vectors).index_source("document:d1")
        original_fts = storage.search_fts("old", limit=10)
        original_vectors = list(vectors.rows)
        storage.upsert_source(SourceRecord("document:d1", "document", "Release", "new keyword", {}, True))

        with self.assertRaisesRegex(RuntimeError, "embedding model unavailable"):
            Indexer(storage, FailingOllama(), vectors).index_source("document:d1")

        self.assertEqual(storage.search_fts("old", limit=10), original_fts)
        self.assertEqual(vectors.rows, original_vectors)
