from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from wikilocal.storage import ChunkRecord, Storage


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    source_key: str
    title: str
    text_content: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[Evidence, ...]


class RetrievalModel(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def rerank(self, question: str, texts: Sequence[str]) -> list[float]: ...


class GenerationModel(Protocol):
    def generate(self, model: str, prompt: str, *, num_ctx: int) -> str: ...


class SearchableVectorStore(Protocol):
    def search(self, embedding: Sequence[float], limit: int) -> Sequence[Mapping[str, object]]: ...


class Retriever:
    _MAX_CANDIDATES = 20
    _FTS_QUOTA_WITH_VECTORS = 12

    def __init__(
        self, storage: Storage, ollama: RetrievalModel, vector_store: SearchableVectorStore | None = None
    ) -> None:
        self._storage = storage
        self._ollama = ollama
        self._vector_store = vector_store

    def search(self, question: str, limit: int = 8) -> list[Evidence]:
        if not question.strip() or limit <= 0:
            return []
        fts_candidates = self._from_fts(question)
        vector_candidates: list[Evidence] = []
        if self._vector_store is not None:
            embedding = self._ollama.embed([question])[0]
            vector_candidates = self._from_vectors(
                self._vector_store.search(embedding, self._MAX_CANDIDATES)
            )
        unique = _select_candidates(
            fts_candidates,
            vector_candidates,
            self._MAX_CANDIDATES,
            self._FTS_QUOTA_WITH_VECTORS,
        )
        if not unique:
            return []
        scores = self._ollama.rerank(question, [evidence.text_content for evidence in unique])
        ranked = [evidence for _score, _index, evidence in sorted(
            ((score, index, evidence) for index, (evidence, score) in enumerate(zip(unique, scores, strict=True))),
            key=lambda item: (-item[0], item[1]),
        )]
        return ranked[: min(limit, 8)]

    def _from_fts(self, question: str) -> list[Evidence]:
        try:
            rows = self._storage.search_fts(question, limit=20)
        except Exception:
            return []
        return [evidence for row in rows if (evidence := self._from_chunk(row)) is not None]

    def _from_chunk(self, chunk: ChunkRecord) -> Evidence | None:
        source = self._storage.get_source(chunk.source_key)
        if source is None or not source.active:
            return None
        return Evidence(
            chunk_id=chunk.chunk_id,
            source_key=chunk.source_key,
            title=chunk.title,
            text_content=chunk.text_content,
            metadata=source.metadata,
        )

    def _from_vectors(self, rows: Sequence[Mapping[str, object]]) -> list[Evidence]:
        evidence: list[Evidence] = []
        for row in rows:
            required = ("chunk_id", "source_key", "title", "text_content")
            if not all(isinstance(row.get(key), str) for key in required):
                continue
            source = self._storage.get_source(str(row["source_key"]))
            if source is None or not source.active:
                continue
            evidence.append(
                Evidence(
                    chunk_id=str(row["chunk_id"]),
                    source_key=str(row["source_key"]),
                    title=str(row["title"]),
                    text_content=str(row["text_content"]),
                    metadata=source.metadata,
                )
            )
        return evidence


class AnswerService:
    def __init__(self, retriever: Any, ollama: GenerationModel) -> None:
        self._retriever = retriever
        self._ollama = ollama

    def answer(self, question: str) -> Answer:
        evidence = tuple(self._retriever.search(question, limit=8))
        if not evidence:
            return Answer("Insufficient evidence in the local knowledge base to answer this question.", ())
        prompt = _answer_prompt(question, evidence)
        text = self._ollama.generate("qwen3:4b", prompt, num_ctx=8192)
        return Answer(text=text, citations=evidence)


def _deduplicate(candidates: Sequence[Evidence]) -> list[Evidence]:
    unique: dict[str, Evidence] = {}
    for candidate in candidates:
        unique.setdefault(candidate.chunk_id, candidate)
    return list(unique.values())


def _select_candidates(
    fts_candidates: Sequence[Evidence],
    vector_candidates: Sequence[Evidence],
    limit: int,
    fts_quota_with_vectors: int,
) -> list[Evidence]:
    """Keep deterministic FTS and vector quotas before the shared reranking cap."""
    fts_unique = _deduplicate(fts_candidates)
    if not vector_candidates:
        return fts_unique[:limit]

    selected = fts_unique[:fts_quota_with_vectors]
    seen = {candidate.chunk_id for candidate in selected}
    for candidate in _deduplicate(vector_candidates):
        if candidate.chunk_id not in seen:
            selected.append(candidate)
            seen.add(candidate.chunk_id)
        if len(selected) == limit:
            return selected
    for candidate in fts_unique[fts_quota_with_vectors:]:
        if candidate.chunk_id not in seen:
            selected.append(candidate)
            seen.add(candidate.chunk_id)
        if len(selected) == limit:
            break
    return selected


def _answer_prompt(question: str, evidence: Sequence[Evidence]) -> str:
    sources = "\n\n".join(
        f"[{index}] {item.title}\nSource key: {item.source_key}\n{item.text_content}"
        for index, item in enumerate(evidence, start=1)
    )
    return (
        "Answer using only the evidence below. Cite every factual claim with its numeric "
        "evidence marker, such as [1]. If the evidence does not support the answer, say "
        "that there is insufficient evidence. Do not invent sources.\n\n"
        f"Question: {question}\n\nEvidence:\n{sources}"
    )
