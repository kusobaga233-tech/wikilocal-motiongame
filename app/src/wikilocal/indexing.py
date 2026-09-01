from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from wikilocal.storage import Storage


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    def vectors_disabled(self) -> bool: ...

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None: ...

    def add(self, rows: Sequence[Mapping[str, object]]) -> None: ...

    def clear(self) -> None: ...

    def disable(self) -> None: ...

    def replace_all(self, rows: Sequence[Mapping[str, object]]) -> None: ...


class LanceDBVectorStore:
    """Small adapter around a local LanceDB table, imported only when enabled."""

    table_name = "chunks"

    def __init__(self, database: object, disabled_marker: Path | None = None) -> None:
        self._database = database
        self._table: object | None = None
        self._disabled_marker = disabled_marker
        self._disabled = disabled_marker.is_file() if disabled_marker is not None else False

    @classmethod
    def open(cls, directory: Path) -> LanceDBVectorStore:
        try:
            import lancedb
        except ImportError as error:
            raise RuntimeError("LanceDB is not installed; install the wikilocal vector extra.") from error
        directory.mkdir(parents=True, exist_ok=True)
        return cls(lancedb.connect(str(directory)), directory / ".vector-search-disabled")

    def delete_source(self, source_key: str) -> None:
        table = self._existing_table()
        if table is not None:
            table.delete(f"source_key = '{source_key.replace("'", "''")}'")

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        table = self._existing_table()
        if table is None:
            return
        for chunk_id in chunk_ids:
            table.delete(f"chunk_id = '{chunk_id.replace("'", "''")}'")

    def add(self, rows: Sequence[Mapping[str, object]]) -> None:
        values = [dict(row) for row in rows]
        if not values:
            return
        table = self._existing_table()
        if table is None:
            self._table = self._database.create_table(self.table_name, data=values)
        else:
            table.add(values)

    def disable(self) -> None:
        """Fail closed so retrieval falls back to SQLite FTS after recovery trouble."""
        self._disabled = True
        if self._disabled_marker is not None:
            try:
                self._disabled_marker.write_text("disabled\n", encoding="utf-8", newline="\n")
            except OSError:
                return

    def vectors_disabled(self) -> bool:
        return self._disabled

    def clear(self) -> None:
        """Remove all vectors before rebuilding from the authoritative FTS state."""
        self.disable()
        try:
            if self._existing_table() is not None:
                self._database.drop_table(self.table_name)
            self._table = None
        except Exception:
            self._table = None
            raise

    def replace_all(self, rows: Sequence[Mapping[str, object]]) -> None:
        """Replace every vector row; leave vector search disabled on any failure."""
        values = [dict(row) for row in rows]
        try:
            self.clear()
            if values:
                self._table = self._database.create_table(self.table_name, data=values)
        except Exception:
            self._table = None
            self.disable()
            raise
        self._enable()

    def search(self, embedding: Sequence[float], limit: int) -> list[dict[str, object]]:
        if self._disabled or limit <= 0:
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

    def _enable(self) -> None:
        if self._disabled_marker is not None:
            try:
                self._disabled_marker.unlink(missing_ok=True)
            except OSError:
                return
        self._disabled = False


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

        if self._vector_store is not None and not self._vectors_disabled():
            previous_chunk_ids = self._storage.list_fts_chunk_ids(source_key)
            new_chunk_ids = {chunk_id for chunk_id, _text, _title in fts_rows}
            new_fts_rows = [row for row in fts_rows if row[0] not in previous_chunk_ids]
            embeddings = self._ollama.embed([text for _chunk_id, text, _title in new_fts_rows]) if new_fts_rows else []
            rows = [
                {
                    "chunk_id": chunk_id,
                    "source_key": source_key,
                    "title": source.title,
                    "text_content": text,
                    "vector": embedding,
                }
                for (chunk_id, text, _title), embedding in zip(new_fts_rows, embeddings, strict=True)
            ]
            if rows:
                self._vector_store.add(rows)
            try:
                self._storage.replace_fts_chunks(source_key, fts_rows)
            except Exception:
                # Rows staged for a failed FTS transaction must not accumulate in LanceDB.
                self._discard_staged_vector_rows(rows)
                raise
            try:
                self._vector_store.delete_chunks(tuple(previous_chunk_ids - new_chunk_ids))
            except Exception:  # noqa: BLE001
                # FTS is authoritative once replaced, so stale vector cleanup can fail safely.
                return len(chunks)
        else:
            self._storage.replace_fts_chunks(source_key, fts_rows)
        return len(chunks)

    def retry_disabled_vector_recovery(self) -> bool:
        """Rebuild disabled vectors without allowing recovery trouble to fail a sync."""
        if self._vector_store is None or not self._vectors_disabled():
            return True
        try:
            self.rebuild_vectors_from_fts()
        except Exception:  # noqa: BLE001
            return False
        return True

    def rebuild_vectors_from_fts(self) -> None:
        """Recreate vectors from authoritative FTS rows after a sync rollback."""
        if self._vector_store is None:
            return
        self._vector_store.disable()
        try:
            self._vector_store.clear()
            chunks = self._storage.list_fts_chunks()
            embeddings = self._ollama.embed([chunk.text_content for chunk in chunks]) if chunks else []
            rows = [
                {
                    "chunk_id": chunk.chunk_id,
                    "source_key": chunk.source_key,
                    "title": chunk.title,
                    "text_content": chunk.text_content,
                    "vector": embedding,
                }
                for chunk, embedding in zip(chunks, embeddings, strict=True)
            ]
            self._vector_store.replace_all(rows)
        except Exception:
            self._vector_store.disable()
            raise

    def _vectors_disabled(self) -> bool:
        if self._vector_store is None:
            return False
        disabled = getattr(self._vector_store, "vectors_disabled", None)
        return bool(disabled()) if callable(disabled) else False

    def _discard_staged_vector_rows(self, rows: Sequence[Mapping[str, object]]) -> None:
        if self._vector_store is None:
            return
        try:
            self._vector_store.delete_chunks(tuple(str(row["chunk_id"]) for row in rows))
        except Exception:  # noqa: BLE001
            return


def _chunk_id(source_key: str, ordinal: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{source_key}:{ordinal}:{digest}"
