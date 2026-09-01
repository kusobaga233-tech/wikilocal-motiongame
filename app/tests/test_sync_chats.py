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
        self.thread_pages_by_request: dict[tuple[str, str | None], dict[str, object]] = {}
        self.message_calls: list[tuple[str, str | None, str | None]] = []
        self.thread_calls: list[tuple[str, str | None]] = []
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
        self,
        thread_id: str,
        *,
        page_token: str | None = None,
    ) -> dict[str, object]:
        self.thread_calls.append((thread_id, page_token))
        if self.thread_pages_by_request:
            return self.thread_pages_by_request[(thread_id, page_token)]
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
            {
                "message_id": "m2",
                "sent_at": "2026-08-26T01:01:00Z",
                "thread_ids": [],
            },
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
                "thread_cursors": {
                    "omt-1": {"message_id": "m2", "sent_at": "2026-08-26T02:00:00Z"}
                },
            },
        )

    def test_revised_and_recalled_messages_refresh_their_persisted_state(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.pages = {
            None: {
                "messages": [text_message("m1", "2026-08-26T01:00:00Z", "Original")],
                "has_more": False,
            }
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        self.assertEqual(synchronizer.sync().created, 1)

        revised = text_message("m1", "2026-08-26T01:00:00Z", "Revised")
        revised["update_time"] = "2026-08-26T02:00:00Z"
        feishu.pages = {None: {"messages": [revised], "has_more": False}}

        revised_result = synchronizer.sync()

        self.assertEqual((revised_result.changed, revised_result.failed), (1, 0))
        revised_source = storage.get_source("message:m1")
        self.assertIsNotNone(revised_source)
        assert revised_source is not None
        self.assertTrue(revised_source.active)
        self.assertEqual(revised_source.text_content, "Revised\n")
        self.assertEqual(revised_source.metadata["revision"], "2026-08-26T02:00:00Z")
        self.assertFalse(revised_source.metadata["deleted"])

        recalled = text_message("m1", "2026-08-26T01:00:00Z", "")
        recalled["update_time"] = "2026-08-26T03:00:00Z"
        recalled["deleted"] = True
        feishu.pages = {None: {"messages": [recalled], "has_more": False}}

        recalled_result = synchronizer.sync()

        self.assertEqual((recalled_result.changed, recalled_result.failed), (1, 0))
        recalled_source = storage.get_source("message:m1")
        self.assertIsNotNone(recalled_source)
        assert recalled_source is not None
        self.assertFalse(recalled_source.active)
        self.assertEqual(recalled_source.text_content, "")
        self.assertEqual(recalled_source.metadata["revision"], "2026-08-26T03:00:00Z")
        self.assertTrue(recalled_source.metadata["deleted"])

    def test_old_message_revisions_are_reconciled_from_the_configured_history_start(self) -> None:
        settings, storage = self.make_storage()
        settings.chat_history_start = "2026-01-01"
        feishu = FakeChatFeishu()
        original = text_message("m1", "2026-01-02T01:00:00Z", "Original")
        newer = text_message("m2", "2026-02-01T01:00:00Z", "Newer")
        feishu.pages_by_request = {
            ("oc-1", None, "2026-01-01"): {
                "messages": [original, newer],
                "has_more": False,
            }
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        self.assertEqual(synchronizer.sync().created, 2)

        recalled = text_message("m1", "2026-01-02T01:00:00Z", "")
        recalled["update_time"] = "2026-03-01T01:00:00Z"
        recalled["recalled"] = True
        feishu.message_calls.clear()
        feishu.pages_by_request = {
            ("oc-1", None, "2026-01-01"): {
                "messages": [recalled, newer],
                "has_more": False,
            }
        }

        result = synchronizer.sync()

        self.assertEqual((result.changed, result.skipped, result.failed), (1, 1, 0))
        self.assertEqual(feishu.message_calls, [("oc-1", None, "2026-01-01")])
        source = storage.get_source("message:m1")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertFalse(source.active)
        self.assertTrue(source.metadata["deleted"])

    def test_known_thread_rescans_full_history_and_idempotently_upserts_replies(self) -> None:
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
        feishu.thread_pages_by_request = {
            ("omt-1", None): {
                "messages": [text_message("m2", "2026-08-26T01:01:00Z", "First reply")],
                "has_more": True,
                "page_token": "older",
            },
            ("omt-1", "older"): {
                "messages": [text_message("m3", "2026-08-26T01:02:00Z", "Second reply")],
                "has_more": False,
            },
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        first_result = synchronizer.sync()

        self.assertEqual((first_result.created, first_result.failed), (3, 0))
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
                "thread_cursors": {
                    "omt-1": {"message_id": "m3", "sent_at": "2026-08-26T01:02:00Z"}
                },
            },
        )

        feishu.pages = {None: {"messages": [], "has_more": False}}
        feishu.thread_calls.clear()
        feishu.thread_pages_by_request = {
            ("omt-1", None): {
                "messages": [text_message("m4", "2026-08-26T02:00:00Z", "New reply")],
                "has_more": False,
            }
        }

        second_result = synchronizer.sync()

        self.assertEqual(
            (second_result.created, second_result.changed, second_result.skipped, second_result.failed),
            (1, 0, 0, 0),
        )
        self.assertEqual(
            [source.source_key for source in storage.list_sources(active_only=True)],
            ["message:m1", "message:m2", "message:m3", "message:m4"],
        )
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
                "thread_cursors": {
                    "omt-1": {"message_id": "m4", "sent_at": "2026-08-26T02:00:00Z"}
                },
            },
        )
        self.assertEqual(feishu.message_calls[-1], ("oc-1", None, None))
        self.assertEqual(feishu.thread_calls, [("omt-1", None)])

    def test_thread_root_seeds_cursor_when_its_initial_reply_page_is_empty(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.pages = {
            None: {
                "messages": [text_message("m1", "2026-08-26T01:00:00Z", "Root", thread_id="omt-1")],
                "has_more": False,
            }
        }
        feishu.thread_pages_by_request = {
            ("omt-1", None): {"messages": [], "has_more": False}
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        synchronizer.sync()

        self.assertEqual(
            storage.get_checkpoint("chat:oc-1")["thread_cursors"],
            {"omt-1": {"message_id": "m1", "sent_at": "2026-08-26T01:00:00Z"}},
        )

        feishu.pages = {None: {"messages": [], "has_more": False}}
        feishu.thread_calls.clear()
        feishu.thread_pages_by_request = {
            ("omt-1", None): {"messages": [], "has_more": False}
        }

        result = synchronizer.sync()

        self.assertEqual((result.created, result.failed), (0, 0))
        self.assertEqual(feishu.thread_calls, [("omt-1", None)])

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
        }
        feishu.thread_pages = {
            ("omt-1", None): {
                "messages": [text_message("m2", "2026-08-26T02:00:00Z", "New reply")],
                "has_more": False,
            }
        }
        synchronizer = ChatSynchronizer(settings, storage, feishu)

        result = synchronizer.sync()

        self.assertEqual((result.created, result.failed), (2, 0))
        self.assertEqual(
            storage.get_checkpoint("chat:oc-1"),
            {
                "message_id": "m1",
                "sent_at": "2026-08-26T01:00:00Z",
                "thread_ids": ["omt-1"],
                "thread_cursors": {
                    "omt-1": {"message_id": "m2", "sent_at": "2026-08-26T02:00:00Z"}
                },
                "thread_discovery_complete": True,
            },
        )
        self.assertEqual(
            feishu.message_calls,
            [("oc-1", None, None), ("oc-1", None, None)],
        )

        feishu.message_calls.clear()
        feishu.thread_calls.clear()
        feishu.thread_pages = {("omt-1", None): {"messages": [], "has_more": False}}

        second_result = synchronizer.sync()

        self.assertEqual((second_result.created, second_result.failed), (0, 0))
        self.assertEqual(feishu.message_calls, [("oc-1", None, None)])
        self.assertEqual(feishu.thread_calls, [("omt-1", None)])

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
            self.assertEqual((chat_id, start, end), ("oc-1", None, None))
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
            ("oc-1", None, None): {"messages": [], "has_more": False},
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

    def test_chat_enumeration_accepts_a_null_final_chat_page(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeChatFeishu()
        feishu.chat_pages = {
            None: {
                "chats": [{"chat_id": "oc-1", "name": "Roadmap"}],
                "has_more": True,
                "page_token": "final",
            },
            "final": {"chats": None, "has_more": False, "page_token": ""},
        }
        feishu.pages = {None: {"messages": [], "has_more": False}}

        result = ChatSynchronizer(settings, storage, feishu).sync()

        self.assertEqual((result.created, result.failed), (0, 0))
        self.assertEqual(feishu.message_calls, [("oc-1", None, None)])
