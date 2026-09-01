from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from wikilocal.feishu import FeishuClient
from wikilocal.indexing import Indexer, LanceDBVectorStore
from wikilocal.ollama import ModelUnavailableError, OllamaClient
from wikilocal.retrieval import AnswerService, Retriever
from wikilocal.scheduler import reconfigure_daily_task_if_installed
from wikilocal.settings import Settings, SettingsError
from wikilocal.storage import SourceRecord, Storage, SyncStateSnapshot
from wikilocal.sync_chats import ChatSynchronizer
from wikilocal.sync_documents import DocumentSynchronizer, SyncResult

SyncKind = Literal["documents", "chats", "all"]
MODEL_NAMES = {"answer": "qwen3:4b", "embedding": "bge-m3", "reranker": "bge-reranker-v2-m3"}
ModelStatusProvider = Callable[[], Mapping[str, bool]]
ScheduleReconfigurer = Callable[[Settings], bool]


class SyncResultFailure(RuntimeError):
    """Raised when a synchronizer reports failed work without raising itself."""


class _SyncSnapshot:
    def __init__(self, storage: SyncStateSnapshot, mirrors: dict[Path, bytes]) -> None:
        self.storage = storage
        self.mirrors = mirrors


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
        self.last_sync = storage.load_sync_status()
        self.conversation_history = ConversationHistory(settings.root / "data" / "conversations")

    def synchronize(
        self, kind: SyncKind, *, honor_enabled: bool = False
    ) -> dict[str, int | str | None]:
        selected = (
            (("documents", self.document_synchronizer), ("chats", self.chat_synchronizer))
            if kind == "all"
            else ((kind, self.document_synchronizer if kind == "documents" else self.chat_synchronizer),)
        )
        if kind == "all" and honor_enabled:
            selected = tuple(
                (name, synchronizer)
                for name, synchronizer in selected
                if (name == "documents" and self.settings.documents_enabled)
                or (name == "chats" and self.settings.chats_enabled)
        )
        snapshot = _snapshot_sync_state(self.storage, self.settings.root / "data" / "documents")
        total = SyncResult()
        completed: list[tuple[str, Any]] = []
        for name, synchronizer in selected:
            try:
                result = synchronizer.sync()
            except Exception as error:
                _restore_sync_state(self.storage, self.settings.root / "data" / "documents", snapshot)
                self._record_sync_status(name, _failed_sync_status(error))
                raise
            total = total.add(
                created=int(result.created),
                changed=int(result.changed),
                skipped=int(result.skipped),
                failed=int(result.failed),
            )
            completed.append((name, result))
        if total.failed:
            error = SyncResultFailure()
            _restore_sync_state(self.storage, self.settings.root / "data" / "documents", snapshot)
            for name, result in completed:
                self._record_sync_status(name, _failed_result_status(result, error))
            raise error
        if self.indexer is not None:
            try:
                self._retry_disabled_vector_recovery()
                for source in self.storage.list_sources(active_only=True):
                    self.indexer.index_source(source.source_key)
            except Exception as error:
                _restore_sync_state(self.storage, self.settings.root / "data" / "documents", snapshot)
                self._recover_vectors_after_rollback()
                for name, result in completed:
                    self._record_sync_status(name, _failed_result_status(result, error))
                raise
        for name, result in completed:
            self._record_sync_status(name, _sync_result_dict(result))
        return _sync_result_dict(total)

    def _record_sync_status(self, name: str, status: dict[str, int | str | None]) -> None:
        self.last_sync[name] = status
        self.storage.save_sync_status(self.last_sync)

    def _recover_vectors_after_rollback(self) -> None:
        if self.indexer is None:
            return
        rebuild = getattr(self.indexer, "rebuild_vectors_from_fts", None)
        if not callable(rebuild):
            return
        try:
            rebuild()
        except Exception:  # noqa: BLE001
            # The indexer has disabled vector search; preserve the original sync failure.
            return

    def _retry_disabled_vector_recovery(self) -> None:
        if self.indexer is None:
            return
        retry = getattr(self.indexer, "retry_disabled_vector_recovery", None)
        if callable(retry):
            retry()


