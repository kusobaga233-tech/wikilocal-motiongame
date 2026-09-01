# WikiLocal

WikiLocal is a Windows-local knowledge base for Feishu Wiki/documents and chats visible to an authorized user. It synchronizes text into local storage, searches it with SQLite FTS5 and LanceDB, and produces cited answers through local Ollama models.

## Architecture and Local-Only Security

```text
Authorized Feishu user (read-only lark-cli calls)
  -> SQLite sources + UTF-8 Markdown mirrors
  -> SQLite FTS5 + local LanceDB vectors
  -> local Ollama (bge-m3, bge-reranker-v2-m3, qwen3:4b)
  -> FastAPI web UI bound to 127.0.0.1
```

All application data, source mirrors, indexes, configuration, models, and generated task XML are kept below `D:\wikilocal`. The service only accepts `127.0.0.1` or `localhost` as bind hosts. Source passages are sent only to the local Ollama endpoint; they are not sent to a cloud model service.

The Feishu adapter runs commands as the authorized user and permits only reads. It never creates, edits, sends, or deletes Feishu resources. It processes document and message text only; attachments are not downloaded, OCRed, transcribed, or indexed. Runtime errors are sanitized so OAuth values and command output are not exposed.

The user authorization must include these read-only scopes:

- `im:chat:read`
- `im:message:readonly`
- `docx:document:readonly`
- `drive:drive:readonly`
- `wiki:space:retrieve`

Run `lark-cli auth status --verify` before setup. The setup script checks this authorization and exits if it is not valid.

## Prerequisites and Setup

Install Python 3.12, Ollama, and `lark-cli`, then authorize `lark-cli` as the Feishu user whose visible sources should be indexed. WikiLocal does not install Ollama automatically.

From `D:\wikilocal`, run:

```powershell
.\app\scripts\setup.ps1
```

The script creates `app\.venv`, installs the Python application with test and vector dependencies, sets `OLLAMA_MODELS` for the current process and user environment, and pulls the local models:

- `qwen3:4b` for answers
- `bge-m3` for embeddings
- `bge-reranker-v2-m3` for reranking

Ollama model files are stored under `D:\wikilocal\models`; use `OLLAMA_MODELS=D:\wikilocal\models` when starting Ollama or running commands outside the scripts.

## Startup and Manual Sync

Start the local web application with:

```powershell
.\app\scripts\start.ps1
```

The script starts a local Ollama server when one is not already responding, starts WikiLocal on `http://127.0.0.1:8765`, waits for `/api/health`, then opens the browser.

Run a manual full synchronization from the app directory with:

```powershell
cd D:\wikilocal\app
.\.venv\Scripts\python.exe -m wikilocal.cli --root D:\wikilocal sync --all
```

Use `--documents` or `--chats` instead of `--all` to sync one source type. The web UI also provides manual source controls. The first document sync writes Markdown mirrors and source metadata, then indexes active sources locally. Drive discovery follows every accessible nested folder with the CLI's `next_page_token`; only text documents are read and indexed.

Known chat threads are fully rescanned on each chat sync because the supported read-only `lark-cli` thread command has no safe `--start` filter. Message source keys are idempotent, so already stored replies are skipped while older edits and newly visible replies are refreshed.

## Daily Scheduling

The default schedule is `02:00` daily. The canonical settings live in `D:\wikilocal\config\settings.json`. Change the time and enabled source types in the web UI, or update the JSON with a valid `HH:MM` time and restart the application.

To create or update the `WikiLocalDailySync` Task Scheduler entry intentionally, run:

```powershell
cd D:\wikilocal\app
.\.venv\Scripts\python.exe -m wikilocal.cli --root D:\wikilocal schedule
```

This writes `D:\wikilocal\data\logs\WikiLocalDailySync.xml` as UTF-8 and invokes `schtasks`. Settings updates only reconfigure a task that already exists; they do not create a new scheduled task implicitly.

## Logs and Recovery

Manual sync writes a JSON result to the console. The UI exposes the latest sync state and sanitized errors at `/api/sync/status`. `D:\wikilocal\data\logs` contains scheduler artifacts, including the generated XML when scheduling is configured.

If authorization, Feishu access, Ollama, or indexing fails, correct the local dependency or permission and rerun the same sync command. Each runtime sync snapshots local source records, checkpoints, FTS rows, and Markdown mirrors before synchronization. A synchronizer failure or indexing failure restores that snapshot, so completed source content and checkpoints do not advance without a corresponding local index update. Inaccessible documents are excluded only after a successful complete scan. Do not delete `data\index` unless a full reindex is intended.

## Layout

```text
D:\wikilocal\
  app\                    Python package, tests, web UI, and scripts
  config\settings.json    Persistent source and schedule settings
  data\documents\         UTF-8 document Markdown mirrors
  data\index\             SQLite source/FTS store and LanceDB index
  data\logs\              Scheduler XML and local operational artifacts
  models\                 Local Ollama model storage
  docs\                   Design, plans, and execution records
```
