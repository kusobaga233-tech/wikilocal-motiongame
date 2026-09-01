from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.settings import Settings
from wikilocal.storage import Storage
from wikilocal.sync_documents import DocumentSynchronizer, SyncResult


class FakeDocumentFeishu:
    def __init__(self) -> None:
        self.nodes = [
            {
                "node_token": "folder-1",
                "obj_token": "folder-1",
                "obj_type": "folder",
                "title": "Engineering",
                "url": "https://example.test/wiki/folder-1",
            },
            {
                "node_token": "node-d1",
                "obj_token": "d1",
                "obj_type": "docx",
                "title": "Release plan",
                "url": "https://example.test/wiki/node-d1",
                "obj_edit_time": "2026-08-26T02:00:00Z",
            },
        ]
        self.documents = {
            "d1": {
                "document": {
                    "document_id": "d1",
                    "content": "First line\r\nSecond line\r\n",
                }
            }
        }
        self.fail_documents: set[str] = set()
        self.fail_personal_document_listing = False
        self.personal_documents: list[dict[str, object]] = []
        self.personal_pages: dict[str | None, dict[str, object]] = {}
        self.personal_calls: list[str | None] = []
        self.personal_folder_pages: dict[tuple[str, str | None], dict[str, object]] = {}
        self.personal_folder_calls: list[tuple[str, str | None]] = []
        self.read_document_calls: list[str] = []

    def list_wiki_spaces(self, *, page_token: str | None = None) -> dict[str, object]:
        assert page_token is None
        return {"items": [{"space_id": "space-1", "name": "Product"}]}

    def list_wiki_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, object]:
        assert space_id == "space-1"
        assert page_token is None
        if parent_node_token is None:
            return {"items": [self.nodes[0]]}
        if parent_node_token == "folder-1":
            return {"items": self.nodes[1:]}
        return {"items": []}

    def list_personal_documents(
        self, *, page_token: str | None = None, folder_token: str | None = None
    ) -> dict[str, object]:
        if folder_token is not None:
            self.personal_folder_calls.append((folder_token, page_token))
            return self.personal_folder_pages[(folder_token, page_token)]
        self.personal_calls.append(page_token)
        if self.fail_personal_document_listing:
            raise RuntimeError("personal library listing failed")
        if self.personal_pages:
            return self.personal_pages[page_token]
        return {"files": self.personal_documents}

    def read_document(self, document: str) -> dict[str, object]:
        self.read_document_calls.append(document)
        if document in self.fail_documents:
            raise RuntimeError("fetch failed")
        return self.documents[document]


