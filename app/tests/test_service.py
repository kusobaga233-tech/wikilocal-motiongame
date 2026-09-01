from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.retrieval import Answer, Evidence
from wikilocal.settings import Settings
from wikilocal.sync_documents import SyncResult


class FakeAnswerService:
    def answer(self, question: str) -> Answer:
        return Answer(
            text=f"Answer for {question}",
            citations=(
                Evidence(
                    chunk_id="document:d1:0:abc",
                    source_key="document:d1",
                    title="Release plan",
                    text_content="The deployment changed to Friday.",
                    metadata={
                        "url": "https://example.test/d1",
                        "wiki_path": "Team / Release",
                        "sender": "Mia",
                        "sent_at": "2026-08-28T09:30:00+08:00",
                    },
                ),
            ),
        )


class FakeStreamingAnswerService(FakeAnswerService):
    def stream_answer(self, question: str):
        answer = self.answer(question)
        return iter(("Answer ", f"for {question}")), answer.citations


class FakeSynchronizer:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self) -> object:
        self.calls += 1
        return type("Result", (), {"created": 1, "changed": 2, "skipped": 3, "failed": 0})()


class FailingSynchronizer:
    def sync(self) -> object:
        raise RuntimeError("token=top-secret\nuntrusted detail")


class FailingIndexer:
    def index_source(self, source_key: str) -> int:
        raise RuntimeError(f"embedding token=top-secret for {source_key}")


class FailedResultSynchronizer:
    def sync(self) -> SyncResult:
        return SyncResult(failed=1)


class MutatingSynchronizer:
    def __init__(self, settings: Settings, storage: object) -> None:
        self._settings = settings
        self._storage = storage

    def sync(self) -> SyncResult:
        from wikilocal.storage import SourceRecord

        self._storage.upsert_source(
            SourceRecord("document:d1", "document", "New title", "New content", {}, True)
        )
        self._storage.set_checkpoint("documents", {"completed_at": "new"})
        self._storage.replace_fts_chunks("document:d1", [("document:d1:0:new", "New content", "New title")])
        mirror = self._settings.root / "data" / "documents" / "document-d1.md"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text("new mirror", encoding="utf-8")
        return SyncResult(changed=1)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        document_sync = FakeSynchronizer()
        chat_sync = FakeSynchronizer()
        app = create_app(
            settings,
            answer_service=FakeAnswerService(),
            document_synchronizer=document_sync,
            chat_synchronizer=chat_sync,
            model_status_provider=lambda: {"qwen3:4b": False, "bge-m3": False, "bge-reranker-v2-m3": False},
        )
        with TestClient(app) as test_client:
            yield test_client, settings, document_sync, chat_sync


def test_answer_returns_local_text_and_citations(client) -> None:
    test_client, _settings, _documents, _chats = client

    response = test_client.post("/api/answer", json={"question": "What changed?"})

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "Answer for What changed?"
    assert body["citations"] == [
        {
            "chunk_id": "document:d1:0:abc",
            "source_key": "document:d1",
            "title": "Release plan",
            "text_content": "The deployment changed to Friday.",
            "metadata": {
                "url": "https://example.test/d1",
                "wiki_path": "Team / Release",
                "sender": "Mia",
                "sent_at": "2026-08-28T09:30:00+08:00",
            },
        }
    ]


