# WikiLocal Spec Compliance Hardening Plan

> **For agentic workers:** Use subagent-driven development with test-first changes and two independent review gates.

**Goal:** Close the data-completeness, incremental-sync, durability, and Q&A interaction gaps identified against the approved design before importing real Feishu content.

**Global constraints:** Remain local-only, use read-only authorized Feishu operations, preserve UTF-8 output, avoid OAuth values in logs, and bind the web service only to loopback.

### Task 7: Document Coverage And Incremental Sync

**Files:** `app/src/wikilocal/feishu.py`, `app/src/wikilocal/sync_documents.py`, `app/tests/test_feishu.py`, `app/tests/test_sync_documents.py`.

- Add a read-only adapter for documents outside Wiki spaces, including the authorized user's personal library when the CLI exposes it.
- Use source update revisions/times plus successful checkpoints to skip unchanged document reads and reindex only changed records.
- Prove full initial import, changed-document update, unchanged-document skip, and inaccessible-source retirement with fakes.

### Task 8: Chat Revisions And Incremental Thread Sync

**Files:** `app/src/wikilocal/sync_chats.py`, `app/tests/test_sync_chats.py`.

- Persist message revision/deletion state and deactivate recalled content.
- Use timestamp/message-id checkpoints for root and thread updates without re-scanning historical thread pages after initial discovery.
- Prove edits, recalls, pagination boundaries, and legacy checkpoint migration.

### Task 9: Durable Sync Operations

**Files:** `app/src/wikilocal/storage.py`, `app/src/wikilocal/service.py`, `app/tests/test_storage.py`, `app/tests/test_service.py`.

- Persist structured, sanitized sync outcomes and errors below `data/logs` and expose the latest persisted status after restart.
- Preserve last valid source/index state on failed runs and test restart/recovery behavior.

### Task 10: Streaming Q&A And Local History

**Files:** `app/src/wikilocal/ollama.py`, `app/src/wikilocal/service.py`, `app/web/app.js`, `app/web/index.html`, `app/web/styles.css`, relevant tests.

- Stream locally generated answer text to the browser, retain an explicit local conversation history, and render citation sender, timestamp, link, and expandable evidence.
- Prove no source text leaves loopback and the non-stream fallback remains cited.

### Verification

- Run `python -m pytest -v`, `python -m ruff check src tests`, PowerShell parser checks, browser smoke tests, then specification and code-quality reviews.
- Do not run first real Feishu synchronization, create a scheduled task, or pull model files until all tasks pass review.
