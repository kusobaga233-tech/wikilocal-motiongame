from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.settings import Settings
from wikilocal.storage import Storage
from wikilocal.sync_documents import DocumentSynchronizer


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

    def read_document(self, document: str) -> dict[str, object]:
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
        content_hash = hashlib.sha256("First line\nSecond line\n".encode("utf-8")).hexdigest()
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

    def test_sync_updates_changed_document_and_mirror(self) -> None:
        settings, storage = self.make_storage()
        feishu = FakeDocumentFeishu()
        synchronizer = DocumentSynchronizer(settings, storage, feishu)
        synchronizer.sync()
        feishu.nodes[1]["obj_edit_time"] = "2026-08-27T02:00:00Z"
        feishu.documents["d1"]["document"]["content"] = "Revised body"

        result = synchronizer.sync()

        self.assertEqual((result.created, result.changed, result.skipped, result.failed), (0, 1, 0, 0))
        content_hash = hashlib.sha256("Revised body\n".encode("utf-8")).hexdigest()
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
