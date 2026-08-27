from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.settings import Settings
from wikilocal.storage import Storage
from wikilocal.sync_chats import ChatSynchronizer


class FakeChatFeishu:
    def __init__(self) -> None:
        self.chats = [{"chat_id": "oc-1", "name": "Roadmap", "chat_mode": "group"}]
        self.pages: dict[str | None, dict[str, object]] = {}
        self.pages_by_chat: dict[tuple[str, str | None], dict[str, object]] = {}
        self.pages_by_request: dict[tuple[str, str | None, str | None], dict[str, object]] = {}
        self.message_errors_by_request: dict[tuple[str, str | None, str | None], Exception] = {}
        self.thread_pages: dict[tuple[str, str | None], dict[str, object]] = {}
        self.message_calls: list[tuple[str, str | None, str | None]] = []
        self.chat_pages: dict[str | None, dict[str, object]] = {}

    def list_chats(self, *, page_token: str | None = None) -> dict[str, object]:
        if self.chat_pages:
            return self.chat_pages[page_token]
        assert page_token is None
        return {"items": self.chats}

    def list_messages(
        self,
        chat_id: str,
        *,
        page_token: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, object]:
        assert end is None
        self.message_calls.append((chat_id, page_token, start))
        request = (chat_id, page_token, start)
        if request in self.message_errors_by_request:
            raise self.message_errors_by_request[request]
        if self.pages_by_request:
            return self.pages_by_request[request]
        if self.pages_by_chat:
            return self.pages_by_chat[(chat_id, page_token)]
        return self.pages[page_token]

    def list_thread_messages(
        self, thread_id: str, *, page_token: str | None = None
    ) -> dict[str, object]:
        return self.thread_pages[(thread_id, page_token)]


def text_message(
    message_id: str,
    sent_at: str,
    text: str,
    *,
    thread_id: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": message_id,
        "msg_type": "text",
        "create_time": sent_at,
        "content": {"text": text},
        "sender": {"name": "Ada"},
        "url": f"https://example.test/messages/{message_id}",
    }
    if thread_id is not None:
        message["thread_id"] = thread_id
    return message


