# Feishu Local Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a local Windows knowledge base for authorized Feishu document and chat text, with Qwen3 4B cited answers.

**Architecture:** FastAPI persists sources in SQLite and Markdown, maintains FTS5 and LanceDB indexes, and calls local Ollama models. A local web UI consumes the API, while Windows Task Scheduler runs the sync CLI daily.

**Tech Stack:** Python 3.12, FastAPI, SQLite FTS5, LanceDB, Ollama, lark-cli, pytest, Ruff, vanilla HTML/CSS/JavaScript, Windows Task Scheduler.

## Global Constraints

- Store all generated code, models, indexes, documents, logs, and settings below `D:\wikilocal` using UTF-8 text.
- Process only document/message text, use only the authorized user's visible sources, and invoke no Feishu write API.
- Bind to `127.0.0.1`; do not send source text to an external model service.
- Use Qwen3 4B with `num_ctx=8192`; the default schedule is `02:00` daily.

## Task 1: Repository, packages, and settings

**Files:** Create `D:\wikilocal\app\pyproject.toml`, `src\wikilocal\__init__.py`, `src\wikilocal\settings.py`, `tests\test_settings.py`, `D:\wikilocal\config\settings.json`, and `D:\wikilocal\.gitignore`.

**Interfaces:** `Settings.load(root: Path) -> Settings`; `Settings.save() -> None`; `Settings.ensure_directories() -> None`; `Settings.database_path -> Path`.

- [ ] Write `test_defaults_create_expected_local_directories`: load settings from a temporary root, call `ensure_directories`, assert `daily_time == "02:00"`, database is `data/index/wikilocal.sqlite3`, and `data/documents` exists.
- [ ] Run `cd D:\wikilocal\app; python -m pytest tests\test_settings.py -v`; expect import failure because `Settings` does not exist.
- [ ] Implement a dataclass with `root`, `daily_time`, `documents_enabled`, `chats_enabled`, and `chat_history_start`; load JSON from `config/settings.json`; create `data/documents`, `data/index`, `data/logs`, `models`, and `config` below root.
- [ ] Run the settings test; expect PASS.
- [ ] Initialize and publish: run `git -C D:\wikilocal init -b main`; set `origin` to `https://github.com/kusobaga233-tech/wikilocal-motiongame.git`; commit the bootstrap; push `main`.

## Task 2: Local storage and read-only Feishu client

**Files:** Create `src\wikilocal\storage.py`, `src\wikilocal\feishu.py`, `tests\test_storage.py`, and `tests\test_feishu.py` under `D:\wikilocal\app`.

**Interfaces:** `SourceRecord(source_key, source_type, title, text_content, metadata, active)`; `Storage.upsert_source`, `Storage.get_checkpoint`, `Storage.set_checkpoint`; `FeishuClient.list_chats`, `list_messages`, `list_wiki_spaces`, `list_wiki_nodes`, `read_document`.

- [ ] Write a storage test that upserts `document:d1` twice and asserts one record remains with the second body. Write a client test with an injected command runner and assert `list_chats` invokes `im +chat-list --types p2p --types group --as user`.
- [ ] Run `python -m pytest tests\test_storage.py tests\test_feishu.py -v`; expect import failures.
- [ ] Create tables `sources(source_key PRIMARY KEY, source_type, title, text_content, metadata_json, active, content_hash, source_updated_at, synced_at)`, `checkpoints(checkpoint_key PRIMARY KEY, cursor_json, updated_at)`, and FTS5 table `chunks_fts(chunk_id UNINDEXED, text_content, title, source_key UNINDEXED)`. Use a parameterized SQLite UPSERT.
- [ ] Implement Feishu execution through `lark-cli`, requiring JSON success and allowing only read commands: chat list/messages/thread messages, Wiki listing, and document reads. Errors never log OAuth values.
- [ ] Run the two tests, expect PASS; commit `feat: add source storage and Feishu reader`.

## Task 3: Document and chat synchronizers

**Files:** Create `src\wikilocal\sync_documents.py`, `src\wikilocal\sync_chats.py`, `tests\test_sync_documents.py`, and `tests\test_sync_chats.py`.

**Interfaces:** `DocumentSynchronizer.sync() -> SyncResult`; `ChatSynchronizer.sync() -> SyncResult`; `SyncResult(created, changed, skipped, failed)`; checkpoint keys `documents` and `chat:<chat_id>`.

- [ ] Write document test using one fake document and assert that `data/documents/document-d1.md` contains its title/body. Write chat test with duplicate `m1` at a pagination boundary followed by `m2`; assert exactly two records and checkpoint message ID `m2`.
- [ ] Run `python -m pytest tests\test_sync_documents.py tests\test_sync_chats.py -v`; expect import failures.
- [ ] Implement document/Wiki traversal and Markdown mirrors with source keys `document:<token>`, title, URL, Wiki path, update time, and content hash. Advance the global document checkpoint only after a successful scan; mark missing sources inactive only after a full successful scan.
- [ ] Implement P2P/group enumeration including muted chats, full initial text history, per-chat incremental cursor, thread reply reading, message keys `message:<id>`, and message metadata `{chat_id, chat_name, sender, sent_at, thread_id, url}`. Skip non-text bodies while preserving their metadata.
- [ ] Run synchronization tests, expect PASS; commit `feat: synchronize Feishu documents and chats`.

