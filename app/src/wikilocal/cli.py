from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import uvicorn

from wikilocal.scheduler import install_daily_task
from wikilocal.service import create_app, create_runtime
from wikilocal.settings import Settings


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: Callable[[Settings], Any] = create_runtime,
    schedule_installer: Callable[[Settings, str], Path] = install_daily_task,
    server_runner: Callable[..., None] = uvicorn.run,
) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.load(Path(args.root))

    if args.command == "setup":
        return _run_setup_script(settings.root)
    if args.command == "sync":
        kind = "documents" if args.documents else "chats" if args.chats else "all"
        runtime = runtime_factory(settings)
        try:
            print(json.dumps(runtime.synchronize(kind), ensure_ascii=False))
        finally:
            runtime.storage.close()
        return 0
    if args.command == "schedule":
        command = f'"{sys.executable}" -m wikilocal.cli sync --all'
        task_file = schedule_installer(settings, command)
        print(task_file)
        return 0
    if args.command == "serve":
        server_runner(create_app(settings), host=args.host, port=args.port)
        return 0
    raise RuntimeError(f"Unsupported command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikilocal")
    parser.add_argument("--root", default=r"D:\wikilocal", help="WikiLocal data directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Run the local setup script")

    sync = commands.add_parser("sync", help="Run a local Feishu synchronization")
    sync_kind = sync.add_mutually_exclusive_group()
    sync_kind.add_argument("--documents", action="store_true")
    sync_kind.add_argument("--chats", action="store_true")
    sync_kind.add_argument("--all", action="store_true")

    serve = commands.add_parser("serve", help="Run the local web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    commands.add_parser("schedule", help="Install the daily local sync task")
    return parser


def _run_setup_script(root: Path) -> int:
    script = root / "app" / "scripts" / "setup.ps1"
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
