"""Tool-free conversations: persistence, process boundaries and inline uploads."""
import asyncio
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.chat import ChatSession
from backend.docker_backend import BrokerError, Config, DockerBroker, MODE_LABEL, OWNER_LABEL, WORKSPACE_LABEL
import test_chat as fixture
import test_solo as solo_fixture

WORKSPACE = fixture.WORKSPACE_A


class WorkspaceModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # Model a real pre-mode registry, including an existing workspace.
        with sqlite3.connect(Path(self.directory.name) / "registry.sqlite3") as db:
            db.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT, created_at TEXT)")
            db.execute("INSERT INTO workspaces VALUES (?, 'existing code', 'earlier')", (WORKSPACE,))
        db.close()
        self.broker = DockerBroker(Config(data_dir=self.directory.name))
        self.addCleanup(self.broker.close)

    async def test_migration_default_and_new_chat_mode_are_durable_and_labeled(self):
        self.assertEqual(self.broker.get(WORKSPACE)["mode"], "code")
        self.broker.check_engine = AsyncMock()
        self.broker.check_running_limit = AsyncMock()
        self.broker._start = AsyncMock()
        async def workspace(identifier):
            return {**self.broker.get(identifier), "running": True}
        self.broker.workspace = workspace
        item = await self.broker.create("普通聊天", mode="chat")
        self.assertEqual(item["mode"], "chat")
        self.assertIn(MODE_LABEL + "=chat", self.broker.labels(item["id"]))
        self.assertEqual({w["mode"] for w in await self.broker.list()}, {"code", "chat"})
        reopened = DockerBroker(Config(data_dir=self.directory.name))
        try:
            self.assertEqual(reopened.get(item["id"])["mode"], "chat")
            self.assertEqual(reopened.get(WORKSPACE)["name"], "existing code")
        finally:
            reopened.close()

    async def test_invalid_mode_is_rejected_before_provisioning(self):
        self.broker.check_engine = AsyncMock()
        for mode in (None, "CHAT", "", [], "--tools Bash"):
            with self.subTest(mode=mode), self.assertRaises(BrokerError):
                await self.broker.create("invalid", mode=mode)
        self.broker.check_engine.assert_not_awaited()

    async def test_resource_mode_mismatch_is_rejected_and_old_code_labels_work(self):
        labels = {OWNER_LABEL: self.broker.owner, WORKSPACE_LABEL: WORKSPACE}
        self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Config": {"Labels": labels}}]).encode(), b""))
        await self.broker.inspect_owned("container", "test", WORKSPACE)
        self.broker.db.execute("UPDATE workspaces SET mode='chat' WHERE id=?", (WORKSPACE,))
        self.broker.db.commit()
        with self.assertRaises(BrokerError):
            await self.broker.inspect_owned("container", "test", WORKSPACE)

    async def test_chat_cli_disables_tools_mcp_skills_and_uses_separate_session(self):
        broker = SimpleNamespace(
            workspace=AsyncMock(return_value={"running": True, "mode": "chat"}),
            shared_auth_status=AsyncMock(return_value={"authenticated": True}),
            names=Mock(return_value={"container": "owned-container"}))
        args = await DockerBroker.chat_command(broker, WORKSPACE, None, fixture.SESSION_A, "sonnet")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--disallowedTools") + 1], "*")
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), {"mcpServers": {}})
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--safe-mode", "--restricted", "--system-prompt"):
            self.assertIn(flag, args)
        self.assertNotIn("--resume", args)
        self.assertEqual(args[args.index("--session-id") + 1], fixture.SESSION_A)
        self.assertNotIn("--dangerously-skip-permissions", args)
        broker.shared_auth_status.return_value = {"authenticated": False}
        with self.assertRaises(BrokerError):
            await DockerBroker.chat_command(broker, WORKSPACE, None, fixture.SESSION_A)


class ChatModeTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.ChatTests.asyncTearDown
    connected = fixture.ChatTests.connected

    async def asyncSetUp(self):
        await fixture.ChatTests.asyncSetUp(self)
        self.broker.get.return_value = {"id": WORKSPACE, "mode": "chat"}
        self.broker.workspace.return_value = {"id": WORKSPACE, "running": True, "mode": "chat"}
        self.session = ChatSession(self.chats, WORKSPACE)
        self.chats.items[WORKSPACE] = self.session

    def attachment(self, name, data, media="application/octet-stream"):
        reference = {"path": "uploads/" + "c" * 32 + "/" + name, "name": name}
        metadata = {**reference, "size": len(data), "mediaType": media}
        content = {"path": reference["path"], "size": len(data), "mediaType": media,
                   "encoding": "base64", "content": base64.b64encode(data).decode()}
        self.broker.files = AsyncMock(side_effect=[metadata, content])
        return reference

    async def test_text_chat_keeps_original_message_and_reports_mode(self):
        process = self.connected()
        self.session.phase = "idle"
        await self.chats.send(WORKSPACE, "请帮我写一个故事")
        await self.session.input_task
        self.assertEqual(process.stdin.messages[0]["message"]["content"], "请帮我写一个故事")
        self.assertEqual(self.session.snapshot()["mode"], "chat")

    async def test_all_permission_requests_are_denied_without_user_approval(self):
        process = self.connected()
        for tool in ("Read", "Bash", "Write", "mcp__external__run", "Agent"):
            await self.session.handle(fixture.permission_frame(tool=tool))
            self.assertEqual(process.stdin.messages[-1]["response"]["response"]["behavior"], "deny")
            self.assertEqual(self.session.pending, {})

    async def test_image_is_inline_and_not_stored_in_public_history(self):
        png = b"\x89PNG\r\n\x1a\nimage-data"
        ref = self.attachment("example.png", png, "image/png")
        process = self.connected()
        self.session.phase = "idle"
        await self.chats.send(WORKSPACE, "看看图片", [ref])
        await self.session.input_task
        blocks = process.stdin.messages[0]["message"]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "看看图片"})
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), png)
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertNotIn(base64.b64encode(png).decode(), json.dumps(self.session.snapshot()))
        self.assertNotIn("Read", json.dumps(blocks))

    async def test_utf8_attachment_is_inline_reference_data(self):
        ref = self.attachment("notes.md", "# 笔记\n需要总结".encode())
        process = self.connected()
        self.session.phase = "idle"
        await self.chats.send(WORKSPACE, "", [ref])
        await self.session.input_task
        blocks = process.stdin.messages[0]["message"]["content"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("需要总结", blocks[1]["text"])
        self.assertEqual(self.session.messages[0]["text"], "")

    async def test_binary_and_large_attachments_fail_before_turn_or_process(self):
        for name, data in (("report.pdf", b"%PDF-plain ascii"), ("archive.zip", b"PK\x03\x04"),
                           ("fake.txt", b"\x00\x01"), ("invalid.txt", b"\xff")):
            ref = self.attachment(name, data)
            with self.subTest(name=name), self.assertRaises(BrokerError):
                await self.chats.send(WORKSPACE, "read", [ref])
        ref = self.attachment("too-big.png", b"12345", "image/png")
        with patch("backend.chat.CHAT_IMAGE_BYTES", 4), self.assertRaises(BrokerError):
            await self.chats.send(WORKSPACE, "read", [ref])
        self.broker.chat_command.assert_not_awaited()
        self.assertIsNone(self.session.input_task)
        self.assertEqual(self.session.messages, [])
        self.assertEqual(self.chats.store.load(WORKSPACE), (None, []))

    async def test_attachment_read_rechecks_authorization_and_running_state(self):
        ref = self.attachment("note.txt", b"hello")
        metadata, content = list(self.broker.files.side_effect)
        started, release = asyncio.Event(), asyncio.Event()
        async def read(_id, op, path, **kwargs):
            if op == "attachment":
                return metadata
            started.set()
            await release.wait()
            return content
        self.broker.files = read
        authorize = Mock()
        task = asyncio.create_task(self.chats.send(WORKSPACE, "read", [ref], authorize=authorize))
        await started.wait()
        async with asyncio.timeout(1):
            async with self.chats.lock:
                self.broker.workspace.return_value["running"] = False
        release.set()
        with self.assertRaises(BrokerError):
            await task
        self.assertGreaterEqual(authorize.call_count, 2)
        self.assertEqual(self.session.messages, [])
        self.broker.chat_command.assert_not_awaited()


class ChatModeApiTests(unittest.TestCase):
    setUp = solo_fixture.SoloApiTests.setUp
    open = solo_fixture.SoloApiTests.open
    tearDown = solo_fixture.SoloApiTests.tearDown
    login = solo_fixture.SoloApiTests.login

    def test_fixed_creation_mode_is_validated_and_returns_in_workspace_and_chat(self):
        headers = self.login()
        for mode in ("chat", "code"):
            response = self.client.post("/api/workspaces", headers=headers, json={"name": "new", "mode": mode})
            self.assertEqual(response.status_code, 201, response.text)
            item = response.json()
            self.assertEqual(item["mode"], mode)
            self.assertEqual(self.client.get(f'/api/workspaces/{item["id"]}/chat').json()["mode"], mode)
        response = self.client.post("/api/workspaces", headers=headers, json={"name": "old client"})
        self.assertEqual(response.json()["mode"], "code")
        for invalid in ("other", "--tools Bash", None, 1):
            response = self.client.post("/api/workspaces", headers=headers, json={"name": "invalid", "mode": invalid})
            self.assertEqual(response.status_code, 422)

