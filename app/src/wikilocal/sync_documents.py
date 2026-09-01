from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from wikilocal.settings import Settings
from wikilocal.storage import SourceRecord, Storage


@dataclass(frozen=True)
class SyncResult:
    created: int = 0
    changed: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, **counts: int) -> SyncResult:
        return SyncResult(
            created=self.created + counts.get("created", 0),
            changed=self.changed + counts.get("changed", 0),
            skipped=self.skipped + counts.get("skipped", 0),
            failed=self.failed + counts.get("failed", 0),
        )


class DocumentReader(Protocol):
    def list_wiki_spaces(self, *, page_token: str | None = None) -> Any: ...

    def list_wiki_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_token: str | None = None,
    ) -> Any: ...

    def list_personal_documents(
        self, *, page_token: str | None = None, folder_token: str | None = None
    ) -> Any: ...

    def read_document(self, document: str) -> Any: ...


class DocumentSynchronizer:
    def __init__(self, settings: Settings, storage: Storage, feishu: DocumentReader) -> None:
        self._settings = settings
        self._storage = storage
        self._feishu = feishu

    def sync(self) -> SyncResult:
        existing = {source.source_key: source for source in self._storage.list_sources()}
        seen_keys: set[str] = set()
        revisions: dict[str, str] = {}
        completed_revisions = _checkpoint_revisions(self._storage.get_checkpoint("documents"))
        result = SyncResult()
        successful_scan = True

        try:
            spaces = list(_pages(self._feishu.list_wiki_spaces))
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return result.add(failed=1)

        for space in spaces:
            space_id = _text(space.get("space_id"))
            if not space_id:
                successful_scan = False
                result = result.add(failed=1)
                continue
            space_name = _text(space.get("name")) or space_id
            try:
                nodes = self._walk_nodes(space_id, space_name)
            except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                successful_scan = False
                result = result.add(failed=1)
                continue
            for node, wiki_path in nodes:
                token = _text(node.get("obj_token"))
                if not token or _text(node.get("obj_type")) != "docx":
                    continue
                metadata = {
                    "url": _text(node.get("url")) or _document_url(token),
                    "wiki_path": wiki_path,
                    "source_scope": "wiki",
                    "source_revision": _record_revision(node),
                    "source_updated_at": _source_updated_at(node),
                }
                result, successful_scan = self._sync_document(
                    token,
                    _text(node.get("title")) or token,
                    metadata,
                    existing,
                    completed_revisions,
                    seen_keys,
                    revisions,
                    result,
                    successful_scan,
                )

        personal_document_list = getattr(self._feishu, "list_personal_documents", None)
        if callable(personal_document_list):
            try:
                personal_documents = _personal_documents(personal_document_list)
            except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                return result.add(failed=1)
            for document in personal_documents:
                token = _text(document.get("token"))
                if not token or _text(document.get("type")) != "docx":
                    continue
                metadata = {
                    "url": _text(document.get("url")) or _document_url(token),
                    "source_scope": "personal_library",
                    "source_revision": _record_revision(document),
                    "source_updated_at": _source_updated_at(document),
                }
                result, successful_scan = self._sync_document(
                    token,
                    _text(document.get("name")) or token,
                    metadata,
                    existing,
                    completed_revisions,
                    seen_keys,
                    revisions,
                    result,
                    successful_scan,
                )
        elif any(
            source.metadata.get("source_scope") == "personal_library"
            for source in existing.values()
        ):
            successful_scan = False
            result = result.add(failed=1)

        if not successful_scan:
            return result

        self._storage.finalize_document_scan(
            seen_keys, {"completed_at": _now(), "revisions": revisions}
        )
        return result

    def _sync_document(
        self,
        token: str,
        title: str,
        metadata: dict[str, str],
        existing: dict[str, SourceRecord],
        completed_revisions: dict[str, str],
        seen_keys: set[str],
        revisions: dict[str, str],
        result: SyncResult,
        successful_scan: bool,
    ) -> tuple[SyncResult, bool]:
        source_key = f"document:{token}"
        if source_key in seen_keys:
            return result, successful_scan
        seen_keys.add(source_key)
        revision = _source_revision(metadata)
        if revision:
            revisions[source_key] = revision
        previous = existing.get(source_key)
        if _can_skip_document(previous, source_key, revision, completed_revisions):
            return result.add(skipped=1), successful_scan
        try:
            content = _document_content(self._feishu.read_document(token))
            source = SourceRecord(
                source_key=source_key,
                source_type="document",
                title=title,
                text_content=content,
                metadata=metadata,
                active=True,
            )
            outcome = _outcome(previous, source)
            self._write_mirror(token, source)
            self._storage.upsert_source(source)
            existing[source_key] = source
            return result.add(**{outcome: 1}), successful_scan
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return result.add(failed=1), False

    def _walk_nodes(self, space_id: str, space_name: str) -> list[tuple[dict[str, Any], str]]:
        result: list[tuple[dict[str, Any], str]] = []

        def visit(parent_node_token: str | None, path: tuple[str, ...]) -> None:
            for node in _pages(
                lambda page_token: self._feishu.list_wiki_nodes(
                    space_id, parent_node_token=parent_node_token, page_token=page_token
                )
            ):
                title = _text(node.get("title")) or _text(node.get("obj_token"))
                node_path = " / ".join((*path, title))
                if _text(node.get("obj_type")) == "docx":
                    result.append((node, node_path))
                node_token = _text(node.get("node_token"))
                if node_token and _is_container(node):
                    visit(node_token, (*path, title))

        visit(None, (space_name,))
        return result

    def _write_mirror(self, token: str, source: SourceRecord) -> None:
        mirror = self._settings.root / "data" / "documents" / f"document-{_token_digest(token)}.md"
        metadata = source.metadata
        provenance = {
            "source_key": source.source_key,
            "document_token": token,
            "url": metadata.get("url"),
            "wiki_path": metadata.get("wiki_path"),
            "source_updated_at": metadata.get("source_updated_at"),
            "content_hash": _content_hash(source.text_content),
        }
        front_matter = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in provenance.items()
        )
        mirror.write_text(
            f"---\n{front_matter}\n---\n\n# {source.title}\n\n{source.text_content}",
            encoding="utf-8",
            newline="\n",
        )


