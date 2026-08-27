from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_build_task_xml_uses_daily_time_and_local_cli_command() -> None:
    from wikilocal.scheduler import build_task_xml

    xml = build_task_xml("python -m wikilocal.cli sync --all", "02:00")

    assert "<StartBoundary>2000-01-01T02:00:00</StartBoundary>" in xml
    assert "<Command>python</Command>" in xml
    assert "-m wikilocal.cli sync --all" in xml
    assert "<UserId>S-1-5-18</UserId>" not in xml


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

        def synchronize(self, kind: str) -> dict[str, int]:
            calls.append(kind)
            return {"created": 0, "changed": 0, "skipped": 0, "failed": 0}

    result = main(
        ["--root", str(tmp_path), "sync", "--chats"],
        runtime_factory=lambda _settings: FakeRuntime(),
    )

    assert result == 0
    assert calls == ["chats", "close"]