class DocumentSynchronizerTests(unittest.TestCase):
    def make_storage(self) -> tuple[Settings, Storage]:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        settings = Settings.load(Path(self.temporary_directory.name))
        storage = Storage(settings)
        storage.initialize()
        self.addCleanup(storage.close)
        return settings, storage

    def test_sync_writes_normalized_markdown_mirror_and_document_metadata(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()

        result = DocumentSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result.created, 1)
        self.assertEqual(result.changed, 0)
        self.assertEqual(result.failed, 0)
        content_hash = hashlib.sha256(b"First line\nSecond line\n").hexdigest()
        self.assertEqual(
            self.mirror_path(settings, "d1").read_text(encoding="utf-8"),
            "---\n"
            "source_key: \"document:d1\"\n"
            "document_token: \"d1\"\n"
            "url: \"https://example.test/wiki/node-d1\"\n"
            "wiki_path: \"Product / Engineering / Release plan\"\n"
            "source_updated_at: \"2026-08-26T02:00:00Z\"\n"
            f"content_hash: \"{content_hash}\"\n"
            "---\n\n"
            "# Release plan\n\nFirst line\nSecond line\n",
        )
        source = storage.list_sources(active_only=True)[0]
        self.assertEqual(source.source_key, "document:d1")
        self.assertEqual(source.text_content, "First line\nSecond line\n")
        self.assertEqual(source.metadata["url"], "https://example.test/wiki/node-d1")
        self.assertEqual(source.metadata["wiki_path"], "Product / Engineering / Release plan")
        self.assertEqual(source.metadata["source_updated_at"], "2026-08-26T02:00:00Z")
        self.assertIsNotNone(storage.get_checkpoint("documents"))

    def test_initial_sync_imports_document_from_personal_library(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.personal_documents.append(
            {
                "token": "personal-1",
                "type": "docx",
                "name": "Personal notes",
                "url": "https://example.test/docx/personal-1",
                "modified_time": "2026-08-27T02:00:00Z",
            }
        )
        feishu.documents["personal-1"] = {
            "document": {"document_id": "personal-1", "content": "Private planning"}
        }

        result = DocumentSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result, SyncResult(created=2))
        source = storage.get_source("document:personal-1")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.title, "Personal notes")
        self.assertEqual(source.text_content, "Private planning\n")
        self.assertEqual(source.metadata["url"], "https://example.test/docx/personal-1")
        self.assertEqual(source.metadata["source_updated_at"], "2026-08-27T02:00:00Z")

    def test_sync_follows_personal_drive_pages_before_finalizing_the_scan(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.personal_pages = {
            None: {
                "files": [
                    {
                        "token": "personal-1",
                        "type": "docx",
                        "name": "First page",
                        "modified_time": "2026-08-27T02:00:00Z",
                    }
                ],
                "has_more": True,
                "page_token": "drive-page-2",
            },
            "drive-page-2": {
                "files": [
                    {
                        "token": "personal-2",
                        "type": "docx",
                        "name": "Second page",
                        "modified_time": "2026-08-27T03:00:00Z",
                    }
                ],
                "has_more": False,
            },
        }
        feishu.documents.update(
            {
                "personal-1": {"document": {"document_id": "personal-1", "content": "One"}},
                "personal-2": {"document": {"document_id": "personal-2", "content": "Two"}},
            }
        )

        result = DocumentSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result, SyncResult(created=3))
        self.assertEqual(feishu.personal_calls, [None, "drive-page-2"])
        self.assertEqual(
            {source.source_key for source in storage.list_sources(active_only=True)},
            {"document:d1", "document:personal-1", "document:personal-2"},
        )

    def test_sync_recursively_follows_drive_folders_and_next_page_token(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.personal_documents = [
            {"token": "folder-1", "type": "folder", "name": "Projects"},
            {"token": "personal-1", "type": "docx", "name": "Root document"},
        ]
        feishu.personal_folder_pages = {
            ("folder-1", None): {
                "files": [{"token": "folder-2", "type": "folder", "name": "Nested"}],
                "has_more": True,
                "next_page_token": "folder-1-page-2",
            },
            ("folder-1", "folder-1-page-2"): {
                "files": [{"token": "personal-2", "type": "docx", "name": "Nested document"}],
                "has_more": False,
            },
            ("folder-2", None): {"files": [], "has_more": False},
        }
        feishu.documents.update(
            {
                "personal-1": {"document": {"document_id": "personal-1", "content": "Root"}},
                "personal-2": {"document": {"document_id": "personal-2", "content": "Nested"}},
            }
        )

        result = DocumentSynchronizer(settings, storage, feishu).sync()

        self.assertEqual(result, SyncResult(created=3))
        self.assertEqual(
            feishu.personal_folder_calls,
            [("folder-1", None), ("folder-2", None), ("folder-1", "folder-1-page-2")],
        )
        self.assertEqual(
            {source.source_key for source in storage.list_sources(active_only=True)},
            {"document:d1", "document:personal-1", "document:personal-2"},
        )

    def test_sync_updates_changed_document_and_mirror(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.nodes.append(
            {
                "node_token": "node-d2",
                "obj_token": "d2",
                "obj_type": "docx",
                "title": "Unchanged",
                "url": "https://example.test/wiki/node-d2",
                "obj_edit_time": "2026-08-26T02:00:00Z",
            }
        )
        feishu.documents["d2"] = {"document": {"document_id": "d2", "content": "No change"}}
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        feishu.read_document_calls.clear()
        feishu.nodes[1]["obj_edit_time"] = "2026-08-27T02:00:00Z"
        feishu.documents["d1"]["document"]["content"] = "Revised body"

        with patch.object(storage, "upsert_source", wraps=storage.upsert_source) as upsert_source:
            result = synchronizer.sync()

        self.assertEqual((result.created, result.changed, result.skipped, result.failed), (0, 1, 1, 0))
        self.assertEqual(feishu.read_document_calls, ["d1"])
        self.assertEqual(upsert_source.call_count, 1)
        content_hash = hashlib.sha256(b"Revised body\n").hexdigest()
        self.assertEqual(
            self.mirror_path(settings, "d1").read_text(encoding="utf-8"),
            "---\n"
            "source_key: \"document:d1\"\n"
            "document_token: \"d1\"\n"
            "url: \"https://example.test/wiki/node-d1\"\n"
            "wiki_path: \"Product / Engineering / Release plan\"\n"
            "source_updated_at: \"2026-08-27T02:00:00Z\"\n"
            f"content_hash: \"{content_hash}\"\n"
            "---\n\n"
            "# Release plan\n\nRevised body\n",
        )

    def test_completed_checkpoint_skips_unchanged_document_reads_and_writes(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        feishu.read_document_calls.clear()

        with patch.object(storage, "upsert_source", wraps=storage.upsert_source) as upsert_source:
            result = synchronizer.sync()

        self.assertEqual((result.created, result.changed, result.skipped, result.failed), (0, 0, 1, 0))
        self.assertEqual(feishu.read_document_calls, [])
        upsert_source.assert_not_called()

    def test_changed_update_time_reloads_document_even_when_revision_id_is_unchanged(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.nodes[1]["revision_id"] = "revision-1"
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        feishu.read_document_calls.clear()
        feishu.nodes[1]["obj_edit_time"] = "2026-08-27T02:00:00Z"
        feishu.documents["d1"]["document"]["content"] = "Revised body"

        result = synchronizer.sync()

        self.assertEqual((result.created, result.changed, result.skipped, result.failed), (0, 1, 0, 0))
        self.assertEqual(feishu.read_document_calls, ["d1"])

    def test_failed_full_scan_keeps_checkpoint_and_does_not_mark_missing_document_inactive(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.nodes.append(
            {
                "node_token": "node-d2",
                "obj_token": "d2",
                "obj_type": "docx",
                "title": "Still active",
                "url": "https://example.test/wiki/node-d2",
                "obj_edit_time": "2026-08-26T02:00:00Z",
            }
        )
        feishu.documents["d2"] = {"document": {"document_id": "d2", "content": "Keep me"}}
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        checkpoint = storage.get_checkpoint("documents")
        feishu.nodes = [feishu.nodes[1]]
        feishu.nodes[0]["obj_edit_time"] = "2026-08-27T02:00:00Z"
        feishu.fail_documents.add("d1")

        result = synchronizer.sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("documents"), checkpoint)
        sources = {source.source_key: source for source in storage.list_sources()}
        self.assertTrue(sources["document:d2"].active)

    def test_successful_full_scan_marks_missing_document_inactive_with_new_checkpoint(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.nodes.append(
            {
                "node_token": "node-d2",
                "obj_token": "d2",
                "obj_type": "docx",
                "title": "Retired",
                "url": "https://example.test/wiki/node-d2",
            }
        )
        feishu.documents["d2"] = {"document": {"document_id": "d2", "content": "Old"}}
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        feishu.nodes = [feishu.nodes[0], feishu.nodes[1]]

        synchronizer.sync()

        sources = {source.source_key: source for source in storage.list_sources()}
        self.assertFalse(sources["document:d2"].active)
        self.assertIsNotNone(storage.get_checkpoint("documents"))

    def test_inaccessible_personal_library_keeps_absent_source_active_until_a_complete_scan(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        feishu.personal_documents.append(
            {
                "token": "personal-1",
                "type": "docx",
                "name": "Personal notes",
                "modified_time": "2026-08-27T02:00:00Z",
            }
        )
        feishu.documents["personal-1"] = {
            "document": {"document_id": "personal-1", "content": "Keep me"}
        }
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        checkpoint = storage.get_checkpoint("documents")
        feishu.personal_documents.clear()
        feishu.fail_personal_document_listing = True

        result = synchronizer.sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("documents"), checkpoint)
        source = storage.get_source("document:personal-1")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertTrue(source.active)

        feishu.fail_personal_document_listing = False
        result = synchronizer.sync()

        self.assertEqual(result.failed, 0)
        source = storage.get_source("document:personal-1")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertFalse(source.active)

    def test_repeated_document_page_token_fails_without_advancing_checkpoint(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        checkpoint = storage.get_checkpoint("documents")
        calls = 0

        def list_wiki_spaces(*, page_token: str | None = None) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls < 3:
                return {"items": [], "has_more": True, "page_token": "loop"}
            return {"items": [], "has_more": False}

        feishu.list_wiki_spaces = list_wiki_spaces  # type: ignore[method-assign]

        result = synchronizer.sync()

        self.assertEqual(result.failed, 1)
        self.assertEqual(storage.get_checkpoint("documents"), checkpoint)

    def test_unsafe_document_token_uses_stable_digest_mirror_name_and_preserves_token(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        unsafe_token = "CON:/folder\\plan"
        feishu.nodes[1]["obj_token"] = unsafe_token
        feishu.documents[unsafe_token] = feishu.documents.pop("d1")

        result = DocumentSynchronizer(settings, storage, feishu).sync()

        mirror = self.mirror_path(settings, unsafe_token)
        mirrors_root = (settings.root / "data" / "documents").resolve()
        self.assertEqual(result.failed, 0)
        self.assertEqual(mirror.parent.resolve(), mirrors_root)
        self.assertTrue(mirror.is_file())
        self.assertEqual(mirror.name, f"document-{hashlib.sha256(unsafe_token.encode('utf-8')).hexdigest()}.md")
        content = mirror.read_text(encoding="utf-8")
        self.assertIn(f"source_key: {json.dumps(f'document:{unsafe_token}')}", content)
        self.assertIn(f"document_token: {json.dumps(unsafe_token)}", content)
        self.assertEqual(storage.list_sources(active_only=True)[0].source_key, f"document:{unsafe_token}")

    @staticmethod
    def mirror_path(settings: Settings, token: str) -> Path:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return settings.root / "data" / "documents" / f"document-{digest}.md"
