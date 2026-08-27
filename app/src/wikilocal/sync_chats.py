from __future__ import annotations

import json
from typing import Any, Protocol

from wikilocal.settings import Settings
from wikilocal.storage import SourceRecord, Storage
from wikilocal.sync_documents import SyncResult, _normalize_text, _outcome, _text


class ChatReader(Protocol):
    def list_chats(self, *, page_token: str | None = None) -> Any: ...

    def list_messages(
        self,
        chat_id: str,
        *,
        page_token: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> Any: ...

    def list_thread_messages(self, thread_id: str, *, page_token: str | None = None) -> Any: ...


class ChatSynchronizer:
    def __init__(self, settings: Settings, storage: Storage, feishu: ChatReader) -> None:
        self._settings = settings
        self._storage = storage
        self._feishu = feishu

    def sync(self) -> SyncResult:
        result = SyncResult()
        try:
            chats = list(_chat_pages(self._feishu.list_chats))
        except Exception:
            return result.add(failed=1)
        for chat in chats:
            chat_id = _text(chat.get("chat_id"))
            if not chat_id:
                result = result.add(failed=1)
                continue
            try:
                result = _add_results(result, self._sync_chat(chat_id, _chat_name(chat)))
            except Exception:
                result = result.add(failed=1)
        return result

    def _sync_chat(self, chat_id: str, chat_name: str) -> SyncResult:
        existing = {source.source_key: source for source in self._storage.list_sources()}
        cursor = self._storage.get_checkpoint(f"chat:{chat_id}")
        start = _cursor_time(cursor) or self._settings.chat_history_start
        thread_ids = _cursor_thread_ids(cursor)
        result = SyncResult()
        latest: tuple[str, str] | None = None
        page_token: str | None = None

        while True:
            page = self._feishu.list_messages(chat_id, page_token=page_token, start=start)
            messages, has_more, next_token = _message_page(page)
            for message in messages:
                outcome, candidate = self._store_message(
                    message, chat_id=chat_id, chat_name=chat_name, existing=existing, thread_id=None
                )
                result = result.add(**{outcome: 1})
                latest = _newer(latest, candidate)
                thread_id = _text(message.get("thread_id"))
                if thread_id:
                    thread_ids.add(thread_id)
            if not has_more:
                break
            if not next_token:
                raise ValueError("Feishu message page is missing its next page token.")
            page_token = next_token

        for thread_id in sorted(thread_ids):
            thread_result, _ = self._sync_thread(thread_id, chat_id, chat_name, existing)
            result = _add_results(result, thread_result)

        checkpoint = _chat_checkpoint(cursor, latest, thread_ids)
        if checkpoint is not None:
            self._storage.set_checkpoint(f"chat:{chat_id}", checkpoint)
        return result

    def _sync_thread(
        self,
        thread_id: str,
        chat_id: str,
        chat_name: str,
        existing: dict[str, SourceRecord],
    ) -> tuple[SyncResult, tuple[str, str] | None]:
        result = SyncResult()
        latest: tuple[str, str] | None = None
        page_token: str | None = None
        while True:
            page = self._feishu.list_thread_messages(thread_id, page_token=page_token)
            messages, has_more, next_token = _message_page(page)
            for message in messages:
                outcome, candidate = self._store_message(
                    message,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    existing=existing,
                    thread_id=thread_id,
                )
                result = result.add(**{outcome: 1})
                latest = _newer(latest, candidate)
            if not has_more:
                return result, latest
            if not next_token:
                raise ValueError("Feishu thread page is missing its next page token.")
            page_token = next_token

    def _store_message(
        self,
        message: dict[str, Any],
        *,
        chat_id: str,
        chat_name: str,
        existing: dict[str, SourceRecord],
        thread_id: str | None,
    ) -> tuple[str, tuple[str, str] | None]:
        message_id = _text(message.get("message_id"))
        if not message_id:
            raise ValueError("Feishu message is missing its message_id.")
        sent_at = _message_time(message)
        actual_thread_id = thread_id or _text(message.get("thread_id")) or None
        message_type = _text(message.get("msg_type")) or "unknown"
        text = _message_text(message) if message_type == "text" else ""
        metadata: dict[str, Any] = {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sender": _sender(message.get("sender")),
            "sent_at": sent_at,
            "thread_id": actual_thread_id,
            "url": _text(message.get("url")) or f"https://feishu.cn/im/message/{message_id}",
            "message_type": message_type,
        }
        if message_type != "text":
            metadata["raw_content"] = message.get("content")
        source = SourceRecord(
            source_key=f"message:{message_id}",
            source_type="message" if message_type == "text" else "message_metadata",
            title=f"{chat_name} · {_sender(message.get('sender'))}",
            text_content=text,
            metadata=metadata,
            active=True,
        )
        outcome = _outcome(existing.get(source.source_key), source)
        self._storage.upsert_source(source)
        existing[source.source_key] = source
        return outcome, (sent_at, message_id) if sent_at else None


def _chat_pages(fetch: Any) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page = fetch(page_token=page_token)
        if not isinstance(page, dict):
            raise ValueError("Feishu chat page must be an object.")
        values = page.get("items", page.get("chats", []))
        if not isinstance(values, list):
            raise ValueError("Feishu chat page must contain chats.")
        chats.extend(value for value in values if isinstance(value, dict))
        if not page.get("has_more"):
            return chats
        page_token = _text(page.get("page_token"))
        if not page_token:
            raise ValueError("Feishu chat page is missing its next page token.")


def _message_page(page: Any) -> tuple[list[dict[str, Any]], bool, str | None]:
    if not isinstance(page, dict):
        raise ValueError("Feishu message page must be an object.")
    values = page.get("messages", page.get("items", []))
    if not isinstance(values, list):
        raise ValueError("Feishu message page must contain messages.")
    return (
        [value for value in values if isinstance(value, dict)],
        bool(page.get("has_more")),
        _text(page.get("page_token")) or None,
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, dict):
        text = content.get("text")
    elif isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            text = content
        else:
            text = parsed.get("text") if isinstance(parsed, dict) else ""
    else:
        text = ""
    return _normalize_text(text) if isinstance(text, str) else ""


def _message_time(message: dict[str, Any]) -> str:
    return _text(message.get("create_time")) or _text(message.get("sent_at"))


def _sender(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text(value.get("name")) or _text(value.get("id")) or _text(value.get("sender_id"))
    return ""


def _chat_name(chat: dict[str, Any]) -> str:
    return _text(chat.get("name")) or _text(chat.get("chat_id"))


def _cursor_time(cursor: Any) -> str | None:
    return _text(cursor.get("sent_at")) or None if isinstance(cursor, dict) else None


def _cursor_thread_ids(cursor: Any) -> set[str]:
    if not isinstance(cursor, dict):
        return set()
    values = cursor.get("thread_ids")
    if not isinstance(values, list):
        return set()
    return {_text(value) for value in values if _text(value)}


def _chat_checkpoint(
    cursor: Any, latest: tuple[str, str] | None, thread_ids: set[str]
) -> dict[str, Any] | None:
    previous = cursor if isinstance(cursor, dict) else {}
    sent_at = latest[0] if latest is not None else _text(previous.get("sent_at"))
    message_id = latest[1] if latest is not None else _text(previous.get("message_id"))
    if not sent_at and not message_id and not thread_ids:
        return None

    checkpoint: dict[str, Any] = {}
    if sent_at:
        checkpoint["sent_at"] = sent_at
    if message_id:
        checkpoint["message_id"] = message_id
    if thread_ids:
        checkpoint["thread_ids"] = sorted(thread_ids)
    return checkpoint


def _newer(
    current: tuple[str, str] | None, candidate: tuple[str, str] | None
) -> tuple[str, str] | None:
    if candidate is None or (current is not None and candidate <= current):
        return current
    return candidate


def _add_results(left: SyncResult, right: SyncResult) -> SyncResult:
    return SyncResult(
        created=left.created + right.created,
        changed=left.changed + right.changed,
        skipped=left.skipped + right.skipped,
        failed=left.failed + right.failed,
    )
