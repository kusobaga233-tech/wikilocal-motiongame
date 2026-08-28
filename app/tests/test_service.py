from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal.retrieval import Answer, Evidence
from wikilocal.settings import Settings


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
                    metadata={"url": "https://example.test/d1", "wiki_path": "Team / Release"},
                ),
            ),
        )


class FakeSynchronizer:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self) -> object:
        self.calls += 1
        return type("Result", (), {"created": 1, "changed": 2, "skipped": 3, "failed": 0})()


class FailingSynchronizer:
    def sync(self) -> object:
        raise RuntimeError("token=top-secret\nuntrusted detail")


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
            "metadata": {"url": "https://example.test/d1", "wiki_path": "Team / Release"},
        }
    ]


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


def test_settings_only_reconfigures_an_existing_daily_task(client) -> None:
    from wikilocal.service import create_app
    from fastapi.testclient import TestClient

    test_client, settings, documents, chats = client
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
    assert "/assets/app.js" in response.text


def test_web_renders_sync_errors_and_model_unavailability() -> None:
    web_script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    assert "item.error" in web_script
    assert "health.model_available" in web_script


def test_setup_script_changes_to_the_absolute_app_directory_before_installing() -> None:
    setup_script = (Path(__file__).resolve().parents[1] / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    assert "Set-Location $app" in setup_script
    assert setup_script.index("Set-Location $app") < setup_script.index("-m pip install -e")


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
