# Task 6 Execution Record

## Scope

Completed the hermetic document-sync, indexing, retrieval, and cited-answer test, plus the root operating documentation. No real Feishu request, Ollama request, cloud model request, Ollama installation, model pull, scheduler registration, or server startup was performed for this task.

## RED

From `D:\wikilocal\app`:

```powershell
python -m pytest tests\test_end_to_end.py -v
```

Result: `1 failed`. The partial fixture supplied only `embed`; production retrieval correctly also required `rerank`, so it failed with `AttributeError: 'FakeOllama' object has no attribute 'rerank'`. The original test also registered SQLite cleanup after the temporary directory cleanup, which can leave the database locked on Windows.

The follow-up assertion change also produced the intended `1 failed`: the test referenced `FakeVectors.searched` before the fake recorded search calls (`AttributeError`).

## GREEN

The fixture now provides deterministic fake embedding, reranking, and generation methods, and a fake in-memory vector store. It synchronizes the fake Feishu `document:release-plan`, indexes it, retrieves it, and answers through the fake local model protocol. The assertion requires the complete returned citation source-key list to equal `['document:release-plan']`; it also verifies the generation model and context configuration are `qwen3:4b` and `8192`.

The test also requires at least one embedding call, vector search call, and reranking call. `FakeVectors` records each `(embedding, limit)` passed to `search`, preserving the hermetic fake while proving the vector retrieval path executed.

SQLite is closed in a `finally` block inside the temporary-directory scope, before Windows removes the database file. This test has no network-capable production clients in its dependency graph.

## Documentation

`D:\wikilocal\README.md` documents the local-only boundary, authorized read-only Feishu scopes, prerequisites, setup, `D:\wikilocal\models` model storage, startup, manual synchronization, daily scheduling configuration, recovery, and directory layout.

## Verification

From `D:\wikilocal\app`, targeted verification passed:

```powershell
python -m pytest tests\test_end_to_end.py -v
# 1 passed

python -m pytest tests\test_sync_documents.py tests\test_indexing.py tests\test_retrieval.py tests\test_end_to_end.py -v
# 16 passed
```

After adding the collaborator-path assertions, the focused command was rerun and passed:

```powershell
python -m pytest tests\test_end_to_end.py -v
# 1 passed
```

Full verification used the project Python 3.12 virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
# 91 passed, 74 subtests passed
```

The requested Ruff command was attempted:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
```

It could not run because `ruff` is not declared or installed in the project virtual environment (`No module named ruff`). An attempted installation stalled while downloading the wheel and was stopped; no application files were changed by that attempt.

## Review-Finding Fixes (2026-08-28)

### RED

From `D:\wikilocal\app`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_service.py::test_create_runtime_shares_one_local_lancedb_store_between_indexing_and_retrieval tests\test_service.py::test_setup_script_runs_project_permission_preflight_before_model_pulls tests\test_end_to_end.py -v
```

Result: `2 failed, 1 passed`. The runtime assembly test failed because `create_runtime()` did not open a LanceDB store (`opened_directories == []`). The setup-script test failed because `setup.ps1` did not contain `FeishuClient().permission_preflight()`. The strengthened hermetic end-to-end test passed on its first run because the existing synchronization result, deterministic fake answer, and generated prompt already met its new assertions.

### GREEN

After wiring one local store at `settings.database_path.parent / "lancedb"` into both `Indexer` and `Retriever`, and after adding the project-Python `FeishuClient.permission_preflight()` gate after application installation and before model pulls:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_service.py::test_create_runtime_shares_one_local_lancedb_store_between_indexing_and_retrieval tests\test_service.py::test_setup_script_runs_project_permission_preflight_before_model_pulls tests\test_end_to_end.py -v
# 3 passed

.\.venv\Scripts\python.exe -m pytest -v
# 93 passed, 74 subtests passed
```

No Feishu command, Ollama process, scheduler task, or README update was performed for these review-finding fixes.

## Final Code-Quality Fixes (2026-08-28)

### RED

Focused regressions failed before the implementation changes:

- `test_empty_source_retires_fts_and_vector_evidence_without_embedding_empty_text` failed because `Indexer` called `embed([])`.
- `test_synchronize_indexes_active_empty_sources_to_retire_old_evidence` failed because `Runtime.synchronize()` skipped active blank sources.
- `test_vector_add_failure_preserves_old_fts_and_vector_evidence` failed because FTS was replaced before the vector add completed.
- `test_search_excludes_stale_vector_rows_not_present_in_current_fts` failed because retrieval returned a stale vector payload.
- `test_create_runtime_closes_initialized_storage_when_lancedb_open_fails` failed because initialized storage was left open.

### GREEN And Verification

The indexer now stages only new vector rows, atomically replaces FTS, then deletes obsolete vector rows on a best-effort basis. Vector search results are rebuilt from current active FTS chunks, so staged, stale, and failed-cleanup vector rows cannot become evidence. Empty active sources replace FTS with no chunks and do not request an embedding.

From `D:\wikilocal\app`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_indexing.py tests\test_retrieval.py tests\test_service.py -v
# 33 passed

.\.venv\Scripts\python.exe -m pytest -v
# 100 passed, 74 subtests passed

.\.venv\Scripts\python.exe -m ruff check src tests
# 35 existing lint findings; command remains non-zero
```

Ruff `0.16.5` was installed into the existing project virtual environment only so the requested command could run. `ruff>=0.12` is declared in both the `test` and `dev` optional dependency groups for reproducible fresh environments. The remaining Ruff findings are in pre-existing unrelated files and were not hidden with global ignores.

The PowerShell parser was also run over `app\scripts\*.ps1` and reported `0 errors`.

No Feishu sync, scheduler registration, Ollama installation, model pull, or commit was performed. The root README required no behavior correction. `wiki-space-auth.png` was preserved; `.gitignore` now ignores only `wiki-space-auth*.png` screenshots.

## Lint Remediation Verification (2026-08-28)

Ruff safe autofixes resolved import ordering, redundant annotations, unused imports, nested test context managers, and UTF-8 byte literal findings. The remaining findings were resolved with behavior-preserving error-type, SQLite error-boundary, and time-validation changes. Feishu synchronization retains its failed-count handling for Feishu client, payload, filesystem, and SQLite operational errors; no broad lint suppressions or per-file ignores were added.

From `D:\wikilocal\app`:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
# All checks passed!

.\.venv\Scripts\python.exe -m pytest -v
# 100 passed, 74 subtests passed
```

No real Feishu sync, Ollama request, scheduler action, model pull, or commit was performed for this remediation.
