from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.ollama import OllamaClient


class OllamaClientTests(unittest.TestCase):
    def test_model_availability_uses_only_local_tags_response(self) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []

        def transport(method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((method, path, payload))
            return {"models": [{"name": "qwen3:4b"}, {"name": "bge-m3:latest"}]}

        client = OllamaClient(transport=transport)

        assert client.model_availability(["qwen3:4b", "bge-m3", "bge-reranker-v2-m3"]) == {
            "qwen3:4b": True,
            "bge-m3": True,
            "bge-reranker-v2-m3": False,
        }
        self.assertEqual(calls, [("GET", "/api/tags", {})])

    def test_accepts_only_http_loopback_base_urls(self) -> None:
        for base_url in (
            "http://localhost",
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://[::1]:11434",
        ):
            with self.subTest(base_url=base_url):
                OllamaClient(base_url=base_url)

    def test_rejects_non_loopback_or_non_http_base_urls(self) -> None:
        for base_url in (
            "https://localhost:11434",
            "http://192.168.1.10:11434",
            "http://localhost.evil.test:11434",
            "ftp://127.0.0.1:11434",
            "http://example.test:11434",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError) as caught:
                    OllamaClient(base_url=base_url)
                self.assertEqual(type(caught.exception).__name__, "InvalidOllamaBaseUrlError")
