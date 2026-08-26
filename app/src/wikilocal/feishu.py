from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any


CommandRunner = Callable[[tuple[str, ...]], str]


class FeishuClientError(RuntimeError):
    """Raised when a read-only Feishu CLI call cannot return valid data."""


class FeishuClient:
    _READ_ONLY_COMMANDS = frozenset(
        {
            ("lark-cli", "im", "+chat-list"),
            ("lark-cli", "im", "+chat-messages-list"),
            ("lark-cli", "im", "+threads-messages-list"),
            ("lark-cli", "wiki", "+space-list"),
            ("lark-cli", "wiki", "+node-list"),
            ("lark-cli", "docs", "+fetch"),
        }
    )

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or self._run_lark_cli

    def list_chats(self, *, page_token: str | None = None) -> Any:
        command = [
            "lark-cli",
            "im",
            "+chat-list",
            "--types",
            "p2p",
            "--types",
            "group",
            "--as",
            "user",
        ]
        _append_optional(command, "--page-token", page_token)
        return self.execute((*command, "--format", "json"))

    def list_messages(
        self,
        chat_id: str,
        *,
        page_token: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> Any:
        command = [
            "lark-cli",
            "im",
            "+chat-messages-list",
            "--chat-id",
            _identifier(chat_id),
            "--as",
            "user",
            "--order",
            "asc",
        ]
        _append_optional(command, "--page-token", page_token)
        _append_optional(command, "--start", start)
        _append_optional(command, "--end", end)
        return self.execute((*command, "--format", "json"))

    def list_thread_messages(self, thread_id: str, *, page_token: str | None = None) -> Any:
        command = [
            "lark-cli",
            "im",
            "+threads-messages-list",
            "--thread",
            _identifier(thread_id),
            "--as",
            "user",
            "--order",
            "asc",
        ]
        _append_optional(command, "--page-token", page_token)
        return self.execute((*command, "--format", "json"))

    def list_wiki_spaces(self, *, page_token: str | None = None) -> Any:
        command = ["lark-cli", "wiki", "+space-list", "--as", "user"]
        _append_optional(command, "--page-token", page_token)
        return self.execute((*command, "--format", "json"))

    def list_wiki_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_token: str | None = None,
    ) -> Any:
        command = ["lark-cli", "wiki", "+node-list", "--space-id", _identifier(space_id)]
        _append_optional(command, "--parent-node-token", parent_node_token)
        command.extend(("--as", "user"))
        _append_optional(command, "--page-token", page_token)
        return self.execute((*command, "--format", "json"))

    def read_document(self, document: str) -> Any:
        return self.execute(
            (
                "lark-cli",
                "docs",
                "+fetch",
                "--doc",
                _identifier(document),
                "--doc-format",
                "markdown",
                "--as",
                "user",
                "--format",
                "json",
            )
        )

    def execute(self, command: Sequence[str]) -> Any:
        command_tuple = tuple(command)
        self._validate_command(command_tuple)
        try:
            payload = json.loads(self._runner(command_tuple))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise FeishuClientError("Feishu read command did not return valid JSON.") from error

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise FeishuClientError("Feishu read command failed.")
        if "data" not in payload:
            raise FeishuClientError("Feishu read command returned no data.")
        return payload["data"]

    @classmethod
    def _validate_command(cls, command: tuple[str, ...]) -> None:
        if len(command) < 3 or command[:3] not in cls._READ_ONLY_COMMANDS:
            raise FeishuClientError("Feishu command is not allowed.")
        if command[-2:] != ("--format", "json"):
            raise FeishuClientError("Feishu read commands must request JSON output.")

    @staticmethod
    def _run_lark_cli(command: tuple[str, ...]) -> str:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        output = completed.stdout if completed.stdout.strip() else completed.stderr
        if not output.strip():
            raise FeishuClientError("Feishu read command returned no response.")
        return output


def _append_optional(command: list[str], flag: str, value: str | None) -> None:
    if value is not None:
        command.extend((flag, _identifier(value)))


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise FeishuClientError("Feishu read command received an invalid identifier.")
    return value
