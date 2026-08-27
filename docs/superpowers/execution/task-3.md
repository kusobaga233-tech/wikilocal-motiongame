# Task 3 Execution Record

## Scope

Implemented the document and chat synchronizers only. The synchronizers use
the existing read-only `FeishuClient` surface and no Feishu write command was
added or executed. All tests use injected fakes; no real full synchronization
was run.

## RED Command

From `D:\\wikilocal\\app`, before creating the synchronizer modules, run:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_sync_documents.py tests\\test_sync_chats.py -v
```

Captured result:

```text
E   ModuleNotFoundError: No module named 'wikilocal.sync_documents'
E   ModuleNotFoundError: No module named 'wikilocal.sync_chats'
=========================== 2 errors in 0.20s ===========================
```

The tests failed because the Task 3 production modules did not yet exist.

## Additional RED Regression

Before correcting chat checkpoint semantics, the following test was added and
run from `D:\\wikilocal\\app`:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_sync_chats.py -v
```

Captured failure:

```text
E AssertionError: {'message_id': 'm2', 'sent_at': '2026-08-26T02:00:00Z'}
E != {'message_id': 'm1', 'sent_at': '2026-08-26T01:00:00Z'}
```

This proved a thread reply was incorrectly moving the main-chat pagination
cursor. The implementation was changed so only committed main-chat messages
advance `chat:<chat_id>`.

## GREEN Evidence

After implementation and regression correction, the Task 3 suite was run from
`D:\\wikilocal\\app`:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_sync_documents.py tests\\test_sync_chats.py -v
```

Captured result:

```text
============================== 8 passed in 0.14s ==============================
```

The full application suite was then run from the same directory:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest -v
```

Captured result:

```text
=================== 40 passed, 65 subtests passed in 0.30s ====================
```

## Review Fix RED

Before changing the Task 3 synchronizers, the known-thread and Markdown
provenance regressions were added and run from `D:\\wikilocal\\app`:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_sync_chats.py tests\\test_sync_documents.py -v
```

Captured result:

```text
FAILED test_second_sync_reads_new_reply_from_known_thread_without_new_root_message
AssertionError: {'message_id': 'm1', 'sent_at': '2026-08-26T01:00:00Z'}
!= {'message_id': 'm1', 'sent_at': '2026-08-26T01:00:00Z', 'thread_ids': ['omt-1']}

FAILED test_sync_writes_normalized_markdown_mirror_and_document_metadata
AssertionError: '# Release plan\n\nFirst line\nSecond line\n' did not contain required front matter

4 failed, 5 passed
```

This demonstrated that old thread roots were not persisted for later reply
scans and that document mirrors lacked the required provenance fields.

## Review Fix GREEN

The chat checkpoint now stores a sorted `thread_ids` list alongside its main
message cursor. After each completed main-message scan, all known threads are
rescanned before that combined checkpoint is committed. Existing message keys
remain idempotent, so an old thread message is counted as skipped rather than
created again. Markdown mirrors now contain UTF-8 front matter for source key,
URL, Wiki path, source update time, and the SHA-256 hash of normalized body
content.

Targeted verification:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_sync_chats.py tests\\test_sync_documents.py -v
```

Captured result:

```text
9 passed in 0.19s
```

Fresh full-suite pre-commit verification:

```powershell
.\\.venv\\Scripts\\python.exe -m pytest -v
```

Captured result:

```text
41 passed, 65 subtests passed in 0.30s
```
