from __future__ import annotations

import json
import os
import shutil
import subprocess
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_CommandRunner = Callable[[tuple[str, ...]], str]
LARK_CLI_TIMEOUT_SECONDS = 60


class FeishuClientError(RuntimeError):
    """Raised when a read-only Feishu CLI call cannot return valid data."""


@dataclass(frozen=True)
class PermissionPreflight:
    granted_scopes: frozenset[str]
    missing_scopes: tuple[str, ...]
    remediation_commands: tuple[str, ...]


class FeishuClient:
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
        payload = self.__auth_status()
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

    def __auth_status(self) -> Mapping[str, Any]:
        try:
            payload = json.loads(
                self.__runner(("lark-cli", "auth", "status", "--json", "--verify"))
            )
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise FeishuClientError("Feishu auth status did not return valid JSON.") from error
        if not isinstance(payload, dict):
            raise FeishuClientError("Feishu auth status did not return an object.")
        return payload

    @classmethod
    def __validate_read_command(cls, command: tuple[str, ...]) -> None:
        prefix = command[:3]
        arguments = command[3:]
        if prefix == ("lark-cli", "im", "+chat-list"):
            cls.__validate_chat_list_arguments(arguments)
        elif prefix == ("lark-cli", "im", "+chat-messages-list"):
            cls.__validate_message_list_arguments(arguments, "--chat-id")
        elif prefix == ("lark-cli", "im", "+threads-messages-list"):
            cls.__validate_message_list_arguments(arguments, "--thread")
        elif prefix == ("lark-cli", "wiki", "+space-list"):
            cls.__validate_space_list_arguments(arguments)
        elif prefix == ("lark-cli", "wiki", "+node-list"):
            cls.__validate_node_list_arguments(arguments)
        elif prefix == ("lark-cli", "docs", "+fetch"):
            cls.__validate_document_fetch_arguments(arguments)
        else:
            raise FeishuClientError("Feishu command is not allowed.")

    @staticmethod
    def __validate_chat_list_arguments(arguments: tuple[str, ...]) -> None:
        expected = ("--types", "p2p", "--types", "group", "--as", "user")
        FeishuClient.__validate_optional_tail(arguments, expected, ("--page-token",))

    @staticmethod
    def __validate_message_list_arguments(
        arguments: tuple[str, ...], identifier_flag: str
    ) -> None:
        if len(arguments) < 6 or arguments[0] != identifier_flag:
            raise FeishuClientError("Feishu command is not allowed.")
        _identifier(arguments[1])
        expected = (identifier_flag, arguments[1], "--as", "user", "--order", "asc")
        optional_flags = (
            ("--page-token", "--start", "--end")
            if identifier_flag == "--chat-id"
            else ("--page-token",)
        )
        FeishuClient.__validate_optional_tail(arguments, expected, optional_flags)

    @staticmethod
    def __validate_space_list_arguments(arguments: tuple[str, ...]) -> None:
        FeishuClient.__validate_optional_tail(arguments, ("--as", "user"), ("--page-token",))

    @staticmethod
    def __validate_node_list_arguments(arguments: tuple[str, ...]) -> None:
        if len(arguments) < 4 or arguments[0] != "--space-id":
            raise FeishuClientError("Feishu command is not allowed.")
        _identifier(arguments[1])
        offset = 2
        if arguments[offset:offset + 1] == ("--parent-node-token",):
            if len(arguments) <= offset + 1:
                raise FeishuClientError("Feishu command is not allowed.")
            _identifier(arguments[offset + 1])
            offset += 2
        expected = (*arguments[:offset], "--as", "user")
        FeishuClient.__validate_optional_tail(arguments, expected, ("--page-token",))

    @staticmethod
    def __validate_document_fetch_arguments(arguments: tuple[str, ...]) -> None:
        if len(arguments) != 8 or arguments[0] != "--doc":
            raise FeishuClientError("Feishu command is not allowed.")
        _identifier(arguments[1])
        if arguments != (
            "--doc",
            arguments[1],
            "--doc-format",
            "markdown",
            "--as",
            "user",
            "--format",
            "json",
        ):
            raise FeishuClientError("Feishu command is not allowed.")

    @staticmethod
    def __validate_optional_tail(
        arguments: tuple[str, ...],
        required: tuple[str, ...],
        optional_flags: tuple[str, ...],
    ) -> None:
        if not arguments[:len(required)] == required:
            raise FeishuClientError("Feishu command is not allowed.")
        remainder = arguments[len(required):]
        if len(remainder) < 2 or remainder[-2:] != ("--format", "json"):
            raise FeishuClientError("Feishu command is not allowed.")
        optional_values = remainder[:-2]
        if len(optional_values) % 2:
            raise FeishuClientError("Feishu command is not allowed.")
        seen_flags: set[str] = set()
        last_flag_index = -1
        for flag, value in zip(optional_values[::2], optional_values[1::2], strict=True):
            if flag not in optional_flags or flag in seen_flags:
                raise FeishuClientError("Feishu command is not allowed.")
            flag_index = optional_flags.index(flag)
            if flag_index <= last_flag_index:
                raise FeishuClientError("Feishu command is not allowed.")
            _identifier(value)
            seen_flags.add(flag)
            last_flag_index = flag_index

    @staticmethod
    def __run_lark_cli(command: tuple[str, ...]) -> str:
        try:
            completed = subprocess.run(
                (resolve_lark_cli_executable(), *command[1:]),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=LARK_CLI_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise FeishuClientError("Feishu read command timed out.") from error
        except (OSError, ValueError) as error:
            raise FeishuClientError("Feishu read command could not start.") from error
        if completed.returncode != 0:
            raise FeishuClientError("Feishu read command execution failed.")
        output = completed.stdout if completed.stdout.strip() else completed.stderr
        if not output.strip():
            raise FeishuClientError("Feishu read command returned no response.")
        return output


def _append_optional(command: list[str], flag: str, value: str | None) -> None:
    if value is not None:
        command.extend((flag, _identifier(value)))


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
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
    if isinstance(scopes, str):
        values = scopes.split()
    elif isinstance(scopes, list):
        values = scopes
    else:
        return frozenset()
    return frozenset(scope for scope in values if isinstance(scope, str) and scope)


def resolve_lark_cli_executable(*, platform_name: str | None = None) -> str:
    platform_name = platform_name or os.name
    candidates = (
        ("lark-cli.cmd", "lark-cli.exe", "lark-cli")
        if platform_name == "nt"
        else ("lark-cli",)
    )
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return executable
    return "lark-cli"