class ConversationHistory:
    """Append-only local UI history; no conversation content leaves the data directory."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / "history.jsonl"

    def load(self) -> list[dict[str, object]]:
        if not self._path.is_file():
            return []
        turns: list[dict[str, object]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                turn = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _valid_conversation_turn(turn):
                turns.append(turn)
        return turns

    def append(self, question: str, answer: str, citations: list[dict[str, object]]) -> dict[str, object]:
        turn: dict[str, object] = {
            "question": question,
            "answer": answer,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "citations": citations,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as history_file:
            history_file.write(json.dumps(turn, ensure_ascii=False, separators=(",", ":")) + "\n")
        return turn


def create_runtime(settings: Settings) -> Runtime:
    storage = Storage(settings)
    storage.initialize()
    try:
        feishu = FeishuClient()
        ollama = OllamaClient()
        vector_store = LanceDBVectorStore.open(settings.database_path.parent / "lancedb")
        return Runtime(
            settings=settings,
            storage=storage,
            answer_service=AnswerService(Retriever(storage, ollama, vector_store), ollama),
            document_synchronizer=DocumentSynchronizer(settings, storage, feishu),
            chat_synchronizer=ChatSynchronizer(settings, storage, feishu),
            indexer=Indexer(storage, ollama, vector_store),
        )
    except Exception:
        storage.close()
        raise


def create_app(
    settings: Settings,
    *,
    answer_service: Any | None = None,
    document_synchronizer: Any | None = None,
    chat_synchronizer: Any | None = None,
    storage: Storage | None = None,
    indexer: Indexer | None = None,
    schedule_reconfigurer: ScheduleReconfigurer | None = None,
    model_status_provider: ModelStatusProvider | None = None,
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
    schedule_reconfigurer = schedule_reconfigurer or _reconfigure_existing_schedule
    model_status_provider = model_status_provider or _local_model_availability
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_storage:
            runtime.storage.close()

    app = FastAPI(title="WikiLocal", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/api/health")
    def health() -> dict[str, object]:
        availability = _complete_model_availability(model_status_provider())
        return {
            "status": "ok",
            "host": "127.0.0.1",
            "models": {
                role: {"name": model, "available": availability[model]}
                for role, model in MODEL_NAMES.items()
            },
            "model_available": all(availability.values()),
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
            candidate.validate()
        except SettingsError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if candidate.daily_time != runtime.settings.daily_time:
            try:
                schedule_reconfigurer(candidate)
            except (RuntimeError, ValueError) as error:
                raise HTTPException(status_code=503, detail="Unable to update the existing daily task.") from error
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
            return runtime.synchronize(kind, honor_enabled=False)  # type: ignore[arg-type]
        except (ModelUnavailableError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=_sync_error_message(error)) from error

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
        citations = [_evidence_dict(item) for item in result.citations]
        runtime.conversation_history.append(question, result.text, citations)
        return {"text": result.text, "citations": citations}

    @app.post("/api/answer/stream")
    def stream_answer(payload: AnswerRequest) -> StreamingResponse:
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="question must not be empty")
        try:
            chunks, evidence = _streaming_answer(runtime.answer_service, question)
        except ModelUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        citations = [_evidence_dict(item) for item in evidence]

        def stream() -> Any:
            parts: list[str] = []
            try:
                for chunk in chunks:
                    parts.append(chunk)
                    yield _ndjson_event({"type": "delta", "text": chunk})
            except ModelUnavailableError as error:
                yield _ndjson_event({"type": "error", "detail": str(error)})
                return
            text = "".join(parts)
            runtime.conversation_history.append(question, text, citations)
            yield _ndjson_event({"type": "answer", "text": text, "citations": citations})

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.get("/api/conversations")
    def conversations() -> dict[str, object]:
        return {"turns": runtime.conversation_history.load()}

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
        @app.get("/assets/app.js", include_in_schema=False)
        def web_script() -> FileResponse:
            return FileResponse(
                web_directory / "app.js",
                media_type="application/javascript",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/assets/styles.css", include_in_schema=False)
        def web_styles() -> FileResponse:
            return FileResponse(web_directory / "styles.css", media_type="text/css")

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


def _failed_sync_status(error: Exception) -> dict[str, int | str | None]:
    return {
        "created": 0,
        "changed": 0,
        "skipped": 0,
        "failed": 1,
        "error": _sync_error_message(error),
    }


def _failed_result_status(result: SyncResult | Any, error: Exception) -> dict[str, int | str | None]:
    status = _sync_result_dict(result)
    status["failed"] = max(1, int(result.failed))
    status["error"] = _sync_error_message(error)
    return status


def _sync_error_message(error: Exception) -> str:
    return f"Synchronization failed ({type(error).__name__})."


def _snapshot_sync_state(storage: Storage, mirror_directory: Path) -> _SyncSnapshot:
    mirrors = {
        path.relative_to(mirror_directory): path.read_bytes()
        for path in mirror_directory.rglob("*")
        if path.is_file()
    } if mirror_directory.is_dir() else {}
    return _SyncSnapshot(storage.snapshot_sync_state(), mirrors)


def _restore_sync_state(storage: Storage, mirror_directory: Path, snapshot: _SyncSnapshot) -> None:
    storage.restore_sync_state(snapshot.storage)
    if mirror_directory.is_dir():
        for path in sorted(mirror_directory.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    for relative_path, content in snapshot.mirrors.items():
        mirror = mirror_directory / relative_path
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_bytes(content)


def _reconfigure_existing_schedule(settings: Settings) -> bool:
    command = f'"{sys.executable}" -m wikilocal.cli sync --all'
    return reconfigure_daily_task_if_installed(settings, command)


def _local_model_availability() -> Mapping[str, bool]:
    try:
        return OllamaClient().model_availability(tuple(MODEL_NAMES.values()))
    except ModelUnavailableError:
        return {model: False for model in MODEL_NAMES.values()}


def _complete_model_availability(availability: Mapping[str, bool]) -> dict[str, bool]:
    return {model: availability.get(model) is True for model in MODEL_NAMES.values()}


def _evidence_dict(evidence: Any) -> dict[str, object]:
    return {
        "chunk_id": evidence.chunk_id,
        "source_key": evidence.source_key,
        "title": evidence.title,
        "text_content": evidence.text_content,
        "metadata": dict(evidence.metadata),
    }


def _streaming_answer(answer_service: Any, question: str) -> tuple[Any, tuple[Any, ...]]:
    stream_answer = getattr(answer_service, "stream_answer", None)
    if callable(stream_answer):
        return stream_answer(question)
    answer = answer_service.answer(question)
    return iter((answer.text,)), answer.citations


def _ndjson_event(event: Mapping[str, object]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def _valid_conversation_turn(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("question"), str)
        and isinstance(value.get("answer"), str)
        and isinstance(value.get("timestamp"), str)
        and isinstance(value.get("citations"), list)
    )


def _source_dict(source: SourceRecord) -> dict[str, object]:
    return {
        "source_key": source.source_key,
        "source_type": source.source_type,
        "title": source.title,
        "metadata": dict(source.metadata),
        "excerpt": source.text_content[:400],
    }
