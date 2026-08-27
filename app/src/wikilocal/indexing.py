from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from wikilocal.storage import Storage


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    def delete_source(self, source_key: str) -> None: ...

    def add(self, rows: Sequence[Mapping[str, object]]) -> None: ...


class LanceDBVectorStore:
    """Small adapter around a local LanceDB table, imported only when enabled."""

    table_name = "chunks"

    def __init__(self, database: object) -> None:
        self._database = database
        self._table: object | None = None

    @classmethod
    def open(cls, directory: Path) -> "LanceDBVectorStore":
        try:
            import lancedb
        except ImportError as error:
            raise RuntimeError("LanceDB is not installed; install the wikilocal vector extra.") from error
        directory.mkdir(parents=True, exist_ok=True)
        return cls(lancedb.connect(str(directory)))

    def delete_source(self, source_key: str) -> None:
        table = self._existing_table()
        if table is not None:
            table.delete(f"source_key = '{source_key.replace("'", "''")}'")

    def add(self, rows: Sequence[Mapping[str, object]]) -> None:
        values = [dict(row) for row in rows]
        if not values:
            return
        table = self._existing_table()
        if table is None:
            self._table = self._database.create_table(self.table_name, data=values)
        else:
            table.add(values)

    def search(self, embedding: Sequence[float], limit: int) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        table = self._existing_table()
        if table is None:
            return []
        return [dict(row) for row in table.search(list(embedding)).limit(limit).to_list()]

    def _existing_table(self) -> object | None:
        if self._table is not None:
            return self._table
        if self.table_name not in self._database.table_names():
            return None
        self._table = self._database.open_table(self.table_name)
        return self._table


def chunk_text(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("size must be positive and overlap must be smaller than size.")
    if not text:
        return []
    step = size - overlap
    return [text[start : start + size] for start in range(0, len(text), step)]


class Indexer:
    def __init__(
        self, storage: Storage, ollama: Embedder, vector_store: VectorStore | None = None
    ) -> None:
        self._storage = storage
        self._ollama = ollama
        self._vector_store = vector_store

    def index_source(self, source_key: str) -> int:
        source = self._storage.get_source(source_key)
        if source is None:
            raise KeyError(f"Unknown source: {source_key}")
        chunks = chunk_text(source.text_content)
        fts_rows = [(_chunk_id(source_key, ordinal, text), text, source.title) for ordinal, text in enumerate(chunks)]

        if self._vector_store is not None:
            embeddings = self._ollama.embed(chunks)
            rows = [
                {
                    "chunk_id": chunk_id,
                    "source_key": source_key,
                    "title": source.title,
                    "text_content": text,
                    "vector": embedding,
                }
                for (chunk_id, text, _title), embedding in zip(fts_rows, embeddings, strict=True)
            ]
            self._storage.replace_fts_chunks(source_key, fts_rows)
            self._vector_store.delete_source(source_key)
            if rows:
                self._vector_store.add(rows)
        else:
            self._storage.replace_fts_chunks(source_key, fts_rows)
        return len(chunks)


def _chunk_id(source_key: str, ordinal: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{source_key}:{ordinal}:{digest}"
