from __future__ import annotations

import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_build_task_xml_uses_daily_time_and_local_cli_command() -> None:
    from wikilocal.scheduler import build_task_xml

    xml = build_task_xml("python -m wikilocal.cli sync --all", "02:00")

    assert "<StartBoundary>2000-01-01T02:00:00</StartBoundary>" in xml
    assert "<Command>python</Command>" in xml
    assert "-m wikilocal.cli sync --all" in xml
    assert "<UserId>S-1-5-18</UserId>" not in xml


def test_write_task_xml_uses_windows_task_scheduler_encoding(tmp_path: Path) -> None:
    from wikilocal.scheduler import write_task_xml
    from wikilocal.settings import Settings

    task_file = write_task_xml(Settings.load(tmp_path), "python -m wikilocal.cli sync --all")

    raw = task_file.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    assert 'encoding="UTF-16"' in raw.decode("utf-16")


@pytest.mark.parametrize("daily_time", ["2:00", "24:00", "12:60", "noon"])
def test_build_task_xml_rejects_invalid_daily_time(daily_time: str) -> None:
    from wikilocal.scheduler import SchedulerError, build_task_xml

    with pytest.raises(SchedulerError, match="daily_time"):
        build_task_xml("python -m wikilocal.cli sync --all", daily_time)


def test_build_task_xml_rejects_non_sync_command() -> None:
    from wikilocal.scheduler import SchedulerError, build_task_xml

    with pytest.raises(SchedulerError, match="sync"):
        build_task_xml("powershell -Command Remove-Item C:\\", "02:00")


def test_cli_sync_invokes_only_the_requested_source_kind(tmp_path: Path) -> None:
    from wikilocal.cli import main

    calls: list[str] = []

    class FakeStorage:
        def close(self) -> None:
            calls.append("close")

    class FakeRuntime:
        storage = FakeStorage()

        def synchronize(self, kind: str, *, honor_enabled: bool = False) -> dict[str, int]:
            calls.append(f"{kind}:{honor_enabled}")
            return {"created": 0, "changed": 0, "skipped": 0, "failed": 0}

    result = main(
        ["--root", str(tmp_path), "sync", "--chats"],
        runtime_factory=lambda _settings: FakeRuntime(),
    )

    assert result == 0
    assert calls == ["chats:False", "close"]


def test_cli_scheduled_all_sync_honors_enabled_source_settings(tmp_path: Path) -> None:
    from wikilocal.cli import main

    calls: list[tuple[str, bool] | str] = []

    class FakeStorage:
        def close(self) -> None:
            calls.append("close")

    class FakeRuntime:
        storage = FakeStorage()

        def synchronize(self, kind: str, *, honor_enabled: bool = False) -> dict[str, int]:
            calls.append((kind, honor_enabled))
            return {"created": 0, "changed": 0, "skipped": 0, "failed": 0}

    result = main(
        ["--root", str(tmp_path), "sync", "--all"],
        runtime_factory=lambda _settings: FakeRuntime(),
    )

    assert result == 0
    assert calls == [("all", True), "close"]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "[::1]"])
def test_cli_serve_rejects_non_loopback_hosts(tmp_path: Path, host: str) -> None:
    from wikilocal.cli import main

    def server_runner(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("A rejected host must not start the server.")

    with pytest.raises(SystemExit):
        main(
            ["--root", str(tmp_path), "serve", "--host", host],
            server_runner=server_runner,
        )


def test_cli_serve_accepts_only_configured_loopback_host(tmp_path: Path) -> None:
    from wikilocal.cli import main

    calls: list[tuple[object, str, int]] = []
    result = main(
        ["--root", str(tmp_path), "serve", "--host", "localhost", "--port", "9000"],
        server_runner=lambda app, *, host, port: calls.append((app, host, port)),
    )

    assert result == 0
    assert calls[0][1:] == ("localhost", 9000)


def test_reconfigure_existing_task_does_not_create_task_when_absent(tmp_path: Path) -> None:
    from wikilocal.scheduler import reconfigure_daily_task_if_installed
    from wikilocal.settings import Settings

    settings = Settings.load(tmp_path)
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        commands.append(command)
        return CompletedProcess(command, returncode=1)

    changed = reconfigure_daily_task_if_installed(
        settings,
        '"python" -m wikilocal.cli sync --all',
        runner=runner,
    )

    assert changed is False
    assert commands == [["schtasks", "/Query", "/TN", "WikiLocalDailySync"]]
