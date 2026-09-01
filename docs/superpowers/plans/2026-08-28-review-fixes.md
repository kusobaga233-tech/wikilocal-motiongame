# Review Fixes Implementation Plan

> **For agentic workers:** Execute test-first, keeping all Feishu access read-only and local test fixtures hermetic.

**Goal:** Correct Feishu thread and Drive traversal behavior while preventing synchronized source state from advancing unless the local index succeeds.

**Architecture:** The Feishu adapter exposes only allow-listed read commands. Chat synchronization fully rescans known threads because the CLI does not support a thread `--start` filter. Runtime-level snapshots restore SQLite source/checkpoint/FTS data and Markdown mirrors when indexing fails or a synchronizer reports failed work.

**Tech Stack:** Python 3.12, SQLite FTS5, pytest, Ruff, lark-cli read-only adapter.

## Global Constraints

- Do not execute live Feishu or Ollama calls in tests.
- Preserve the local-only and read-only Feishu boundaries.
- Do not create a git commit.
- Write files as UTF-8.

### Task 1: Read-only Feishu Adapter

- [ ] Add failing unit tests for absent thread `--start`, `next_page_token`, and folder-scoped Drive lists.
- [ ] Run the focused Feishu tests and observe failure.
- [ ] Remove thread `start`, add validated `--folder-token`, and normalize Drive continuation tokens.
- [ ] Re-run focused Feishu tests.

### Task 2: Synchronizer Coverage

- [ ] Add failing chat and document tests for full known-thread rescans and nested Drive folders.
- [ ] Run the focused synchronizer tests and observe failure.
- [ ] Fully rescan known threads through idempotent upserts and recursively enumerate Drive folders.
- [ ] Re-run focused synchronizer tests.

### Task 3: Runtime Atomicity And Status

- [ ] Add failing runtime tests for mirror/source/checkpoint restoration and a sanitized error from `SyncResult.failed`.
- [ ] Run the focused service tests and observe failure.
- [ ] Snapshot and restore all local sync state surrounding synchronization and indexing, then record generic statuses.
- [ ] Re-run focused service tests.

### Task 4: Documentation And Verification

- [ ] Document the full thread rescan and Drive traversal behavior.
- [ ] Run focused tests, full pytest, and Ruff.