## Task 4: Indexing, local models, retrieval, and cited answers

**Files:** Create `src\wikilocal\indexing.py`, `src\wikilocal\ollama.py`, `src\wikilocal\retrieval.py`, `tests\test_indexing.py`, and `tests\test_retrieval.py`.

**Interfaces:** `chunk_text(text, size=800, overlap=120) -> list[str]`; `Indexer.index_source(source_key) -> int`; `OllamaClient.embed`; `Retriever.search(question, limit=8) -> list[Evidence]`; `AnswerService.answer(question) -> Answer`.

- [ ] Write a chunk test for 1000 characters with 800/120 boundaries and a service test asserting an answer citation keeps `source_key == "document:d1"`.
- [ ] Run `python -m pytest tests\test_indexing.py tests\test_retrieval.py -v`; expect import failures.
- [ ] Implement deterministic IDs `source_key:ordinal:sha256prefix`, FTS rows, LanceDB vectors from local `bge-m3`, stale-chunk deletion, keyword/vector merge, duplicate removal, top-20 rerank through local `bge-reranker-v2-m3`, and eight evidence items maximum.
- [ ] Prompt Qwen with the selected evidence only: require numeric citations for factual claims and require an explicit insufficient-evidence answer when sources do not support the question. Call local `qwen3:4b` with `num_ctx=8192`.
- [ ] Run index/retrieval tests, expect PASS; commit `feat: add local retrieval and cited answers`.

## Task 5: API, web UI, setup scripts, and daily scheduler

**Files:** Create `src\wikilocal\service.py`, `src\wikilocal\scheduler.py`, `src\wikilocal\cli.py`, `web\index.html`, `web\app.js`, `web\styles.css`, `scripts\setup.ps1`, `scripts\start.ps1`, `tests\test_service.py`, and `tests\test_scheduler.py`.

**Interfaces:** `create_app(settings) -> FastAPI`; routes `GET /api/health`, `GET/PUT /api/settings`, `POST /api/sync/{kind}`, `GET /api/sync/status`, `POST /api/answer`, `GET /api/sources`; `build_task_xml(command, daily_time) -> str`.

- [ ] Write API test posting `{"question":"What changed?"}` to `/api/answer` and asserting a citation. Write task test asserting `build_task_xml("python -m wikilocal.cli sync --all", "02:00")` includes `T02:00:00`.
- [ ] Run `python -m pytest tests\test_service.py tests\test_scheduler.py -v`; expect import failures.
- [ ] Implement API validation and static-file serving. Build a compact Q&A-first UI with citations, source/sync view, settings view, model status, manual document/chat/all sync buttons, enable switches, configurable daily time, history start date, and error/log status.
- [ ] Implement commands `setup`, `sync`, `serve`, and `schedule`. The UTF-8 setup script checks `lark-cli auth status --verify`, sets `OLLAMA_MODELS=D:\wikilocal\models`, and pulls `qwen3:4b`, `bge-m3`, and `bge-reranker-v2-m3`. Scheduler writes UTF-8 task XML and runs `schtasks /Create /TN WikiLocalDailySync /XML <file> /F`.
- [ ] Run API/scheduler tests, expect PASS; commit `feat: add local web app and daily sync`.

## Task 6: End-to-end verification and final publication

**Files:** Create `D:\wikilocal\app\tests\test_end_to_end.py`; create `D:\wikilocal\README.md`.

**Interfaces:** Document `scripts\setup.ps1`, `python -m wikilocal.cli sync --all`, and `scripts\start.ps1`.

- [ ] Write an end-to-end fixture test that syncs fake Feishu data, indexes it with fake local embeddings, asks a question, and asserts citation `document:release-plan`.
- [ ] Run the test, expect FAIL before wiring the complete fixture.
- [ ] Implement the fixture and operating documentation covering local-only boundaries, source permissions, first sync, schedule changes, log paths, and failed-sync recovery.
- [ ] Run `cd D:\wikilocal\app; python -m pytest -v; python -m ruff check src tests`; expect all PASS.
- [ ] Start `python -m wikilocal.cli serve --host 127.0.0.1 --port 8765`, inspect the UI, then commit and push all remaining changes to `origin/main`.

## Plan Self-Review

- Coverage: Tasks 1-2 implement the local configuration/storage/permission boundary; Task 3 implements first and incremental source sync; Task 4 implements all local model and citation behavior; Task 5 implements UI and scheduling; Task 6 verifies and publishes.
- All required scopes, files, commands, and interfaces are explicitly named.
- Every later task consumes an interface defined by an earlier task.
