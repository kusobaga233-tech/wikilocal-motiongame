from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wikilocal import feishu
from wikilocal.feishu import FeishuClient, FeishuClientError


class FeishuClientTests(unittest.TestCase):
    def test_windows_executable_resolution_prefers_available_cmd_file(self) -> None:
        cmd_path = r"C:\\Users\\Admin\\AppData\\Roaming\\npm\\lark-cli.cmd"

        def which(candidate: str) -> str | None:
            return cmd_path if candidate == "lark-cli.cmd" else None

        with patch("shutil.which", side_effect=which):
            executable = feishu.resolve_lark_cli_executable(platform_name="nt")

        self.assertEqual(executable, cmd_path)

    def test_executable_resolution_keeps_cross_platform_fallback(self) -> None:
        with patch("shutil.which", return_value=None):
            executable = feishu.resolve_lark_cli_executable(platform_name="posix")

        self.assertEqual(executable, "lark-cli")

    def test_list_chats_constructs_a_read_only_user_command(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...]) -> str:
            commands.append(command)
            return json.dumps({"ok": True, "data": {"items": []}})

        client = FeishuClient(runner=runner)

        self.assertEqual(client.list_chats(), {"items": []})
        self.assertEqual(
            commands,
            [
                (
                    "lark-cli",
                    "im",
                    "+chat-list",
                    "--types",
                    "p2p",
                    "--types",
                    "group",
                    "--as",
                    "user",
                    "--format",
                    "json",
                )
            ],
        )

    def test_list_messages_returns_data_from_a_successful_json_response(self) -> None:
        client = FeishuClient(
            runner=lambda command: json.dumps(
                {"ok": True, "identity": "user", "data": {"items": [{"message_id": "om_1"}]}}
            )
        )

        result = client.list_messages("oc_123", page_token="next-page")

        self.assertEqual(result, {"items": [{"message_id": "om_1"}]})

    def test_public_methods_do_not_allow_identity_download_or_extra_cli_arguments(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...]) -> str:
            commands.append(command)
            return json.dumps({"ok": True, "data": {"items": []}})

        client = FeishuClient(runner=runner)
        client.list_messages("oc_123")

        command = commands[0]
        self.assertFalse(hasattr(client, "execute"))
        self.assertFalse(hasattr(client, "runner"))
        self.assertEqual(command.count("--as"), 1)
        self.assertEqual(command[command.index("--as") + 1], "user")
        self.assertNotIn("bot", command)
        self.assertNotIn("--download-resources", command)

        with self.assertRaises(TypeError):
            client.list_messages("oc_123", identity="bot")
        with self.assertRaises(TypeError):
            client.read_document("doc_123", download_resources=True)
        with self.assertRaises(TypeError):
            client.list_chats(extra_args=("--as", "bot"))
        with self.assertRaisesRegex(FeishuClientError, "invalid identifier"):
            client.list_messages("--as")

        self.assertEqual(len(commands), 1)

    def test_private_read_dispatcher_rejects_attachment_download_for_every_command(self) -> None:
        client = FeishuClient(
            runner=lambda command: json.dumps({"ok": True, "data": {"items": []}})
        )
        read = client._FeishuClient__read
        commands = (
            ("lark-cli", "im", "+chat-list", "--as", "user", "--download-resources", "--format", "json"),
            ("lark-cli", "im", "+chat-messages-list", "--chat-id", "oc_1", "--as", "user", "--download-resources", "--format", "json"),
            ("lark-cli", "im", "+threads-messages-list", "--thread", "omt_1", "--as", "user", "--download-resources", "--format", "json"),
            ("lark-cli", "wiki", "+space-list", "--as", "user", "--download-resources", "--format", "json"),
            ("lark-cli", "wiki", "+node-list", "--space-id", "space_1", "--as", "user", "--download-resources", "--format", "json"),
            ("lark-cli", "docs", "+fetch", "--doc", "doc_1", "--doc-format", "markdown", "--as", "user", "--download-resources", "--format", "json"),
        )

        for command in commands:
            with self.subTest(command=command[:3]):
                with self.assertRaisesRegex(FeishuClientError, "not allowed"):
                    read(command)

    def test_private_read_dispatcher_rejects_unknown_flags_for_every_command(self) -> None:
        client = FeishuClient(
            runner=lambda command: json.dumps({"ok": True, "data": {"items": []}})
        )
        read = client._FeishuClient__read
        commands = (
            ("lark-cli", "im", "+chat-list", "--as", "user", "--unexpected", "value", "--format", "json"),
            ("lark-cli", "im", "+chat-messages-list", "--chat-id", "oc_1", "--as", "user", "--unexpected", "value", "--format", "json"),
            ("lark-cli", "im", "+threads-messages-list", "--thread", "omt_1", "--as", "user", "--unexpected", "value", "--format", "json"),
            ("lark-cli", "wiki", "+space-list", "--as", "user", "--unexpected", "value", "--format", "json"),
            ("lark-cli", "wiki", "+node-list", "--space-id", "space_1", "--as", "user", "--unexpected", "value", "--format", "json"),
            ("lark-cli", "docs", "+fetch", "--doc", "doc_1", "--doc-format", "markdown", "--as", "user", "--unexpected", "value", "--format", "json"),
        )

        for command in commands:
            with self.subTest(command=command[:3]):
                with self.assertRaisesRegex(FeishuClientError, "not allowed"):
                    read(command)

    def test_private_read_dispatcher_allows_only_markdown_document_fetch(self) -> None:
        command = (
            "lark-cli",
            "docs",
            "+fetch",
            "--doc",
            "doc_1",
            "--doc-format",
            "markdown",
            "--as",
            "user",
            "--format",
            "json",
        )
        client = FeishuClient(
            runner=lambda received: json.dumps(
                {"ok": True, "data": {"command": list(received)}}
            )
        )

        result = client._FeishuClient__read(command)

        self.assertEqual(result, {"command": list(command)})

    def test_wiki_and_document_methods_construct_read_only_commands(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...]) -> str:
            commands.append(command)
            return json.dumps({"ok": True, "data": {"items": []}})

        client = FeishuClient(runner=runner)

        client.list_wiki_spaces()
        client.list_wiki_nodes("space_1", parent_node_token="node_1")
        client.read_document("doc_1")
        client.list_thread_messages("omt_1")

        self.assertEqual(
            commands,
            [
                ("lark-cli", "wiki", "+space-list", "--as", "user", "--format", "json"),
                (
                    "lark-cli",
                    "wiki",
                    "+node-list",
                    "--space-id",
                    "space_1",
                    "--parent-node-token",
                    "node_1",
                    "--as",
                    "user",
                    "--format",
                    "json",
                ),
                (
                    "lark-cli",
                    "docs",
                    "+fetch",
                    "--doc",
                    "doc_1",
                    "--doc-format",
                    "markdown",
                    "--as",
                    "user",
                    "--format",
                    "json",
                ),
                (
                    "lark-cli",
                    "im",
                    "+threads-messages-list",
                    "--thread",
                    "omt_1",
                    "--as",
                    "user",
                    "--order",
                    "asc",
                    "--format",
                    "json",
                ),
            ],
        )

    def test_failed_json_response_raises_a_sanitized_error(self) -> None:
        client = FeishuClient(
            runner=lambda command: json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "permission",
                        "message": "access token secret-token-value must not leak",
                    },
                }
            )
        )

        with self.assertRaisesRegex(FeishuClientError, "Feishu read command failed") as raised:
            client.list_wiki_spaces()

        self.assertNotIn("secret-token-value", str(raised.exception))

    def test_permission_preflight_reports_missing_scope_without_authorizing(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...]) -> str:
            commands.append(command)
            return json.dumps(
                {
                    "ok": True,
                    "identities": {
                        "user": {
                            "scope": [
                                "im:chat:read",
                                "im:message:readonly",
                                "docx:document:readonly",
                            ]
                        }
                    },
                }
            )

        client = FeishuClient(runner=runner)

        result = client.permission_preflight()

        self.assertIn("wiki:space:retrieve", result.missing_scopes)
        self.assertIn(
            'lark-cli auth login --scope "wiki:space:retrieve"',
            result.remediation_commands,
        )
        self.assertEqual(commands, [("lark-cli", "auth", "status", "--json", "--verify")])

    def test_permission_preflight_accepts_whitespace_separated_user_scopes(self) -> None:
        client = FeishuClient(
            runner=lambda command: json.dumps(
                {
                    "ok": True,
                    "identities": {
                        "user": {
                            "scope": "im:chat:read im:message:readonly "
                            "docx:document:readonly wiki:space:retrieve"
                        }
                    },
                }
            )
        )

        result = client.permission_preflight()

        self.assertEqual(result.missing_scopes, ())
        self.assertIn("wiki:space:retrieve", result.granted_scopes)

    def test_permission_preflight_accepts_auth_status_payload_without_ok(self) -> None:
        client = FeishuClient(
            runner=lambda command: json.dumps(
                {
                    "identities": {
                        "user": {
                            "scope": "im:chat:read im:message:readonly "
                            "docx:document:readonly wiki:space:retrieve"
                        }
                    }
                }
            )
        )

        result = client.permission_preflight()

        self.assertEqual(result.missing_scopes, ())