def _pages(fetch: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()
    while True:
        page = fetch(page_token=page_token)
        if not isinstance(page, dict):
            raise TypeError("Feishu pagination response must be an object.")
        values = page.get("items", page.get("nodes", []))
        if not isinstance(values, list):
            raise TypeError("Feishu pagination response must contain an item list.")
        items.extend(value for value in values if isinstance(value, dict))
        if not page.get("has_more"):
            return items
        next_token = _text(page.get("page_token"))
        if not next_token:
            raise ValueError("Feishu pagination response is missing its next page token.")
        if next_token in seen_page_tokens:
            raise ValueError("Feishu pagination response repeated a page token.")
        seen_page_tokens.add(next_token)
        page_token = next_token


def _personal_documents(fetch: Any) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    visited_folders: set[str] = set()

    def visit(folder_token: str | None) -> None:
        if folder_token is not None:
            if folder_token in visited_folders:
                return
            visited_folders.add(folder_token)
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            if folder_token is None:
                payload = fetch(page_token=page_token)
            else:
                payload = fetch(page_token=page_token, folder_token=folder_token)
            if not isinstance(payload, dict):
                raise TypeError("Feishu personal library response must be an object.")
            values = payload.get("files")
            if not isinstance(values, list):
                raise TypeError("Feishu personal library response must contain a file list.")
            for document in values:
                if not isinstance(document, dict):
                    continue
                if _text(document.get("type")) == "folder":
                    child_folder_token = _text(document.get("token"))
                    if not child_folder_token:
                        raise ValueError("Feishu folder is missing its token.")
                    visit(child_folder_token)
                else:
                    documents.append(document)
            if not payload.get("has_more"):
                return
            next_token = _text(payload.get("next_page_token")) or _text(payload.get("page_token"))
            if not next_token:
                raise ValueError("Feishu personal library response is missing its next page token.")
            if next_token in seen_page_tokens:
                raise ValueError("Feishu personal library response repeated a page token.")
            seen_page_tokens.add(next_token)
            page_token = next_token

    visit(None)
    return documents


def _is_container(node: dict[str, Any]) -> bool:
    return _text(node.get("obj_type")) in {"folder", "wiki"} or bool(node.get("has_child"))


def _document_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TypeError("Feishu document response must be an object.")
    document = payload.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("content"), str):
        raise TypeError("Feishu document response is missing Markdown content.")
    return _normalize_text(document["content"])


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.rstrip("\n") + "\n" if value else ""


def _outcome(previous: SourceRecord | None, source: SourceRecord) -> str:
    if previous is None:
        return "created"
    if (
        previous.title == source.title
        and previous.text_content == source.text_content
        and dict(previous.metadata) == dict(source.metadata)
        and previous.active == source.active
    ):
        return "skipped"
    return "changed"


def _source_updated_at(record: dict[str, Any]) -> str:
    return (
        _text(record.get("obj_edit_time"))
        or _text(record.get("update_time"))
        or _text(record.get("modified_time"))
    )


def _record_revision(record: dict[str, Any]) -> str:
    return _text(record.get("revision_id")) or _text(record.get("revision"))


def _source_revision(metadata: dict[str, str]) -> str:
    values = [
        value
        for value in (metadata["source_revision"], metadata["source_updated_at"])
        if value
    ]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":")) if values else ""


def _can_skip_document(
    previous: SourceRecord | None,
    source_key: str,
    revision: str,
    completed_revisions: dict[str, str],
) -> bool:
    return bool(previous and previous.active and revision and completed_revisions.get(source_key) == revision)


def _checkpoint_revisions(checkpoint: Any) -> dict[str, str]:
    if not isinstance(checkpoint, dict):
        return {}
    revisions = checkpoint.get("revisions")
    if not isinstance(revisions, dict):
        return {}
    return {
        source_key: revision
        for source_key, revision in revisions.items()
        if isinstance(source_key, str) and isinstance(revision, str) and revision
    }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _document_url(token: str) -> str:
    return f"https://feishu.cn/docx/{token}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
