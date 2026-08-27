from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.ollama import OllamaClient


class OllamaClientTests(unittest.TestCase):
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
