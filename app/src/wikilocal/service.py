from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from wikilocal.feishu import FeishuClient
from wikilocal.indexing import Indexer
from wikilocal.ollama import ModelUnavailableError, OllamaClient
from wikilocal.retrieval import AnswerService, Retriever
from wikilocal.settings import Settings, SettingsError
from wikilocal.storage import SourceRecord, Storage
from wikilocal.sync_chats import ChatSynchronizer
from wikilocal.sync_documents import DocumentSynchronizer, SyncResult


SyncKind = Literal["documents", "chats", "all"]


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class SettingsPayload(BaseModel):
    daily_time: str
    documents_enabled: bool
    chats_enabled: bool
    chat_history_start: str | None = None


class Runtime:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        answer_service: Any,
        document_synchronizer: Any,
        chat_synchronizer: Any,
        indexer: Indexer | None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.answer_service = answer_service
        self.document_synchronizer = document_synchronizer
        self.chat_synchronizer = chat_synchronizer
        self.indexer = indexer
        self.last_sync: dict[str, dict[str, int | str | None]] = {
            "documents": _empty_sync_status(),
            "chats": _empty_sync_status(),
        }

    def synchronize(self, kind: SyncKind) -> dict[str, int | str | None]:
        selected = (
            (("documents", self.document_synchronizer), ("chats", self.chat_synchronizer))
            if kind == "all"
            else ((kind, self.document_synchronizer if kind == "documents" else self.chat_synchronizer),)
        )
        total = SyncResult()
        for name, synchronizer in selected:
            result = synchronizer.sync()
            self.last_sync[name] = _sync_result_dict(result)
            total = total.add(
                created=int(result.created),
                changed=int(result.changed),
                skipped=int(result.skipped),
                failed=int(result.failed),
            )
        if self.indexer is not None and total.failed == 0:
            for source in self.storage.list_sources(active_only=True):
                if source.text_content:
                    self.indexer.index_source(source.source_key)
        return _sync_result_dict(total)


def create_runtime(settings: Settings) -> Runtime:
    storage = Storage(settings)
    storage.initialize()
    feishu = FeishuClient()
    ollama = OllamaClient()
    return Runtime(
        settings=settings,
        storage=storage,
        answer_service=AnswerService(Retriever(storage, ollama), ollama),
        document_synchronizer=DocumentSynchronizer(settings, storage, feishu),
        chat_synchronizer=ChatSynchronizer(settings, storage, feishu),
        indexer=Indexer(storage, ollama),
    )


def create_app(
    settings: Settings,
    *,
    answer_service: Any | None = None,
    document_synchronizer: Any | None = None,
    chat_synchronizer: Any | None = None,
    storage: Storage | None = None,
    indexer: Indexer | None = None,
) -> FastAPI:
    owns_storage = storage is None
    if storage is None:
        storage = Storage(settings)
        storage.initialize()
    if answer_service is None or document_synchronizer is None or chat_synchronizer is None:
        default_runtime = create_runtime(settings)
        if owns_storage:
            storage.close()
            storage = default_runtime.storage
        answer_service = answer_service or default_runtime.answer_service
        document_synchronizer = document_synchronizer or default_runtime.document_synchronizer
        chat_synchronizer = chat_synchronizer or default_runtime.chat_synchronizer
        indexer = indexer or default_runtime.indexer

    runtime = Runtime(
        settings,
        storage,
        answer_service,
        document_synchronizer,
        chat_synchronizer,
        indexer,
    )
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_storage:
            runtime.storage.close()

    app = FastAPI(title="WikiLocal", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "host": "127.0.0.1",
            "models": {"answer": "qwen3:4b", "embedding": "bge-m3", "reranker": "bge-reranker-v2-m3"},
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, object]:
        return _settings_dict(runtime.settings)

    @app.put("/api/settings")
    def update_settings(payload: SettingsPayload) -> dict[str, object]:
        candidate = Settings(
            root=runtime.settings.root,
            daily_time=payload.daily_time,
            documents_enabled=payload.documents_enabled,
            chats_enabled=payload.chats_enabled,
            chat_history_start=payload.chat_history_start,
        )
        try:
            candidate.save()
        except SettingsError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        runtime.settings.daily_time = candidate.daily_time
        runtime.settings.documents_enabled = candidate.documents_enabled
        runtime.settings.chats_enabled = candidate.chats_enabled
        runtime.settings.chat_history_start = candidate.chat_history_start
        return _settings_dict(runtime.settings)

    @app.post("/api/sync/{kind}")
    def synchronize(kind: str) -> dict[str, int | str | None]:
        if kind not in {"documents", "chats", "all"}:
            raise HTTPException(status_code=404, detail="Unknown sync kind")
        try:
            return runtime.synchronize(kind)  # type: ignore[arg-type]
        except (ModelUnavailableError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/sync/status")
    def sync_status() -> dict[str, object]:
        return {"documents": runtime.last_sync["documents"], "chats": runtime.last_sync["chats"]}

    @app.post("/api/answer")
    def answer(payload: AnswerRequest) -> dict[str, object]:
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="question must not be empty")
        try:
            result = runtime.answer_service.answer(question)
        except ModelUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"text": result.text, "citations": [_evidence_dict(item) for item in result.citations]}

    @app.get("/api/sources")
    def sources(query: str = "", limit: int = 100) -> dict[str, object]:
        safe_limit = max(1, min(limit, 200))
        matches = [
            _source_dict(source)
            for source in runtime.storage.list_sources(active_only=True)
            if not query.strip()
            or query.casefold() in source.title.casefold()
            or query.casefold() in source.text_content.casefold()
        ]
        return {"sources": matches[:safe_limit], "total": len(matches)}

    web_directory = Path(__file__).resolve().parents[2] / "web"
    if web_directory.is_dir():
        app.mount("/assets", StaticFiles(directory=web_directory), name="assets")

        @app.get("/", include_in_schema=False)
        def web_app() -> FileResponse:
            return FileResponse(web_directory / "index.html")

    return app


def _settings_dict(settings: Settings) -> dict[str, object]:
    return {
        "daily_time": settings.daily_time,
        "documents_enabled": settings.documents_enabled,
        "chats_enabled": settings.chats_enabled,
        "chat_history_start": settings.chat_history_start,
    }


def _sync_result_dict(result: SyncResult | Any) -> dict[str, int | str | None]:
    return {
        "created": int(result.created),
        "changed": int(result.changed),
        "skipped": int(result.skipped),
        "failed": int(result.failed),
        "error": None,
    }


def _empty_sync_status() -> dict[str, int | str | None]:
    return {"created": 0, "changed": 0, "skipped": 0, "failed": 0, "error": None}


def _evidence_dict(evidence: Any) -> dict[str, object]:
    return {
        "chunk_id": evidence.chunk_id,
        "source_key": evidence.source_key,
        "title": evidence.title,
        "text_content": evidence.text_content,
        "metadata": dict(evidence.metadata),
    }


def _source_dict(source: SourceRecord) -> dict[str, object]:
    return {
        "source_key": source.source_key,
        "source_type": source.source_type,
        "title": source.title,
        "metadata": dict(source.metadata),
        "excerpt": source.text_content[:400],
    }
