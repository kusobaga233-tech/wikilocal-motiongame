from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.ollama import ModelUnavailableError, OllamaClient
from wikilocal.retrieval import AnswerService, Evidence, Retriever
from wikilocal.settings import Settings
from wikilocal.storage import SourceRecord, Storage


class FakeOllama:
    def __init__(self) -> None:
        self.rerank_inputs: list[list[str]] = []
        self.prompts: list[str] = []

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        self.rerank_inputs.append(texts)
        return [float(index) for index in range(len(texts), 0, -1)]

    def generate(self, model: str, prompt: str, *, num_ctx: int) -> str:
        self.prompts.append(prompt)
        self.assert_model = model
        self.assert_num_ctx = num_ctx
        return "The release changed the deployment date. [1]"


class FakeVectors:
    def search(self, embedding: list[float], limit: int) -> list[dict[str, object]]:
        return [
            {
                "chunk_id": "document:d1:0:abc",
                "source_key": "document:d1",
                "title": "Release plan",
                "text_content": "Deployment date moved to Friday.",
                "_distance": 0.1,
            },
            {
                "chunk_id": "document:d2:0:def",
                "source_key": "document:d2",
                "title": "Other",
                "text_content": "Unrelated notes.",
                "_distance": 0.2,
            },
        ][:limit]


class FakeEmbeddingOllama(FakeOllama):
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]


class RetrievalTests(unittest.TestCase):
    def make_storage(self) -> Storage:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        storage = Storage(Settings.load(Path(self.temporary_directory.name)))
        storage.initialize()
        self.addCleanup(storage.close)
        return storage

    def test_search_merges_duplicate_hits_reranks_at_most_twenty_and_returns_eight(self) -> None:
        storage = self.make_storage()
        for index in range(10):
            source_key = f"document:d{index}"
            storage.upsert_source(SourceRecord(source_key, "document", f"Doc {index}", "body", {}, True))
            storage.replace_fts_chunks(
                source_key,
                [(f"{source_key}:0:x", f"keyword {index}", f"Doc {index}")],
            )
        ollama = FakeEmbeddingOllama()

        results = Retriever(storage, ollama, FakeVectors()).search("keyword", limit=99)

        self.assertLessEqual(len(results), 8)
        self.assertEqual(len({item.chunk_id for item in results}), len(results))
        self.assertLessEqual(len(ollama.rerank_inputs[0]), 20)
        self.assertEqual(results[0].source_key, "document:d0")

    def test_answer_keeps_citations_independent_from_model_text_and_uses_only_evidence(self) -> None:
        ollama = FakeOllama()
        evidence = Evidence(
            chunk_id="document:d1:0:abc",
            source_key="document:d1",
            title="Release plan",
            text_content="Deployment date moved to Friday.",
            metadata={"url": "https://example.test/d1"},
        )

        class StaticRetriever:
            def search(self, question: str, limit: int = 8) -> list[Evidence]:
                return [evidence]

        answer = AnswerService(StaticRetriever(), ollama).answer("What changed?")

        self.assertEqual(answer.citations[0].source_key, "document:d1")
        self.assertEqual(ollama.assert_model, "qwen3:4b")
        self.assertEqual(ollama.assert_num_ctx, 8192)
        self.assertIn("Deployment date moved to Friday.", ollama.prompts[0])
        self.assertNotIn("unrelated", ollama.prompts[0].lower())

    def test_ollama_client_wraps_local_transport_failures_as_model_unavailable(self) -> None:
        def unavailable(method: str, path: str, body: dict[str, object]) -> dict[str, object]:
            raise OSError("connection refused")

        with self.assertRaises(ModelUnavailableError):
            OllamaClient(transport=unavailable).embed(["hello"])

