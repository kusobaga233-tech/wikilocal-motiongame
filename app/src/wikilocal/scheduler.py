from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as element_tree
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wikilocal.settings import Settings


class SchedulerError(ValueError):
    """Raised when a daily WikiLocal task configuration is unsafe or invalid."""


_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_SYNC_COMMAND_PATTERN = re.compile(
    r'^(?P<program>"[^"]+"|[^\s]+)\s+(?P<arguments>-m wikilocal\.cli sync --all)\Z'
)
_TASK_NAME = "WikiLocalDailySync"
TaskRunner = Callable[..., subprocess.CompletedProcess[Any]]


def build_task_xml(command: str, daily_time: str) -> str:
    """Return a UTF-8 Task Scheduler definition for the sole WikiLocal sync task."""
    if not _TIME_PATTERN.fullmatch(daily_time):
        raise SchedulerError("Invalid daily_time: expected HH:MM in 24-hour time.")
    match = _SYNC_COMMAND_PATTERN.fullmatch(command.strip())
    if match is None:
        raise SchedulerError("The scheduled command must be wikilocal.cli sync --all.")

    task = element_tree.Element("Task", {"version": "1.4", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"})
    triggers = element_tree.SubElement(task, "Triggers")
    daily = element_tree.SubElement(triggers, "CalendarTrigger")
    element_tree.SubElement(daily, "StartBoundary").text = f"2000-01-01T{daily_time}:00"
    schedule_by_day = element_tree.SubElement(daily, "ScheduleByDay")
    element_tree.SubElement(schedule_by_day, "DaysInterval").text = "1"
    settings = element_tree.SubElement(task, "Settings")
    element_tree.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    element_tree.SubElement(settings, "StartWhenAvailable").text = "true"
    element_tree.SubElement(settings, "ExecutionTimeLimit").text = "PT4H"
    actions = element_tree.SubElement(task, "Actions", {"Context": "Author"})
    execute = element_tree.SubElement(actions, "Exec")
    element_tree.SubElement(execute, "Command").text = match.group("program").strip('"')
    element_tree.SubElement(execute, "Arguments").text = match.group("arguments")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + element_tree.tostring(
        task, encoding="unicode", short_empty_elements=False
    )


def write_task_xml(settings: Settings, command: str) -> Path:
    task_file = settings.root / "data" / "logs" / f"{_TASK_NAME}.xml"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(build_task_xml(command, settings.daily_time), encoding="utf-8", newline="\n")
    return task_file


def install_daily_task(
    settings: Settings,
    command: str,
    *,
    runner: TaskRunner = subprocess.run,
) -> Path:
    """Write and register exactly the WikiLocal daily sync task when the CLI asks for it."""
    task_file = write_task_xml(settings, command)
    try:
        runner(
            ["schtasks", "/Create", "/TN", _TASK_NAME, "/XML", str(task_file), "/F"],
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SchedulerError("Unable to create the WikiLocal daily sync task.") from error
    return task_file


def reconfigure_daily_task_if_installed(
    settings: Settings,
    command: str,
    *,
    runner: TaskRunner = subprocess.run,
) -> bool:
    """Update the installed task, without creating a new task from a settings save."""
    try:
        result = runner(
            ["schtasks", "/Query", "/TN", _TASK_NAME],
            check=False,
            timeout=60,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SchedulerError("Unable to check the WikiLocal daily sync task.") from error
    if result.returncode != 0:
        return False
    install_daily_task(settings, command, runner=runner)
    return True