def test_streaming_answer_emits_ndjson_and_persists_cited_local_history() -> None:
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        app = create_app(
            settings,
            answer_service=FakeStreamingAnswerService(),
            document_synchronizer=FakeSynchronizer(),
            chat_synchronizer=FakeSynchronizer(),
            model_status_provider=dict,
        )
        with TestClient(app) as test_client:
            response = test_client.post("/api/answer/stream", json={"question": "What changed?"})

        restarted_app = create_app(
            settings,
            answer_service=FakeStreamingAnswerService(),
            document_synchronizer=FakeSynchronizer(),
            chat_synchronizer=FakeSynchronizer(),
            model_status_provider=dict,
        )
        with TestClient(restarted_app) as restarted_client:
            history = restarted_client.get("/api/conversations")

        history_file = settings.root / "data" / "conversations" / "history.jsonl"

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in response.text.splitlines()]
        assert [event["type"] for event in events] == ["delta", "delta", "answer"]
        assert events[-1]["text"] == "Answer for What changed?"
        assert history.status_code == 200
        assert history.json()["turns"] == [
            {
                "question": "What changed?",
                "answer": "Answer for What changed?",
                "timestamp": history.json()["turns"][0]["timestamp"],
                "citations": [
                    {
                        "chunk_id": "document:d1:0:abc",
                        "source_key": "document:d1",
                        "title": "Release plan",
                        "text_content": "The deployment changed to Friday.",
                        "metadata": {
                            "url": "https://example.test/d1",
                            "wiki_path": "Team / Release",
                            "sender": "Mia",
                            "sent_at": "2026-08-28T09:30:00+08:00",
                        },
                    }
                ],
            }
        ]
        assert '"question":"What changed?"' in history_file.read_text(encoding="utf-8")


def test_json_answer_fallback_remains_cited_and_persists_local_history(client) -> None:
    test_client, settings, _documents, _chats = client

    response = test_client.post("/api/answer", json={"question": "What changed?"})
    history = test_client.get("/api/conversations")

    assert response.status_code == 200
    assert response.json()["citations"][0]["title"] == "Release plan"
    assert history.json()["turns"][0]["answer"] == "Answer for What changed?"
    assert (settings.root / "data" / "conversations" / "history.jsonl").is_file()


def test_settings_and_manual_sync_routes_update_local_state(client) -> None:
    test_client, settings, documents, _chats = client

    updated = test_client.put(
        "/api/settings",
        json={
            "daily_time": "03:30",
            "documents_enabled": False,
            "chats_enabled": True,
            "chat_history_start": "2025-01-01",
        },
    )
    sync = test_client.post("/api/sync/documents")
    status = test_client.get("/api/sync/status")

    assert updated.status_code == 200
    assert updated.json()["daily_time"] == "03:30"
    assert Settings.load(settings.root).documents_enabled is False
    assert sync.status_code == 200
    assert sync.json()["created"] == 1
    assert documents.calls == 1
    assert status.status_code == 200
    assert status.json()["documents"]["created"] == 1


def test_sync_status_is_reloaded_after_app_restart() -> None:
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        first_app = create_app(
            settings,
            answer_service=FakeAnswerService(),
            document_synchronizer=FakeSynchronizer(),
            chat_synchronizer=FakeSynchronizer(),
            model_status_provider=dict,
        )
        with TestClient(first_app) as first_client:
            assert first_client.post("/api/sync/documents").status_code == 200

        restarted_app = create_app(
            settings,
            answer_service=FakeAnswerService(),
            document_synchronizer=FakeSynchronizer(),
            chat_synchronizer=FakeSynchronizer(),
            model_status_provider=dict,
        )
        with TestClient(restarted_app) as restarted_client:
            status = restarted_client.get("/api/sync/status")

    assert status.status_code == 200
    assert status.json() == {
        "documents": {"created": 1, "changed": 2, "skipped": 3, "failed": 0, "error": None},
        "chats": {"created": 0, "changed": 0, "skipped": 0, "failed": 0, "error": None},
    }


def test_settings_only_reconfigures_an_existing_daily_task(client) -> None:
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app

    _test_client, settings, documents, chats = client
    calls: list[str] = []
    app = create_app(
        settings,
        answer_service=FakeAnswerService(),
        document_synchronizer=documents,
        chat_synchronizer=chats,
        schedule_reconfigurer=lambda candidate: calls.append(candidate.daily_time) or False,
        model_status_provider=lambda: {"qwen3:4b": False, "bge-m3": False, "bge-reranker-v2-m3": False},
    )

    with TestClient(app) as configured_client:
        response = configured_client.put(
            "/api/settings",
            json={
                "daily_time": "04:15",
                "documents_enabled": True,
                "chats_enabled": True,
                "chat_history_start": None,
            },
        )

    assert response.status_code == 200
    assert calls == ["04:15"]


