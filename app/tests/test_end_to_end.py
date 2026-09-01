from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.indexing import Indexer
from wikilocal.retrieval import AnswerService, Retriever
from wikilocal.settings import Settings
from wikilocal.storage import Storage
from wikilocal.sync_documents import DocumentSynchronizer, SyncResult


class FakeFeishu:
    def list_wiki_spaces(self, *, page_token: str | None = None) -> dict[str, object]:
        assert page_token is None
        return {"items": [{"space_id": "product", "name": "Product"}]}

    def list_wiki_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, object]:
        assert space_id == "product"
        assert parent_node_token is None
        assert page_token is None
        return {
            "items": [
                {
                    "node_token": "release-plan-node",
                    "obj_token": "release-plan",
                    "obj_type": "docx",
                    "title": "Release plan",
                    "url": "https://example.test/wiki/release-plan",
                    "obj_edit_time": "2026-08-28T02:00:00Z",
                }
            ]
        }

    def read_document(self, document: str) -> dict[str, object]:
        assert document == "release-plan"
        return {
            "document": {
                "document_id": document,
                "content": "The release moves deployment to Friday.",
            }
        }


class FakeOllama:
    def __init__(self) -> None:
        self.embedded: list[list[str]] = []
        self.reranked: list[tuple[str, list[str]]] = []
        self.generated: list[tuple[str, str, int]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.append(texts)
        return [[float(len(text))] for text in texts]

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        self.reranked.append((question, texts))
        return [1.0 for _ in texts]

    def generate(self, model: str, prompt: str, *, num_ctx: int) -> str:
        self.generated.append((model, prompt, num_ctx))
        return "Deployment moves to Friday. [1]"


class FakeVectors:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.searched: list[tuple[list[float], int]] = []

    def delete_source(self, source_key: str) -> None:
        self.rows = [row for row in self.rows if row["source_key"] != source_key]

    def delete_chunks(self, chunk_ids: list[str] | tuple[str, ...]) -> None:
        self.rows = [row for row in self.rows if row["chunk_id"] not in chunk_ids]

    def add(self, rows: list[dict[str, object]]) -> None:
        self.rows.extend(rows)

    def search(self, embedding: list[float], limit: int) -> list[dict[str, object]]:
        self.searched.append((embedding, limit))
        return self.rows[:limit]


class EndToEndTests(unittest.TestCase):
    def test_fake_document_sync_index_retrieval_and_answer_keep_release_plan_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings.load(Path(temporary_directory))
            storage = Storage(settings)
            storage.initialize()
            vectors = FakeVectors()
            ollama = FakeOllama()

            try:
                sync_result = DocumentSynchronizer(settings, storage, FakeFeishu()).sync()
                indexed = Indexer(storage, ollama, vectors).index_source("document:release-plan")
                answer = AnswerService(Retriever(storage, ollama, vectors), ollama).answer(
                    "When is the deployment?"
                )
            finally:
                storage.close()

        self.assertEqual(sync_result, SyncResult(created=1, changed=0, skipped=0, failed=0))
        self.assertEqual(indexed, 1)
        self.assertEqual(answer.text, "Deployment moves to Friday. [1]")
        self.assertEqual([citation.source_key for citation in answer.citations], ["document:release-plan"])
        self.assertTrue(ollama.embedded)
        self.assertTrue(vectors.searched)
        self.assertTrue(ollama.reranked)
        self.assertEqual(ollama.generated[0][0], "qwen3:4b")
        self.assertEqual(ollama.generated[0][2], 8192)
        self.assertIn("[1] Release plan", ollama.generated[0][1])
        self.assertIn("Source key: document:release-plan", ollama.generated[0][1])
        self.assertIn("The release moves deployment to Friday.", ollama.generated[0][1])
        self.assertIn(
            "Cite every factual claim with its numeric evidence marker, such as [1].",
            ollama.generated[0][1],
        )
