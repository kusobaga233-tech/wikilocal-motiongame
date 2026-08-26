from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_CommandRunner = Callable[[tuple[str, ...]], str]


class FeishuClientError(RuntimeError):
    """Raised when a read-only Feishu CLI call cannot return valid data."""


@dataclass(frozen=True)
class PermissionPreflight:
    granted_scopes: frozenset[str]
    missing_scopes: tuple[str, ...]
    remediation_commands: tuple[str, ...]


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
    _REQUIRED_SCOPES = (
        "im:chat:read",
        "im:message:readonly",
        "docx:document:readonly",
        "wiki:space:retrieve",
    )

    def __init__(self, runner: _CommandRunner | None = None) -> None:
        self.__runner = runner or self.__run_lark_cli

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
        return self.__read((*command, "--format", "json"))

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
        return self.__read((*command, "--format", "json"))

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
        return self.__read((*command, "--format", "json"))

    def list_wiki_spaces(self, *, page_token: str | None = None) -> Any:
        command = ["lark-cli", "wiki", "+space-list", "--as", "user"]
        _append_optional(command, "--page-token", page_token)
        return self.__read((*command, "--format", "json"))

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
        return self.__read((*command, "--format", "json"))

    def read_document(self, document: str) -> Any:
        return self.__read(
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

    def permission_preflight(self) -> PermissionPreflight:
        payload = self.__invoke(("lark-cli", "auth", "status", "--json", "--verify"))
        granted_scopes = _user_scopes(payload)
        missing_scopes = tuple(
            scope for scope in self._REQUIRED_SCOPES if scope not in granted_scopes
        )
        return PermissionPreflight(
            granted_scopes=granted_scopes,
            missing_scopes=missing_scopes,
            remediation_commands=tuple(
                f'lark-cli auth login --scope "{scope}"' for scope in missing_scopes
            ),
        )

    def __read(self, command: Sequence[str]) -> Any:
        command_tuple = tuple(command)
        self.__validate_read_command(command_tuple)
        payload = self.__invoke(command_tuple)
        if "data" not in payload:
            raise FeishuClientError("Feishu read command returned no data.")
        return payload["data"]

    def __invoke(self, command: tuple[str, ...]) -> dict[str, Any]:
        try:
            payload = json.loads(self.__runner(command))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise FeishuClientError("Feishu read command did not return valid JSON.") from error

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise FeishuClientError("Feishu read command failed.")
        return payload

    @classmethod
    def __validate_read_command(cls, command: tuple[str, ...]) -> None:
        if len(command) < 3 or command[:3] not in cls._READ_ONLY_COMMANDS:
            raise FeishuClientError("Feishu command is not allowed.")
        if command.count("--as") != 1:
            raise FeishuClientError("Feishu read commands must use user identity exactly once.")
        as_index = command.index("--as")
        if as_index + 1 >= len(command) or command[as_index + 1] != "user":
            raise FeishuClientError("Feishu read commands must use user identity exactly once.")
        if command.count("--format") != 1:
            raise FeishuClientError("Feishu read commands must request JSON output.")
        if command[-2:] != ("--format", "json"):
            raise FeishuClientError("Feishu read commands must request JSON output.")

    @staticmethod
    def __run_lark_cli(command: tuple[str, ...]) -> str:
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


def _user_scopes(payload: Mapping[str, Any]) -> frozenset[str]:
    identities = payload.get("identities")
    if not isinstance(identities, dict):
        return frozenset()
    user = identities.get("user")
    if not isinstance(user, dict):
        return frozenset()
    scopes = user.get("scope")
    if not isinstance(scopes, list):
        return frozenset()
    return frozenset(scope for scope in scopes if isinstance(scope, str))