def test_scheduled_all_sync_honors_settings_but_manual_source_sync_is_forced(client) -> None:
    test_client, _settings, documents, chats = client
    test_client.put(
        "/api/settings",
        json={
            "daily_time": "02:00",
            "documents_enabled": False,
            "chats_enabled": True,
            "chat_history_start": None,
        },
    )

    scheduled = test_client.app.state.runtime.synchronize("all", honor_enabled=True)
    manual = test_client.post("/api/sync/documents")

    assert scheduled["created"] == 1
    assert documents.calls == 1
    assert chats.calls == 1
    assert manual.status_code == 200
    assert documents.calls == 1


def test_synchronize_indexes_active_empty_sources_to_retire_old_evidence() -> None:
    from wikilocal.service import Runtime
    from wikilocal.storage import SourceRecord, Storage

    class CapturingIndexer:
        def __init__(self) -> None:
            self.source_keys: list[str] = []

        def index_source(self, source_key: str) -> int:
            self.source_keys.append(source_key)
            return 0

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        storage = Storage(settings)
        storage.initialize()
        storage.upsert_source(SourceRecord("document:empty", "document", "Empty", "", {}, True))
        indexer = CapturingIndexer()
        runtime = Runtime(
            settings,
            storage,
            FakeAnswerService(),
            FakeSynchronizer(),
            FakeSynchronizer(),
            indexer,  # type: ignore[arg-type]
        )
        try:
            runtime.synchronize("documents")
        finally:
            storage.close()

    assert indexer.source_keys == ["document:empty"]


def test_manual_all_sync_forces_both_sources_when_disabled(client) -> None:
    test_client, _settings, documents, chats = client
    test_client.put(
        "/api/settings",
        json={
            "daily_time": "02:00",
            "documents_enabled": False,
            "chats_enabled": False,
            "chat_history_start": None,
        },
    )

    response = test_client.post("/api/sync/all")

    assert response.status_code == 200
    assert response.json()["created"] == 2
    assert documents.calls == 1
    assert chats.calls == 1


