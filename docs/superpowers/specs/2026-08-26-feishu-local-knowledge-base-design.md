# Feishu Local Knowledge Base Design

## Goal

Build a Windows-local knowledge base in `D:\wikilocal` that synchronizes all Feishu Wiki/document text and all chats visible to the authorized user, indexes the content locally, and provides cited answers through a local Qwen3 4B model.

## Constraints

- Store application code, model files, source mirrors, indexes, configuration, and logs under `D:\wikilocal`.
- Process only document and message text in the first release. Preserve attachment metadata but do not download, OCR, transcribe, or index attachments.
- Use the authorized Feishu user identity. Sync only resources that identity can read.
- Do not create, update, send, delete, or otherwise mutate Feishu resources.
- Run all retrieval and generation locally. Do not send source content to external LLM services.
- Bind the web server to `127.0.0.1` by default.
- Use UTF-8 for all generated text files.

## Architecture

```text
Feishu Wiki / Docs / group chats / direct chats / thread replies
  -> Feishu sync adapters
  -> SQLite source store and Markdown mirrors
  -> text chunker
  -> LanceDB vector index + SQLite FTS5 index
  -> hybrid retrieval and reranking
  -> Ollama Qwen3 4B
  -> local web UI with citations
```

The implementation is a native Windows Python application. It uses FastAPI for the local HTTP API and web application, SQLite for transactional source data, FTS5 for keyword search, and LanceDB for vector search. Ollama runs the answer model locally. Windows Task Scheduler invokes the application's sync command at the configured time.

## Directory Layout

```text
D:\wikilocal\
  app\                 application source, tests, and runtime scripts
  data\
    documents\         Markdown mirrors for documents and chat records
    index\             SQLite database and LanceDB data
    logs\              structured sync and scheduler logs
  models\              Ollama model storage
  config\settings.json persisted user settings
  docs\                design and implementation documents
```

## Source Synchronization

### Documents

The document adapter enumerates Wiki spaces and nodes accessible to the user, resolves readable document content, normalizes it to UTF-8 Markdown/text, and persists the Feishu identity, title, Wiki path, URL, content hash, source update time, and local sync time.

The initial run performs a full traversal. Incremental runs compare source revision or update timestamps to the last successful cursor and re-index only changed documents. Deleted or inaccessible sources are marked inactive locally and excluded from retrieval without destroying their audit record.

### Chats

The chat adapter lists all visible group and P2P chats, including muted chats. It retrieves historical messages and thread replies by paginating each conversation. Each message stores the chat identifier and name, sender identity when available, timestamp, message ID, parent/thread relation, normalized text, source URL when available, and local sync time.

The initial synchronization imports all available text history. Incremental synchronization resumes separately per conversation using its stored latest source timestamp and message ID. Idempotent message upserts prevent duplicate records at pagination boundaries. Edited or recalled messages are refreshed when Feishu exposes their updated state.

## Indexing and Retrieval

Each active source is split into bounded, overlap-aware chunks. A chunk never spans source boundaries. The system writes chunk text and metadata to SQLite FTS5 and chunk embeddings to LanceDB.

For each user question, the API retrieves candidates from FTS5 and LanceDB, merges and deduplicates them, reranks the top candidates, and gives Qwen3 only the selected source passages. The response includes citations mapped to the original document or chat message. The UI renders the source title/chat, sender where applicable, timestamp, and source link. When evidence is insufficient, the model is instructed to state that no source supports the answer.

## Local Models

- Answer generation: `qwen3:4b` through Ollama.
- Embeddings: `bge-m3`.
- Reranking: `bge-reranker-v2-m3`.
- Default answer context window: 8192 tokens.

The host has an RTX 5060 with 8 GB VRAM and 16 GB RAM. The service uses Qwen3 4B as the default to leave runtime headroom. Indexing runs in bounded batches and does not execute concurrently with a generation request unless resource limits permit it.

## Scheduling and Operations

The default schedule is daily at `02:00`, implemented as a Windows Task Scheduler task that calls a CLI sync command. The task and application use `config\settings.json` as the single source of truth for the time and enabled source types.

The UI provides:

- Enable/disable document and chat sync independently.
- Configure the daily schedule time.
- Configure the initial chat-history start date; the default is unbounded history.
- Trigger document-only, chat-only, or full synchronization.
- View current/last sync status, item counts, errors, and logs.
- View model availability and local index size.

Syncs are transactional at the item level. A failed run records the error and checkpoint, preserves the last valid index, and resumes from the last successful cursor on a later run. Logs must not contain OAuth tokens or application secrets.

## Web Experience

The default page is the question-and-answer workspace: question input, streaming answer, rendered citations, expandable evidence text, and local conversation history. Secondary views provide source search/sync status and configuration. The interface is intended for repeated work: compact, scan-friendly, and without a marketing landing page.

## Acceptance Criteria

1. Ollama can run Qwen3 4B with model files under `D:\wikilocal\models`.
2. A full sync stores all text content accessible from Feishu documents/Wiki and group/P2P chats without duplicate source records.
3. A daily incremental task runs at the configured time, and a manual run can be initiated from the UI.
4. Queries return local answers with traceable document/message citations.
5. A source outside the authorized user's Feishu visibility is never included in local results.
6. The first release leaves non-text attachments unindexed.
7. Automated tests cover cursor advancement, idempotent upserts, chunk/source metadata preservation, hybrid retrieval, citation mapping, settings validation, and scheduler command generation.
