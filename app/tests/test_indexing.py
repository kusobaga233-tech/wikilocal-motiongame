from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.indexing import Indexer, LanceDBVectorStore, chunk_text
from wikilocal.retrieval import Retriever
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

    def delete_chunks(self, chunk_ids: list[str] | tuple[str, ...]) -> None:
        self.rows = [row for row in self.rows if row["chunk_id"] not in chunk_ids]

    def add(self, rows: list[dict[str, object]]) -> None:
        self.rows.extend(rows)

    def search(self, embedding: list[float], limit: int) -> list[dict[str, object]]:
        return self.rows[:limit]


class FakeLanceTable:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def add(self, rows: list[dict[str, object]]) -> None:
        self.rows.extend(rows)

    def delete(self, predicate: str) -> None:
        column, value = predicate.split(" = ", 1)
        value = value.strip("'").replace("''", "'")
        self.rows = [row for row in self.rows if row[column] != value]

    def search(self, embedding: list[float]) -> FakeLanceSearch:
        return FakeLanceSearch(self.rows)


class FakeLanceSearch:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def limit(self, value: int) -> FakeLanceSearch:
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

    def drop_table(self, name: str) -> None:
        self.table = None

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
        self.assertEqual(len(vectors.rows), 2)
        self.assertEqual(vectors.rows[0]["chunk_id"], expected_first_id)
        self.assertEqual([row.chunk_id for row in storage.search_fts("alpha", limit=10)], [
            expected_first_id,
            "document:d1:1:" + hashlib.sha256(text[680:].encode("utf-8")).hexdigest()[:12],
        ])

    def test_lance_vector_store_deletes_chunks_and_searches_locally(self) -> None:
        database = FakeLanceDatabase()
        vectors = LanceDBVectorStore(database)
        vectors.add([
            {"chunk_id": "document:d1:0:a", "source_key": "document:d1", "title": "D1", "text_content": "one", "vector": [1.0]},
            {"chunk_id": "document:d2:0:b", "source_key": "document:d2", "title": "D2", "text_content": "two", "vector": [2.0]},
        ])

        vectors.delete_chunks(["document:d1:0:a"])

        self.assertEqual(vectors.search([1.0], 10), [
            {"chunk_id": "document:d2:0:b", "source_key": "document:d2", "title": "D2", "text_content": "two", "vector": [2.0]}
        ])

    def test_lance_vector_store_clear_removes_rows_before_recovery_embeddings(self) -> None:
        database = FakeLanceDatabase()
        vectors = LanceDBVectorStore(database)
        vectors.add([
            {"chunk_id": "document:d1:0:a", "source_key": "document:d1", "title": "D1", "text_content": "old", "vector": [1.0]}
        ])

        vectors.clear()

        self.assertEqual(vectors.search([1.0], 10), [])

    def test_failed_vector_rebuild_clears_stale_rows_and_falls_back_to_fts(self) -> None:
        class RebuildFailingOllama(FakeOllama):
            def __init__(self) -> None:
                self.fail_rebuild = True

            def embed(self, texts: list[str]) -> list[list[float]]:
                if self.fail_rebuild:
                    raise RuntimeError("rebuild embedding failed")
                return super().embed(texts)

            def rerank(self, question: str, texts: list[str]) -> list[float]:
                return [1.0 for _ in texts]

        class FailClosedVectors(FakeVectors):
            def __init__(self) -> None:
                super().__init__()
                self.disabled = False

            def clear(self) -> None:
                self.rows = []
                self.disabled = True

            def disable(self) -> None:
                self.disabled = True

            def replace_all(self, rows: list[dict[str, object]]) -> None:
                self.rows = list(rows)
                self.disabled = False

            def search(self, embedding: list[float], limit: int) -> list[dict[str, object]]:
                return [] if self.disabled else super().search(embedding, limit)

        storage = self.make_storage()
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "restored evidence", {}, True)
        )
        storage.replace_fts_chunks(
            "document:d1", [("document:d1:0:restored", "restored evidence", "Release")]
        )
        ollama = RebuildFailingOllama()
        vectors = FailClosedVectors()
        vectors.rows = [{
            "chunk_id": "document:d1:0:stale",
            "source_key": "document:d1",
            "title": "Release",
            "text_content": "stale evidence",
            "vector": [1.0],
        }]

        with self.assertRaisesRegex(RuntimeError, "rebuild embedding failed"):
            Indexer(storage, ollama, vectors).rebuild_vectors_from_fts()

        self.assertTrue(vectors.disabled)
        self.assertEqual(vectors.rows, [])
        ollama.fail_rebuild = False
        self.assertEqual(
            [item.text_content for item in Retriever(storage, ollama, vectors).search("restored")],
            ["restored evidence"],
        )

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

    def test_empty_source_retires_fts_and_vector_evidence_without_embedding_empty_text(self) -> None:
        class RejectingEmptyEmbedder(FakeOllama):
            def embed(self, texts: list[str]) -> list[list[float]]:
                if not texts:
                    raise AssertionError("empty sources must not be embedded")
                return super().embed(texts)

            def rerank(self, question: str, texts: list[str]) -> list[float]:
                return [1.0 for _ in texts]

        storage = self.make_storage()
        vectors = FakeVectors()
        ollama = RejectingEmptyEmbedder()
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "old keyword", {}, True)
        )
        indexer = Indexer(storage, ollama, vectors)
        indexer.index_source("document:d1")
        storage.upsert_source(SourceRecord("document:d1", "document", "Release", "", {}, True))

        self.assertEqual(indexer.index_source("document:d1"), 0)

        self.assertEqual(storage.search_fts("old", limit=10), [])
        self.assertEqual(Retriever(storage, ollama, vectors).search("old"), [])

    def test_vector_add_failure_preserves_old_fts_and_vector_evidence(self) -> None:
        class RankingOllama(FakeOllama):
            def rerank(self, question: str, texts: list[str]) -> list[float]:
                return [1.0 for _ in texts]

        class FailingNewVectors(FakeVectors):
            def __init__(self) -> None:
                super().__init__()
                self.fail_add = False

            def add(self, rows: list[dict[str, object]]) -> None:
                if self.fail_add:
                    raise RuntimeError("vector add failed")
                super().add(rows)

        storage = self.make_storage()
        vectors = FailingNewVectors()
        ollama = RankingOllama()
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "old keyword", {}, True)
        )
        Indexer(storage, ollama, vectors).index_source("document:d1")
        original_vectors = list(vectors.rows)
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "new keyword", {}, True)
        )
        vectors.fail_add = True

        with self.assertRaisesRegex(RuntimeError, "vector add failed"):
            Indexer(storage, ollama, vectors).index_source("document:d1")

        self.assertEqual([row.text_content for row in storage.search_fts("old", limit=10)], ["old keyword"])
        self.assertEqual(vectors.rows, original_vectors)
        self.assertEqual(
            [item.text_content for item in Retriever(storage, ollama, vectors).search("old")],
            ["old keyword"],
        )

    def test_fts_replace_failure_keeps_old_evidence_retrievable_after_vector_staging(self) -> None:
        class FailingReplaceStorage(Storage):
            def __init__(self, settings: Settings) -> None:
                super().__init__(settings)
                self.fail_replace = False

            def replace_fts_chunks(
                self, source_key: str, chunks: list[tuple[str, str, str]]
            ) -> None:
                if self.fail_replace:
                    raise RuntimeError("FTS replace failed")
                super().replace_fts_chunks(source_key, chunks)

        class RankingOllama(FakeOllama):
            def rerank(self, question: str, texts: list[str]) -> list[float]:
                return [1.0 for _ in texts]

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        storage = FailingReplaceStorage(Settings.load(Path(temporary_directory.name)))
        storage.initialize()
        self.addCleanup(storage.close)
        vectors = FakeVectors()
        ollama = RankingOllama()
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "old keyword", {}, True)
        )
        Indexer(storage, ollama, vectors).index_source("document:d1")
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "new keyword", {}, True)
        )
        storage.fail_replace = True

        with self.assertRaisesRegex(RuntimeError, "FTS replace failed"):
            Indexer(storage, ollama, vectors).index_source("document:d1")

        self.assertEqual(
            [item.text_content for item in Retriever(storage, ollama, vectors).search("old")],
            ["old keyword"],
        )
        self.assertEqual(
            [item.text_content for item in Retriever(storage, ollama, vectors).search("new")],
            ["old keyword"],
        )
        self.assertEqual([row["text_content"] for row in vectors.rows], ["old keyword"])

    def test_failed_vector_cleanup_cannot_return_retired_evidence(self) -> None:
        class FailingCleanupVectors(FakeVectors):
            def delete_chunks(self, chunk_ids: list[str] | tuple[str, ...]) -> None:
                if chunk_ids:
                    raise RuntimeError("vector cleanup failed")
                super().delete_chunks(chunk_ids)

        class RankingOllama(FakeOllama):
            def rerank(self, question: str, texts: list[str]) -> list[float]:
                return [1.0 for _ in texts]

        storage = self.make_storage()
        vectors = FailingCleanupVectors()
        ollama = RankingOllama()
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "old keyword", {}, True)
        )
        indexer = Indexer(storage, ollama, vectors)
        indexer.index_source("document:d1")
        storage.upsert_source(
            SourceRecord("document:d1", "document", "Release", "new keyword", {}, True)
        )

        indexer.index_source("document:d1")

        self.assertEqual(
            [item.text_content for item in Retriever(storage, ollama, vectors).search("old")],
            ["new keyword"],
        )