def test_failed_existing_task_reconfiguration_keeps_persisted_daily_time(client) -> None:
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app

    _test_client, settings, documents, chats = client
    app = create_app(
        settings,
        answer_service=FakeAnswerService(),
        document_synchronizer=documents,
        chat_synchronizer=chats,
        schedule_reconfigurer=lambda _candidate: (_ for _ in ()).throw(ValueError("task rejected")),
        model_status_provider=lambda: {"qwen3:4b": False, "bge-m3": False, "bge-reranker-v2-m3": False},
    )

    with TestClient(app, raise_server_exceptions=False) as configured_client:
        response = configured_client.put(
            "/api/settings",
            json={
                "daily_time": "04:15",
                "documents_enabled": True,
                "chats_enabled": True,
                "chat_history_start": None,
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to update the existing daily task."
    assert Settings.load(settings.root).daily_time == "02:00"


def test_indexing_failure_stores_sanitized_status_after_source_sync() -> None:
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app
    from wikilocal.storage import SourceRecord, Storage

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        storage = Storage(settings)
        storage.initialize()
        storage.upsert_source(
            SourceRecord("document:index-me", "document", "Index me", "Text for indexing", {}, True)
        )
        checkpoint = {"completed_at": "2026-08-28T03:00:00+00:00"}
        indexed_chunks = [("document:index-me:0:existing", "Previously indexed", "Index me")]
        storage.set_checkpoint("documents", checkpoint)
        storage.replace_fts_chunks("document:index-me", indexed_chunks)
        app = create_app(
            settings,
            answer_service=FakeAnswerService(),
            document_synchronizer=FakeSynchronizer(),
            chat_synchronizer=FakeSynchronizer(),
            storage=storage,
            indexer=FailingIndexer(),
            model_status_provider=dict,
        )

        with TestClient(app, raise_server_exceptions=False) as failing_client:
            response = failing_client.post("/api/sync/documents")
            status = failing_client.get("/api/sync/status")

        assert response.status_code == 503
        assert status.json()["documents"] == {
            "created": 1,
            "changed": 2,
            "skipped": 3,
            "failed": 1,
            "error": "Synchronization failed (RuntimeError).",
        }
        assert "top-secret" not in status.text
        assert storage.get_checkpoint("documents") == checkpoint
        assert storage.list_fts_chunk_ids("document:index-me") == {indexed_chunks[0][0]}
        sync_log = settings.root / "data" / "logs" / "sync-status.json"
        assert "top-secret" not in sync_log.read_text(encoding="utf-8")
        storage.close()


def test_indexing_failure_restores_sources_checkpoints_fts_and_document_mirrors() -> None:
    from wikilocal.service import Runtime
    from wikilocal.storage import SourceRecord, Storage

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        storage = Storage(settings)
        storage.initialize()
        storage.upsert_source(SourceRecord("document:d1", "document", "Old title", "Old content", {}, True))
        storage.set_checkpoint("documents", {"completed_at": "old"})
        storage.replace_fts_chunks("document:d1", [("document:d1:0:old", "Old content", "Old title")])
        mirror = settings.root / "data" / "documents" / "document-d1.md"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text("old mirror", encoding="utf-8")
        runtime = Runtime(
            settings,
            storage,
            FakeAnswerService(),
            MutatingSynchronizer(settings, storage),
            FakeSynchronizer(),
            FailingIndexer(),  # type: ignore[arg-type]
        )

        with pytest.raises(RuntimeError):
            runtime.synchronize("documents")

        assert storage.get_source("document:d1") == SourceRecord(
            "document:d1", "document", "Old title", "Old content", {}, True
        )
        assert storage.get_checkpoint("documents") == {"completed_at": "old"}
        assert storage.list_fts_chunk_ids("document:d1") == {"document:d1:0:old"}
        assert mirror.read_text(encoding="utf-8") == "old mirror"
        storage.close()


def test_later_source_index_failure_rebuilds_vectors_from_restored_fts_state() -> None:
    from wikilocal.indexing import Indexer
    from wikilocal.retrieval import Retriever
    from wikilocal.service import Runtime
    from wikilocal.storage import SourceRecord, Storage

    class LocalOllama:
        def embed(self, texts: list[str]) -> list[list[float]]:
            if "B changed" in texts:
                raise RuntimeError("source B embedding failed")
            return [[float(index)] for index, _text in enumerate(texts)]

        def rerank(self, question: str, texts: list[str]) -> list[float]:
            return [float(len(texts) - index) for index, _text in enumerate(texts)]

    class InMemoryVectors:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []
            self.disabled = False

        def add(self, rows: list[dict[str, object]]) -> None:
            self.rows.extend(rows)

        def delete_chunks(self, chunk_ids: list[str] | tuple[str, ...]) -> None:
            self.rows = [row for row in self.rows if row["chunk_id"] not in chunk_ids]

        def clear(self) -> None:
            self.rows = []
            self.disabled = True

        def search(self, embedding: list[float], limit: int) -> list[dict[str, object]]:
            return [] if self.disabled else self.rows[:limit]

        def disable(self) -> None:
            self.disabled = True

        def replace_all(self, rows: list[dict[str, object]]) -> None:
            self.rows = list(rows)
            self.disabled = False

    class MutatingSynchronizer:
        def __init__(self, storage: Storage) -> None:
            self._storage = storage

        def sync(self) -> SyncResult:
            self._storage.upsert_source(
                SourceRecord("document:a", "document", "A", "A changed", {}, True)
            )
            self._storage.upsert_source(
                SourceRecord("document:b", "document", "B", "B changed", {}, True)
            )
            return SyncResult(changed=2)

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        storage = Storage(settings)
        storage.initialize()
        ollama = LocalOllama()
        vectors = InMemoryVectors()
        indexer = Indexer(storage, ollama, vectors)  # type: ignore[arg-type]
        storage.upsert_source(SourceRecord("document:a", "document", "A", "A original", {}, True))
        storage.upsert_source(SourceRecord("document:b", "document", "B", "B original", {}, True))
        indexer.index_source("document:a")
        indexer.index_source("document:b")
        original_vectors = list(vectors.rows)
        runtime = Runtime(
            settings,
            storage,
            FakeAnswerService(),
            MutatingSynchronizer(storage),
            FakeSynchronizer(),
            indexer,
        )

        with pytest.raises(RuntimeError, match="source B embedding failed"):
            runtime.synchronize("documents")

        assert [row.text_content for row in storage.search_fts("original", limit=8)] == [
            "A original",
            "B original",
        ]
        assert [
            (str(row["chunk_id"]), str(row["source_key"]), str(row["text_content"]))
            for row in vectors.rows
        ] == [
            (str(row["chunk_id"]), str(row["source_key"]), str(row["text_content"]))
            for row in original_vectors
        ]
        assert [evidence.text_content for evidence in Retriever(storage, ollama, vectors).search("semantic lookup")] == [
            "A original",
            "B original",
        ]
        storage.close()


def test_later_successful_sync_retries_failed_vector_recovery() -> None:
    from wikilocal.indexing import Indexer
    from wikilocal.retrieval import Retriever
    from wikilocal.service import Runtime
    from wikilocal.storage import SourceRecord, Storage

    class TransientlyFailingOllama:
        def __init__(self) -> None:
            self.fail_index = True
            self.fail_recovery = False

        def embed(self, texts: list[str]) -> list[list[float]]:
            if self.fail_index and "B changed" in texts:
                raise RuntimeError("source B embedding failed")
            if self.fail_recovery and "A original" in texts:
                raise RuntimeError("recovery embedding failed")
            return [[float(index)] for index, _text in enumerate(texts)]

        def rerank(self, question: str, texts: list[str]) -> list[float]:
            return [float(len(texts) - index) for index, _text in enumerate(texts)]

    class FailClosedVectors:
        def __init__(self, disabled_marker: Path) -> None:
            self.rows: list[dict[str, object]] = []
            self.disabled = False
            self.disabled_marker = disabled_marker
            self.searches = 0

        def add(self, rows: list[dict[str, object]]) -> None:
            self.rows.extend(rows)

        def delete_chunks(self, chunk_ids: list[str] | tuple[str, ...]) -> None:
            self.rows = [row for row in self.rows if row["chunk_id"] not in chunk_ids]

        def clear(self) -> None:
            self.rows = []
            self.disabled = True

        def disable(self) -> None:
            self.disabled = True
            self.disabled_marker.parent.mkdir(parents=True, exist_ok=True)
            self.disabled_marker.write_text("disabled\n", encoding="utf-8", newline="\n")

        def replace_all(self, rows: list[dict[str, object]]) -> None:
            self.rows = list(rows)
            self.disabled_marker.unlink(missing_ok=True)
            self.disabled = False

        def vectors_disabled(self) -> bool:
            return self.disabled

        def search(self, embedding: list[float], limit: int) -> list[dict[str, object]]:
            self.searches += 1
            return [] if self.disabled else self.rows[:limit]

    class MutatingSynchronizer:
        def __init__(self, storage: Storage) -> None:
            self._storage = storage

        def sync(self) -> SyncResult:
            self._storage.upsert_source(
                SourceRecord("document:a", "document", "A", "A changed", {}, True)
            )
            self._storage.upsert_source(
                SourceRecord("document:b", "document", "B", "B changed", {}, True)
            )
            return SyncResult(changed=2)

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        storage = Storage(settings)
        storage.initialize()
        ollama = TransientlyFailingOllama()
        vectors = FailClosedVectors(settings.root / "data" / "index" / ".vector-search-disabled")
        indexer = Indexer(storage, ollama, vectors)  # type: ignore[arg-type]
        storage.upsert_source(SourceRecord("document:a", "document", "A", "A original", {}, True))
        storage.upsert_source(SourceRecord("document:b", "document", "B", "B original", {}, True))
        indexer.index_source("document:a")
        indexer.index_source("document:b")
        ollama.fail_recovery = True
        runtime = Runtime(
            settings,
            storage,
            FakeAnswerService(),
            MutatingSynchronizer(storage),
            FakeSynchronizer(),
            indexer,
        )

        try:
            with pytest.raises(RuntimeError, match="source B embedding failed"):
                runtime.synchronize("documents")

            assert vectors.disabled is True
            assert vectors.disabled_marker.is_file()
            assert vectors.rows == []
            ollama.fail_index = False
            ollama.fail_recovery = False

            runtime.synchronize("documents")

            assert vectors.disabled is False
            assert not vectors.disabled_marker.exists()
            assert [evidence.text_content for evidence in Retriever(storage, ollama, vectors).search("semantic lookup")] == [
                "A changed",
                "B changed",
            ]
            assert vectors.searches == 1
        finally:
            storage.close()


def test_failed_sync_result_persists_a_generic_sanitized_error() -> None:
    from wikilocal.service import Runtime
    from wikilocal.storage import Storage

    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        storage = Storage(settings)
        storage.initialize()
        runtime = Runtime(
            settings,
            storage,
            FakeAnswerService(),
            FailedResultSynchronizer(),
            FakeSynchronizer(),
            None,
        )

        with pytest.raises(RuntimeError):
            runtime.synchronize("documents")

        assert runtime.last_sync["documents"] == {
            "created": 0,
            "changed": 0,
            "skipped": 0,
            "failed": 1,
            "error": "Synchronization failed (SyncResultFailure).",
        }
        assert "top-secret" not in (settings.root / "data" / "logs" / "sync-status.json").read_text(encoding="utf-8")
        storage.close()


def test_failed_sync_stores_a_sanitized_error_for_status_ui(client) -> None:
    from fastapi.testclient import TestClient

    from wikilocal.service import create_app

    _test_client, settings, _documents, chats = client
    app = create_app(
        settings,
        answer_service=FakeAnswerService(),
        document_synchronizer=FailingSynchronizer(),
        chat_synchronizer=chats,
        model_status_provider=lambda: {"qwen3:4b": False, "bge-m3": False, "bge-reranker-v2-m3": False},
    )

    with TestClient(app) as failing_client:
        response = failing_client.post("/api/sync/documents")
        status = failing_client.get("/api/sync/status")

    assert response.status_code == 503
    assert status.json()["documents"] == {
        "created": 0,
        "changed": 0,
        "skipped": 0,
        "failed": 1,
        "error": "Synchronization failed (RuntimeError).",
    }
    assert "top-secret" not in status.text
    assert "top-secret" not in response.text
    assert response.json()["detail"] == "Synchronization failed (RuntimeError)."


def test_health_reports_local_model_availability(client) -> None:
    test_client, _settings, _documents, _chats = client

    health = test_client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["models"] == {
        "answer": {"name": "qwen3:4b", "available": False},
        "embedding": {"name": "bge-m3", "available": False},
        "reranker": {"name": "bge-reranker-v2-m3", "available": False},
    }
    assert health.json()["model_available"] is False


def test_api_rejects_empty_question_and_invalid_sync_kind(client) -> None:
    test_client, _settings, _documents, _chats = client

    assert test_client.post("/api/answer", json={"question": "  "}).status_code == 422
    assert test_client.post("/api/sync/unknown").status_code == 404
    assert test_client.get("/api/health").json()["status"] == "ok"


def test_root_serves_the_local_question_workspace(client) -> None:
    test_client, _settings, _documents, _chats = client

    response = test_client.get("/")

    assert response.status_code == 200
    assert "WikiLocal" in response.text
    assert "/assets/app.js?v=" in response.text


def test_web_assets_use_browser_executable_media_types(client) -> None:
    test_client, _settings, _documents, _chats = client

    script = test_client.get("/assets/app.js")
    stylesheet = test_client.get("/assets/styles.css")

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("application/javascript")
    assert script.headers["cache-control"] == "no-store"
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")


def test_web_renders_sync_errors_and_model_unavailability() -> None:
    web_script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    assert "item.error" in web_script
    assert "health.model_available" in web_script


def test_web_loads_local_history_and_renders_citation_details() -> None:
    web_script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    web_page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")

    assert 'api("/conversations")' in web_script
    assert 'fetch("/api/answer/stream"' in web_script
    assert "appendAnswer(turn.question, { text: turn.answer, citations: turn.citations })" in web_script
    assert "metadata.sender" in web_script
    assert "metadata.sent_at" in web_script
    assert "<details" in web_script
    assert 'aria-live="polite"' in web_page


def test_setup_script_changes_to_the_absolute_app_directory_before_installing() -> None:
    setup_script = (Path(__file__).resolve().parents[1] / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    assert "Set-Location $app" in setup_script
    assert setup_script.index("Set-Location $app") < setup_script.index("-m pip install -e")


def test_create_runtime_shares_one_local_lancedb_store_between_indexing_and_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wikilocal import service

    opened_directories: list[Path] = []
    vector_store = object()

    class FakeLanceDBVectorStore:
        @classmethod
        def open(cls, directory: Path) -> object:
            opened_directories.append(directory)
            return vector_store

    monkeypatch.setattr(service, "LanceDBVectorStore", FakeLanceDBVectorStore, raising=False)
    with tempfile.TemporaryDirectory() as temporary_directory:
        settings = Settings.load(Path(temporary_directory))
        runtime = service.create_runtime(settings)
        try:
            assert opened_directories == [settings.database_path.parent / "lancedb"]
            assert runtime.indexer is not None
            assert runtime.indexer._vector_store is vector_store
            assert runtime.answer_service._retriever._vector_store is vector_store
        finally:
            runtime.storage.close()


def test_create_runtime_closes_initialized_storage_when_lancedb_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wikilocal import service

    created_storages: list[object] = []

    class TrackingStorage:
        def __init__(self, settings: Settings) -> None:
            self.closed = False
            self.initialized = False
            created_storages.append(self)

        def initialize(self) -> None:
            self.initialized = True

        def close(self) -> None:
            self.closed = True

    class FailingLanceDBVectorStore:
        @classmethod
        def open(cls, directory: Path) -> object:
            raise RuntimeError("LanceDB unavailable")

    monkeypatch.setattr(service, "Storage", TrackingStorage)
    monkeypatch.setattr(service, "LanceDBVectorStore", FailingLanceDBVectorStore)
    with tempfile.TemporaryDirectory() as temporary_directory, pytest.raises(
        RuntimeError, match="LanceDB unavailable"
    ):
        service.create_runtime(Settings.load(Path(temporary_directory)))

    assert len(created_storages) == 1
    storage = created_storages[0]
    assert storage.initialized is True  # type: ignore[attr-defined]
    assert storage.closed is True  # type: ignore[attr-defined]


def test_setup_script_runs_project_permission_preflight_before_model_pulls() -> None:
    setup_script = (Path(__file__).resolve().parents[1] / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    install = setup_script.index('-m pip install -e ".[test,vector]"')
    preflight = setup_script.index("FeishuClient().permission_preflight()")
    first_model_pull = setup_script.index("pull qwen3:4b")

    assert install < preflight < first_model_pull
    assert "missing required read-only scopes" in setup_script
    assert "result.missing_scopes" in setup_script
    assert "result.remediation_commands" in setup_script


def test_start_script_waits_for_local_health_before_opening_the_browser() -> None:
    start_script = (Path(__file__).resolve().parents[1] / "scripts" / "start.ps1").read_text(encoding="utf-8")

    server_start = start_script.index("Start-Process -FilePath $python")
    health_poll = start_script.index('"http://127.0.0.1:8765/api/health"')
    browser_open = start_script.index('Start-Process -FilePath "http://127.0.0.1:8765"')

    assert "-WindowStyle Hidden" in start_script[server_start:health_poll]
    assert "-PassThru" in start_script[server_start:health_poll]
    assert "AddSeconds(20)" in start_script
    assert server_start < health_poll < browser_open
    assert "Stop-Process -Id $server.Id" in start_script
    assert "exit 1" in start_script


def test_sources_returns_searchable_active_local_sources(client) -> None:
    from wikilocal.storage import SourceRecord

    test_client, _settings, _documents, _chats = client
    test_client.app.state.runtime.storage.upsert_source(
        SourceRecord(
            "document:release",
            "document",
            "Release plan",
            "Friday deployment window",
            {"url": "https://example.test/release"},
            True,
        )
    )

    response = test_client.get("/api/sources?query=friday")

    assert response.status_code == 200
    assert response.json()["sources"][0]["source_key"] == "document:release"
