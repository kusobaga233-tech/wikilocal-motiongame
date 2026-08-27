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
