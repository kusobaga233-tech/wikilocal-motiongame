from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelUnavailableError(RuntimeError):
    """Raised when a required local Ollama endpoint or model is unavailable."""


HttpTransport = Callable[[str, str, dict[str, object]], Mapping[str, object]]


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        transport: HttpTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport or self._http_request

    def embed(self, texts: Sequence[str], *, model: str = "bge-m3") -> list[list[float]]:
        if not texts:
            return []
        payload = self._call("POST", "/api/embed", {"model": model, "input": list(texts)})
        raw_embeddings = payload.get("embeddings")
        if not isinstance(raw_embeddings, list):
            raise ModelUnavailableError("Local Ollama embedding response is invalid.")
        embeddings: list[list[float]] = []
        for embedding in raw_embeddings:
            if not isinstance(embedding, list) or not all(
                isinstance(value, (int, float)) for value in embedding
            ):
                raise ModelUnavailableError("Local Ollama embedding response is invalid.")
            embeddings.append([float(value) for value in embedding])
        if len(embeddings) != len(texts):
            raise ModelUnavailableError("Local Ollama returned an unexpected embedding count.")
        return embeddings

    def rerank(
        self, question: str, texts: Sequence[str], *, model: str = "bge-reranker-v2-m3"
    ) -> list[float]:
        if not texts:
            return []
        payload = self._call(
            "POST", "/api/rerank", {"model": model, "query": question, "documents": list(texts)}
        )
        values = payload.get("scores")
        if not isinstance(values, list) or len(values) != len(texts):
            raise ModelUnavailableError("Local Ollama reranker response is invalid.")
        if not all(isinstance(value, (int, float)) for value in values):
            raise ModelUnavailableError("Local Ollama reranker response is invalid.")
        return [float(value) for value in values]

    def generate(self, model: str, prompt: str, *, num_ctx: int) -> str:
        payload = self._call(
            "POST",
            "/api/generate",
            {"model": model, "prompt": prompt, "stream": False, "options": {"num_ctx": num_ctx}},
        )
        response = payload.get("response")
        if not isinstance(response, str):
            raise ModelUnavailableError("Local Ollama generation response is invalid.")
        return response

    def _call(self, method: str, path: str, payload: dict[str, object]) -> Mapping[str, object]:
        try:
            response = self._transport(method, path, payload)
        except (OSError, HTTPError, URLError, TimeoutError, ValueError) as error:
            raise ModelUnavailableError("Local Ollama is unavailable. Start Ollama and pull the required model.") from error
        if not isinstance(response, Mapping):
            raise ModelUnavailableError("Local Ollama response is invalid.")
        return response

    def _http_request(self, method: str, path: str, payload: dict[str, object]) -> Mapping[str, object]:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=60) as response:  # nosec B310: loopback URL is configurable by user.
            decoded: Any = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise ValueError("Ollama did not return a JSON object.")
        return decoded
