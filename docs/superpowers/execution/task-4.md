# Task 4 Execution Record

## Scope

Implemented local chunk indexing, optional vector indexing, Ollama client integration,
hybrid retrieval, and citation-backed local answers. No model was downloaded and no
external model endpoint is used.

## RED

From `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_indexing.py tests\test_retrieval.py -v
```

Before implementation, collection failed as expected because
`wikilocal.indexing` and `wikilocal.ollama` did not exist.

## GREEN

From `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest -v
55 passed, 65 subtests passed
```

Coverage includes deterministic 800/120 chunks, FTS stale replacement, injected vector
storage and a lazy local LanceDB adapter, hybrid candidate deduplication and top-20 reranking, eight-result maximum,
local model transport failures, Qwen3 `num_ctx=8192`, and citation source identity.

## Review Fixes (2026-08-27)

### RED

From `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_retrieval.py tests\test_storage.py tests\test_indexing.py tests\test_ollama.py -v
```

The added regression cases failed as expected before implementation: inactive source
chunks were returned by FTS and vector retrieval, embedding failure erased existing FTS
chunks, and non-loopback Ollama URLs were accepted. The hybrid test was then isolated
with twenty FTS-only sources and eight vector-only sources; it failed because all vector
candidates were truncated before reranking. A subsequent local-only URL edge case for
`http://localhost` also failed until the validator stopped requiring an explicit port.

### GREEN

The implementation uses a deterministic 12 FTS plus up to 8 unique vector candidate
quota before the shared 20-candidate reranking cap, joins FTS chunks to active sources,
filters inactive vector sources, validates only HTTP loopback Ollama URLs, and generates
embeddings before replacing FTS or vector rows.

From `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest -v
61 passed, 74 subtests passed
```
