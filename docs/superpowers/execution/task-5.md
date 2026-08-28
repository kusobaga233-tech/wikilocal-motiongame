# Task 5 Execution Record

## Scope

Implemented the local FastAPI service, daily task XML generator, command-line interface,
static Q&A workspace, and Windows setup/start scripts. No Feishu synchronization, model pull,
or Windows Task Scheduler registration was executed during this task.

## RED

From `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_service.py tests\test_scheduler.py -v
```

Initial result: the scheduler tests failed with `ModuleNotFoundError: No module named
'wikilocal.scheduler'`; the API tests were blocked because FastAPI was not yet installed.
After declaring the FastAPI test dependencies, the root workspace test failed with `404` and the
CLI test failed with `ModuleNotFoundError: No module named 'wikilocal.cli'`.

## GREEN

From `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_service.py tests\test_scheduler.py -v
```

Result:

```text
12 passed
```

The tests use fake answer and synchronization services. They do not require Ollama, Feishu, a
scheduled task, or model downloads.

## Review Fixes

Follow-up RED tests were added before the fixes and run from `D:\wikilocal\app`:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_ollama.py::OllamaClientTests::test_model_availability_uses_only_local_tags_response tests\test_service.py tests\test_scheduler.py -v
```

The initial run failed because `OllamaClient.model_availability` and
`reconfigure_daily_task_if_installed` did not exist, `create_app` did not accept the injectable
model/scheduler checks, `--all` did not honor enabled settings, the CLI accepted non-loopback
hosts, and the web/setup assertions were absent.

GREEN verification after implementation:

```text
.\.venv\Scripts\python.exe -m pytest -v
86 passed, 74 subtests passed
```

The follow-up tests use fake synchronizers, scheduler runners, and Ollama transports. No Feishu
sync, Ollama installation/model download, or Task Scheduler task creation was executed.

## Final P1 Fixes

Additional tests were written first in `tests\\test_service.py` to require that manual
`POST /api/sync/all` runs both sources regardless of their enabled settings, failed existing-task
reconfiguration leaves `settings.json` at its previous `daily_time`, and a post-sync indexing
failure is retained as a sanitized error in `/api/sync/status`. The existing
`test_cli_scheduled_all_sync_honors_enabled_source_settings` test already covered the required
scheduled `sync --all` behavior and remained green.

RED verification from `D:\\wikilocal\\app`:

```text
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_service.py tests\\test_scheduler.py -v
3 failed, 24 passed
```

The failures showed that manual all-sync skipped disabled sources, task reconfiguration raised an
uncontrolled 500 response after persisting the new time, and indexing failures left a successful
sync status with no error.

GREEN verification after the minimal service changes:

```text
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_service.py tests\\test_scheduler.py -v
27 passed
```

No real Feishu synchronization, Ollama model operation, or Windows Task Scheduler operation was
performed for this regression fix.

Full application verification from `D:\\wikilocal\\app`:

```text
.\\.venv\\Scripts\\python.exe -m pytest -v
89 passed, 74 subtests passed
```

## Start Script Readiness Fix

A contract test was added first in `tests\\test_service.py`. It reads `scripts\\start.ps1`
without executing it, so the test cannot leave a server process running. The contract requires a
hidden background Uvicorn process to start before a bounded loop polls only
`http://127.0.0.1:8765/api/health`, with the browser opened only after readiness succeeds and the
new process stopped on timeout.

RED verification from `D:\\wikilocal\\app`:

```text
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_service.py::test_start_script_waits_for_local_health_before_opening_the_browser -v
1 failed
```

The prior script opened the browser before starting the foreground Uvicorn command and did not
poll the local health endpoint.

GREEN verification after the script change:

```text
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_service.py::test_start_script_waits_for_local_health_before_opening_the_browser -v
1 passed
```

Final verification from `D:\\wikilocal\\app`:

```text
.\\.venv\\Scripts\\python.exe -m pytest -v
90 passed, 74 subtests passed
```

`scripts\\start.ps1` was also parsed with PowerShell's parser without syntax errors. The start
script was not executed during verification, so no server process was left running.