class ChatSynchronizerTests(unittest.TestCase):
    def make_storage(self) -> tuple[Settings, Storage]:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        settings = Settings.load(Path(self.temporary_directory.name))
        storage = Storage(settings)
        storage.initialize()
        self.addCleanup(storage.close)
        return settings, storage

    def test_duplicate_pagination_boundary_creates_two_messages_and_advances_cursor(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.pages = {
            None: {
                "messages": [text_message("m1", "2026-08-26T01:00:00Z", "First")],
                "has_more": True,
                "page_token": "next",
            },
            "next": {
                "messages": [
                    text_message("m1", "2026-08-26T01:00:00Z", "First"),
                    text_message("m2", "2026-08-26T01:01:00Z", "Second"),
                ],
                "has_more": False,
            },
        }

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual((result.created, result.changed, result.skipped, result.failed), (2, 0, 1, 0))
        self.assertEqual(
            [source.source_key for source in storage.list_sources(active_only=True)],
            ["message:m1", "message:m2"],
        )
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {"message_id": "m2", "sent_at": "2026-08-26T01:01:00Z"},
        )
        self.assertEqual(feishu.message_calls[0], ("oc-1", None, None))

    def test_thread_replies_are_stored_and_non_text_messages_keep_unindexed_metadata(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.pages = {
            None: {
                "messages": [
                    text_message("m1", "2026-08-26T01:00:00Z", "Root", thread_id="omt-1"),
                    {
                        "message_id": "m3",
                        "msg_type": "image",
                        "create_time": "2026-08-26T01:02:00Z",
                        "content": {"image_key": "img-1"},
                        "sender": {"name": "Grace"},
                    },
                ],
                "has_more": False,
            }
        }
        feishu.thread_pages = {
            ("omt-1", None): {
                "messages": [text_message("m2", "2026-08-26T01:01:00Z", "Reply")],
                "has_more": False,
            }
        }

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual((result.created, result.failed), (3, 0))
        sources = {source.source_key: source for source in storage.list_sources(active_only=True)}
        self.assertEqual(sources["message:m2"].metadata["thread_id"], "omt-1")
        self.assertEqual(sources["message:m2"].metadata["chat_name"], "Roadmap")
        self.assertEqual(sources["message:m3"].source_type, "message_metadata")
        self.assertEqual(sources["message:m3"].text_content, "")
        self.assertEqual(sources["message:m3"].metadata["message_type"], "image")
        self.assertEqual(sources["message:m3"].metadata["raw_content"], {"image_key": "img-1"})

    def test_thread_reply_does_not_advance_main_chat_message_cursor(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.pages = {
            None: {
                "messages": [text_message("m1", "2026-08-26T01:00:00Z", "Root", thread_id="omt-1")],
                "has_more": False,
            }
        }
        feishu.thread_pages = {
            ("omt-1", None): {
                "messages": [text_message("m2", "2026-08-26T02:00:00Z", "Later reply")],
                "has_more": False,
            }
        }

        ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
            },
        )

    def test_second_sync_reads_new_reply_from_known_thread_without_new_root_message(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.pages = {
            None: {
                "messages": [
                    text_message("m1", "2026-08-26T01:00:00Z", "Root", thread_id="omt-1")
                ],
                "has_more": False,
            }
        }
        feishu.thread_pages = {
            ("omt-1", None): {"messages": [], "has_more": False},
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        first_result = synchronizer.sync()

        self.assertEqual((first_result.created, first_result.failed), (1, 0))
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
            },
        )

        feishu.pages = {None: {"messages": [], "has_more": False}}
        feishu.thread_pages = {
            ("omt-1", None): {
                "messages": [
                    text_message("m1", "2026-08-26T01:00:00Z", "Root"),
                    text_message("m2", "2026-08-26T02:00:00Z", "New reply"),
                ],
                "has_more": False,
            },
        }

        second_result = synchronizer.sync()

        self.assertEqual(
            (second_result.created, second_result.changed, second_result.skipped, second_result.failed),
            (1, 0, 1, 0),
        )
        self.assertEqual(
            [source.source_key for source in storage.list_sources(active_only=True)],
            ["message:m1", "message:m2"],
        )
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
            },
        )
        self.assertEqual(feishu.message_calls[-1], ("oc-1", None, "2026-08-26T01:00:00Z"))

    def test_legacy_checkpoint_discovers_old_thread_and_stores_new_reply_once(self) -> None:
        settings, storage = self.make_storage()
        storage.set_checkpoint(
            "chat:oc-1",
            {"message_id": "m1", "sent_at": "2026-08-26T01:00:00Z"},
        )
        feishu = FakeChatFeishu()
        feishu.pages_by_request = {
            ("oc-1", None, None): {
                "messages": [text_message("m1", "2026-08-26T01:00:00Z", "Old root", thread_id="omt-1")],
                "has_more": False,
            },
            ("oc-1", None, "2026-08-26T01:00:00Z"): {
                "messages": [],
                "has_more": False,
            },
        }
        feishu.thread_pages = {
            ("omt-1", None): {
                "messages": [text_message("m2", "2026-08-26T02:00:00Z", "New reply")],
                "has_more": False,
            }
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        result = synchronizer.sync()

        self.assertEqual((result.created, result.failed), (1, 0))
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
                "thread_discovery_complete": True,
            },
        )
        self.assertEqual(
            feishu.message_calls,
            [("oc-1", None, None), ("oc-1", None, "2026-08-26T01:00:00Z")],
        )

        feishu.message_calls.clear()
        feishu.thread_pages = {("omt-1", None): {"messages": [], "has_more": False}}

        second_result = synchronizer.sync()

        self.assertEqual((second_result.created, second_result.failed), (0, 0))
        self.assertEqual(feishu.message_calls, [("oc-1", None, "2026-08-26T01:00:00Z")])

    def test_legacy_discovery_failure_keeps_checkpoint_unmigrated(self) -> None:
        settings, storage = self.make_storage()
        legacy_checkpoint = {"message_id": "m1", "sent_at": "2026-08-26T01:00:00Z"}
        storage.set_checkpoint("chat:oc-1", legacy_checkpoint)
        feishu = FakeChatFeishu()
        feishu.message_errors_by_request[("oc-1", None, None)] = RuntimeError("network failure")

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual((result.created, result.failed), (0, 1))
        self.assertEqual(storage.get_checkpoint("chat:oc-1"), legacy_checkpoint)
        self.assertEqual(feishu.message_calls, [("oc-1", None, None)])

    def test_sync_includes_p2p_and_muted_chats_returned_by_read_only_enumeration(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.chats = [
            {"chat_id": "oc-1", "name": "Muted group", "chat_mode": "group", "is_muted": True},
            {"chat_id": "oc-2", "name": "Direct chat", "chat_mode": "p2p"},
        ]
        feishu.pages_by_chat = {
            ("oc-1", None): {
                "messages": [text_message("m1", "2026-08-26T01:00:00Z", "Group")],
                "has_more": False,
            },
            ("oc-2", None): {
                "messages": [text_message("m2", "2026-08-26T01:01:00Z", "Direct")],
                "has_more": False,
            },
        }

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result.created, 2)
        self.assertEqual(
            {source.metadata["chat_name"] for source in storage.list_sources(active_only=True)},
            {"Muted group", "Direct chat"},
        )

    def test_repeated_chat_page_token_fails_without_changing_existing_checkpoint(self) -> None:
        settings, storage = self.make_storage()
        checkpoint = {
            "message_id": "m1",
            "sent_at": "2026-08-26T01:00:00Z",
            "thread_ids": [],
        }
        storage.set_checkpoint("chat:oc-1", checkpoint)
        feishu = FakeChatFeishu()
        calls = 0

        def list_messages(
            chat_id: str, *, page_token: str | None = None, start: str | None = None, end: str | None = None
        ) -> dict[str, object]:
            nonlocal calls
            self.assertEqual((chat_id, start, end), ("oc-1", "2026-08-26T01:00:00Z", None))
            calls += 1
            return (
                {"messages": [], "has_more": True, "page_token": "loop"}
                if calls < 3
                else {"messages": [], "has_more": False}
            )

        feishu.list_messages = list_messages  # type: ignore[method-assign]

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("chat:oc-1"), checkpoint)

    def test_repeated_legacy_discovery_page_token_fails_without_migrating_checkpoint(self) -> None:
        settings, storage = self.make_storage()
        checkpoint = {"message_id": "m1", "sent_at": "2026-08-26T01:00:00Z"}
        storage.set_checkpoint("chat:oc-1", checkpoint)
        feishu = FakeChatFeishu()
        calls = 0

        def list_messages(
            chat_id: str, *, page_token: str | None = None, start: str | None = None, end: str | None = None
        ) -> dict[str, object]:
            nonlocal calls
            self.assertEqual((chat_id, end), ("oc-1", None))
            if start is not None:
                self.assertEqual(start, "2026-08-26T01:00:00Z")
                return {"messages": [], "has_more": False}
            calls += 1
            return {"messages": [], "has_more": True, "page_token": "loop"} if calls < 3 else {"messages": [], "has_more": False}

        feishu.list_messages = list_messages  # type: ignore[method-assign]

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("chat:oc-1"), checkpoint)

    def test_repeated_thread_reply_page_token_fails_without_changing_checkpoint(self) -> None:
        settings, storage = self.make_storage()
        checkpoint = {
            "message_id": "m1",
            "sent_at": "2026-08-26T01:00:00Z",
            "thread_ids": ["omt-1"],
        }
        storage.set_checkpoint("chat:oc-1", checkpoint)
        feishu = FakeChatFeishu()
        feishu.pages_by_request = {
            ("oc-1", None, "2026-08-26T01:00:00Z"): {"messages": [], "has_more": False},
        }
        calls = 0

        def list_thread_messages(thread_id: str, *, page_token: str | None = None) -> dict[str, object]:
            nonlocal calls
            self.assertEqual(thread_id, "omt-1")
            calls += 1
            return (
                {"messages": [], "has_more": True, "page_token": "loop"}
                if calls < 3
                else {"messages": [], "has_more": False}
            )

        feishu.list_thread_messages = list_thread_messages  # type: ignore[method-assign]

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("chat:oc-1"), checkpoint)

    def test_repeated_chat_enumeration_page_token_fails_without_changing_checkpoint(self) -> None:
        settings, storage = self.make_storage()
        checkpoint = {"message_id": "m1", "sent_at": "2026-08-26T01:00:00Z"}
        storage.set_checkpoint("chat:oc-1", checkpoint)
        feishu = FakeChatFeishu()
        calls = 0

        def list_chats(*, page_token: str | None = None) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return (
                {"items": [], "has_more": True, "page_token": "loop"}
                if calls < 3
                else {"items": [], "has_more": False}
            )

        feishu.list_chats = list_chats  # type: ignore[method-assign]

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("chat:oc-1"), checkpoint)
