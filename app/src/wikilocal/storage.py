from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wikilocal.settings import Settings


@dataclass(frozen=True)
class SourceRecord:
    source_key: str
    source_type: str
    title: str
    text_content: str
    metadata: Mapping[str, Any]
    active: bool


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    text_content: str
    title: str
    source_key: str


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.database_path: Path = settings.database_path
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self._connection is None:
            # FastAPI executes synchronous endpoints in a worker thread while the
            # application can be initialized and shut down on another thread.
            self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row

        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                source_key TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                title TEXT NOT NULL,
                text_content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                active INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                source_updated_at TEXT,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_key TEXT PRIMARY KEY,
                cursor_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                text_content,
                title,
                source_key UNINDEXED
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def upsert_source(self, source: SourceRecord) -> None:
        metadata = dict(source.metadata)
        source_updated_at = metadata.get("source_updated_at")
        if source_updated_at is not None:
            source_updated_at = str(source_updated_at)

        connection = self._connection_or_raise()
        connection.execute(
            """
            INSERT INTO sources (
                source_key, source_type, title, text_content, metadata_json,
                active, content_hash, source_updated_at, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                source_type = excluded.source_type,
                title = excluded.title,
                text_content = excluded.text_content,
                metadata_json = excluded.metadata_json,
                active = excluded.active,
                content_hash = excluded.content_hash,
                source_updated_at = excluded.source_updated_at,
                synced_at = excluded.synced_at
            """,
            (
                source.source_key,
                source.source_type,
                source.title,
                source.text_content,
                _json_dumps(metadata),
                int(source.active),
                _content_hash(source),
                source_updated_at,
                _timestamp(),
            ),
        )
        connection.commit()

    def list_sources(self, *, active_only: bool = False) -> list[SourceRecord]:
        query = """
            SELECT source_key, source_type, title, text_content, metadata_json, active
            FROM sources
        """
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY source_key"

        rows = self._connection_or_raise().execute(query).fetchall()
        return [
            SourceRecord(
                source_key=row["source_key"],
                source_type=row["source_type"],
                title=row["title"],
                text_content=row["text_content"],
                metadata=json.loads(row["metadata_json"]),
                active=bool(row["active"]),
            )
            for row in rows
        ]

    def get_source(self, source_key: str) -> SourceRecord | None:
        row = self._connection_or_raise().execute(
            """
            SELECT source_key, source_type, title, text_content, metadata_json, active
            FROM sources WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()
        if row is None:
            return None
        return SourceRecord(
            source_key=row["source_key"],
            source_type=row["source_type"],
            title=row["title"],
            text_content=row["text_content"],
            metadata=json.loads(row["metadata_json"]),
            active=bool(row["active"]),
        )

    def replace_fts_chunks(
        self, source_key: str, chunks: Sequence[tuple[str, str, str]]
    ) -> None:
        """Replace every FTS chunk owned by a source in one transaction."""
        connection = self._connection_or_raise()
        try:
            connection.execute("BEGIN")
            connection.execute("DELETE FROM chunks_fts WHERE source_key = ?", (source_key,))
            connection.executemany(
                """
                INSERT INTO chunks_fts (chunk_id, text_content, title, source_key)
                VALUES (?, ?, ?, ?)
                """,
                [(chunk_id, text, title, source_key) for chunk_id, text, title in chunks],
            )
        except Exception:
            connection.rollback()
            raise
        connection.commit()

    def search_fts(self, query: str, *, limit: int = 20) -> list[ChunkRecord]:
        if not query.strip() or limit <= 0:
            return []
        rows = self._connection_or_raise().execute(
            """
            SELECT chunks_fts.chunk_id, chunks_fts.text_content, chunks_fts.title, chunks_fts.source_key
            FROM chunks_fts
            JOIN sources ON sources.source_key = chunks_fts.source_key
            WHERE chunks_fts MATCH ? AND sources.active = 1
            ORDER BY bm25(chunks_fts), chunk_id
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [
            ChunkRecord(
                chunk_id=row["chunk_id"],
                text_content=row["text_content"],
                title=row["title"],
                source_key=row["source_key"],
            )
            for row in rows
        ]

    def get_checkpoint(self, checkpoint_key: str) -> Any | None:
        row = self._connection_or_raise().execute(
            "SELECT cursor_json FROM checkpoints WHERE checkpoint_key = ?",
            (checkpoint_key,),
        ).fetchone()
        return None if row is None else json.loads(row["cursor_json"])

    def set_checkpoint(self, checkpoint_key: str, cursor: Any) -> None:
        connection = self._connection_or_raise()
        connection.execute(
            """
            INSERT INTO checkpoints (checkpoint_key, cursor_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(checkpoint_key) DO UPDATE SET
                cursor_json = excluded.cursor_json,
                updated_at = excluded.updated_at
            """,
            (checkpoint_key, _json_dumps(cursor), _timestamp()),
        )
        connection.commit()

    def finalize_document_scan(self, seen_source_keys: set[str], cursor: Any) -> None:
        """Atomically retire absent documents and advance the completed-scan cursor."""
        connection = self._connection_or_raise()
        try:
            connection.execute("BEGIN")
            if seen_source_keys:
                placeholders = ", ".join("?" for _ in seen_source_keys)
                connection.execute(
                    f"""
                    UPDATE sources
                    SET active = 0, synced_at = ?
                    WHERE source_type = 'document' AND active = 1
                    AND source_key NOT IN ({placeholders})
                    """,
                    (_timestamp(), *sorted(seen_source_keys)),
                )
            else:
                connection.execute(
                    """
                    UPDATE sources
                    SET active = 0, synced_at = ?
                    WHERE source_type = 'document' AND active = 1
                    """,
                    (_timestamp(),),
                )
            connection.execute(
                """
                INSERT INTO checkpoints (checkpoint_key, cursor_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    updated_at = excluded.updated_at
                """,
                ("documents", _json_dumps(cursor), _timestamp()),
            )
        except Exception:
            connection.rollback()
            raise
        connection.commit()

    def _connection_or_raise(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Storage.initialize() must be called before use.")
        return self._connection


def _content_hash(source: SourceRecord) -> str:
    content = "\0".join((source.source_type, source.title, source.text_content))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
