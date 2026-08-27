from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from wikilocal.settings import Settings
from wikilocal.storage import SourceRecord, Storage


@dataclass(frozen=True)
class SyncResult:
    created: int = 0
    changed: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, **counts: int) -> "SyncResult":
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

    def read_document(self, document: str) -> Any: ...


class DocumentSynchronizer:
    def __init__(self, settings: Settings, storage: Storage, feishu: DocumentReader) -> None:
        self._settings = settings
        self._storage = storage
        self._feishu = feishu

    def sync(self) -> SyncResult:
        existing = {source.source_key: source for source in self._storage.list_sources()}
        seen_keys: set[str] = set()
        result = SyncResult()
        successful_scan = True

        try:
            spaces = list(_pages(self._feishu.list_wiki_spaces))
        except Exception:
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
            except Exception:
                successful_scan = False
                result = result.add(failed=1)
                continue
            for node, wiki_path in nodes:
                token = _text(node.get("obj_token"))
                if not token or _text(node.get("obj_type")) != "docx":
                    continue
                source_key = f"document:{token}"
                seen_keys.add(source_key)
                try:
                    payload = self._feishu.read_document(token)
                    content = _document_content(payload)
                    title = _text(node.get("title")) or token
                    metadata = {
                        "url": _text(node.get("url")) or _document_url(token),
                        "wiki_path": wiki_path,
                        "source_updated_at": _text(node.get("obj_edit_time"))
                        or _text(node.get("update_time")),
                    }
                    source = SourceRecord(
                        source_key=source_key,
                        source_type="document",
                        title=title,
                        text_content=content,
                        metadata=metadata,
                        active=True,
                    )
                    outcome = _outcome(existing.get(source_key), source)
                    self._write_mirror(token, title, content)
                    self._storage.upsert_source(source)
                    existing[source_key] = source
                    result = result.add(**{outcome: 1})
                except Exception:
                    successful_scan = False
                    result = result.add(failed=1)

        if not successful_scan:
            return result

        self._storage.finalize_document_scan(seen_keys, {"completed_at": _now()})
        return result

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

    def _write_mirror(self, token: str, title: str, content: str) -> None:
        mirror = self._settings.root / "data" / "documents" / f"document-{token}.md"
        mirror.write_text(f"# {title}\n\n{content}", encoding="utf-8", newline="\n")


def _pages(fetch: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page = fetch(page_token=page_token)
        if not isinstance(page, dict):
            raise ValueError("Feishu pagination response must be an object.")
        values = page.get("items", page.get("nodes", []))
        if not isinstance(values, list):
            raise ValueError("Feishu pagination response must contain an item list.")
        items.extend(value for value in values if isinstance(value, dict))
        if not page.get("has_more"):
            return items
        next_token = _text(page.get("page_token"))
        if not next_token:
            raise ValueError("Feishu pagination response is missing its next page token.")
        page_token = next_token


def _is_container(node: dict[str, Any]) -> bool:
    return _text(node.get("obj_type")) in {"folder", "wiki"} or bool(node.get("has_child"))


def _document_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Feishu document response must be an object.")
    document = payload.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("content"), str):
        raise ValueError("Feishu document response is missing Markdown content.")
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


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _document_url(token: str) -> str:
    return f"https://feishu.cn/docx/{token}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
