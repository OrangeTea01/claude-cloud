"""Upload boundaries and chat attachments, without cloud or real Docker."""
import asyncio
import base64
import json
import os
from copy import deepcopy
import unittest
from unittest.mock import ANY, AsyncMock, patch

from backend import file_helper as files
from backend.docker_backend import BrokerError
import test_chat as chat_fixture
import test_file_helper as file_fixture

WORKSPACE = chat_fixture.WORKSPACE_A
NAME = "截图 中文.png"
PATH = "uploads/" + "c" * 32 + "/" + NAME
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\xff\x01\xfe"
REFERENCE = {"path": PATH, "name": NAME}
METADATA = {**REFERENCE, "size": len(PNG), "mediaType": "image/png"}


class UploadValidationTests(unittest.TestCase):
    def test_names_preserve_unicode_and_reject_paths_or_controls(self):
        for name in (NAME, "report.pdf", "a ' quote.txt", "zero byte"):
            self.assertEqual(files.upload_name(name), name)
        for name in (None, "", ".", "..", "../secret", "/etc/passwd", "a/b", "a\\b", "a:b", "a\x00b", "line\nbreak", "tab\tname", "del\x7f", "中" * 86):
            with self.subTest(name=name), self.assertRaises(ValueError):
                files.upload_name(name)

    def test_attachment_references_only_accept_uploaded_namespace(self):
        self.assertEqual(files.attachment_parts(PATH), ["uploads", "c" * 32, NAME])
        for path in ("/workspace/" + PATH, "src/a.png", "uploads/../a.png", "uploads/not-an-id/a", "uploads/" + "c" * 32 + "/sub/file"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.attachment_parts(path)

    def test_image_type_is_from_bytes_and_does_not_allow_svg(self):
        for data, expected in ((PNG, "image/png"), (b"\xff\xd8\xff\xe0jpeg", "image/jpeg"),
                               (b"GIF89a", "image/gif"), (b"RIFF1234WEBP", "image/webp"),
                               (b'<svg onload="run()"/>', "application/octet-stream"),
                               (b"<html>not an image</html>", "application/octet-stream")):
            self.assertEqual(files.media_type(data), expected)

    def test_upload_bytes_and_size_limits_are_enforced(self):
        self.assertEqual(files.decode_content(base64.b64encode(PNG).decode(), "base64"), PNG)
        self.assertEqual(files.decode_content("", "base64"), b"")
        with self.assertRaises(ValueError):
            files.decode_content("??not-base64", "base64")
        with patch.object(files, "MAX_FILE_SIZE", len(PNG) - 1):
            with self.assertRaises(ValueError):
                files.decode_content(base64.b64encode(PNG).decode(), "base64")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux dir_fd/O_NOFOLLOW required")
class UploadFilesystemTests(unittest.TestCase):
    setUp = file_fixture.FileBoundaryTests.setUp
    tearDown = file_fixture.FileBoundaryTests.tearDown
    call = file_fixture.FileBoundaryTests.call

    def upload(self, name=NAME, data=PNG):
        return self.call("upload", name=name, content=base64.b64encode(data).decode())

    def test_upload_is_unique_preserves_bytes_and_can_be_inspected(self):
        first, second = self.upload(), self.upload()
        self.assertNotEqual(first["path"], second["path"])
        for item in (first, second):
            self.assertEqual((self.root / item["path"]).read_bytes(), PNG)
            self.assertEqual(self.call("attachment", item["path"]), item)
            self.assertEqual(self.call("read", item["path"])["mediaType"], "image/png")
            self.assertEqual((self.root / item["path"]).stat().st_mode & 0o777, 0o600)

    def test_upload_directory_symlink_is_rejected(self):
        (self.root / "uploads").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.upload()
        self.assertEqual([item.name for item in self.outside.iterdir()], ["secret"])

    def test_attachment_symlink_hardlink_fifo_and_parent_symlink_are_rejected(self):
        item = self.upload()
        target = self.root / item["path"]
        target.unlink()
        target.symlink_to(self.outside / "secret")
        with self.assertRaises((OSError, ValueError)):
            self.call("attachment", item["path"])
        target.unlink()
        os.link(self.outside / "secret", target)
        with self.assertRaises(ValueError):
            self.call("attachment", item["path"])
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(ValueError):
            self.call("attachment", item["path"])
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.call("attachment", item["path"])

    def test_invalid_or_oversized_upload_never_creates_files(self):
        with self.assertRaises(ValueError):
            self.upload(name="../escape")
        with patch.object(files, "MAX_FILE_SIZE", 1):
            with self.assertRaises(ValueError):
                self.upload()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_uuid_collision_never_overwrites_existing_attachment(self):
        with patch.object(files.secrets, "token_hex", return_value="c" * 32):
            item = self.upload()
            with self.assertRaises(FileExistsError):
                self.upload(data=b"overwrite")
            self.assertEqual((self.root / item["path"]).read_bytes(), PNG)

    def test_changed_or_removed_attachment_is_rejected_before_send(self):
        item = self.upload()
        with patch.object(files, "MAX_FILE_SIZE", 1):
            with self.assertRaises(ValueError):
                self.call("attachment", item["path"])
        (self.root / item["path"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.call("attachment", item["path"])


class ChatAttachmentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = chat_fixture.ChatTests.asyncSetUp
    asyncTearDown = chat_fixture.ChatTests.asyncTearDown
    connected = chat_fixture.ChatTests.connected

    async def test_attachment_paths_reach_claude_but_history_keeps_original_message(self):
        process = self.connected()
        self.session.phase = "idle"
        self.broker.files = AsyncMock(return_value=METADATA)
        raw = "  请说明这个截图。\n"
        await self.chats.send(WORKSPACE, raw, [REFERENCE])
        await self.session.input_task
        self.broker.files.assert_awaited_once_with(WORKSPACE, "attachment", PATH)
        content = process.stdin.messages[0]["message"]["content"]
        self.assertTrue(content.startswith(raw))
        self.assertIn("Read", content)
        manifest = json.loads(content.splitlines()[-1])
        self.assertEqual(manifest[0]["path"], "/workspace/" + PATH)
        self.assertEqual(manifest[0]["mediaType"], "image/png")
        message = self.session.messages[0]
        self.assertEqual(message["text"], raw)
        self.assertEqual(message["attachments"], [METADATA])
        self.assertEqual(self.chats.store.load(WORKSPACE)[1][0]["attachments"], [METADATA])

    async def test_image_only_message_is_supported(self):
        process = self.connected()
        self.session.phase = "idle"
        self.broker.files = AsyncMock(return_value=METADATA)
        await self.chats.send(WORKSPACE, "", [REFERENCE])
        await self.session.input_task
        self.assertEqual(self.session.messages[0]["text"], "")
        self.assertIn("/workspace/" + PATH, process.stdin.messages[0]["message"]["content"])

    async def test_invalid_duplicate_and_excess_references_do_not_reach_storage(self):
        self.broker.files = AsyncMock()
        invalid = ([REFERENCE] * 6, [REFERENCE, REFERENCE], [{"path": "/etc/passwd", "name": "passwd"}],
                   [{**REFERENCE, "name": "other.png"}], [{**REFERENCE, "size": 12}], [None], "bad")
        for attachments in invalid:
            with self.subTest(attachments=attachments), self.assertRaises(BrokerError):
                await self.chats.send(WORKSPACE, "check", attachments)
        self.broker.files.assert_not_awaited()
        self.assertEqual(self.session.messages, [])

    async def test_missing_or_unverified_attachment_never_starts_turn(self):
        self.broker.files = AsyncMock(side_effect=BrokerError("文件不存在"))
        with self.assertRaises(BrokerError):
            await self.chats.send(WORKSPACE, "check", [REFERENCE])
        self.broker.chat_command.assert_not_awaited()
        self.assertEqual(self.session.messages, [])
        self.broker.files = AsyncMock(return_value={**METADATA, "size": files.MAX_FILE_SIZE + 1})
        with self.assertRaises(BrokerError):
            await self.chats.send(WORKSPACE, "check", [REFERENCE])
        self.assertEqual(self.session.messages, [])

    async def test_metadata_wait_does_not_block_stop_and_rechecks_admission(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def inspect(*args):
            started.set()
            await release.wait()
            return METADATA
        self.broker.files = inspect
        task = asyncio.create_task(self.chats.send(WORKSPACE, "check", [REFERENCE]))
        await started.wait()
        # Lifecycle operations must remain able to acquire the shared lock.
        async with asyncio.timeout(1):
            async with self.chats.lock:
                self.broker.workspace.return_value = {"id": WORKSPACE, "running": False}
        release.set()
        with self.assertRaises(BrokerError):
            await task
        self.assertEqual(self.session.messages, [])


class UploadApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chat_fixture.ChatApiTests.setUpClass.__func__(cls)

    setUp = chat_fixture.ChatApiTests.setUp
    tearDown = chat_fixture.ChatApiTests.tearDown
    login = chat_fixture.ChatApiTests.login

    def test_upload_requires_auth_origin_csrf_before_file_broker(self):
        path = f"/api/workspaces/{WORKSPACE}/attachments"
        payload = {"name": NAME, "content": base64.b64encode(PNG).decode()}
        self.broker.files = AsyncMock(return_value=METADATA)
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        headers = self.login()
        for bad in ({}, {"Origin": self.origin}, {**headers, "Origin": "https://evil.example"},
                    {**headers, "X-CSRF-Token": "wrong"}):
            self.assertEqual(self.client.post(path, headers=bad, json=payload).status_code, 403)
        bad_bytes = [(b"origin", self.origin.encode()), (b"x-csrf-token", b"\xff")]
        self.assertEqual(self.client.post(path, headers=bad_bytes, json=payload).status_code, 403)
        self.broker.files.assert_not_awaited()
        response = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), METADATA)
        self.broker.files.assert_awaited_once_with(WORKSPACE, "upload", "", name=NAME,
                                                 content=payload["content"], authorize=ANY)
        self.assertTrue(callable(self.broker.files.await_args.kwargs["authorize"]))

    def test_upload_rejects_paths_extra_fields_and_oversized_bodies(self):
        headers = self.login()
        path = f"/api/workspaces/{WORKSPACE}/attachments"
        self.broker.files = AsyncMock()
        for body in ({"name": "../escape", "content": ""}, {"name": NAME, "content": "", "path": "overwritten"}):
            self.assertEqual(self.client.post(path, headers=headers, json=body).status_code, 422)
        response = self.client.post(path, headers=headers, content=b"x" * (files.MAX_REQUEST_SIZE + 1))
        self.assertEqual(response.status_code, 413)
        self.broker.files.assert_not_awaited()

    def test_chat_accepts_attachment_only_and_passes_only_validated_fields(self):
        headers = self.login()
        chats = self.app.state.runtime["chats"]
        with patch.object(chats, "send", AsyncMock(return_value={"phase": "starting"})) as send:
            response = self.client.post(f"/api/workspaces/{WORKSPACE}/chat/message", headers=headers,
                                        json={"text": "", "attachments": [REFERENCE]})
            self.assertEqual(response.status_code, 202)
            send.assert_awaited_once_with(WORKSPACE, "", [REFERENCE], authorize=ANY)
            self.assertTrue(callable(send.await_args.kwargs["authorize"]))

    def test_chat_rejects_invalid_attachment_shape_and_blank_without_files(self):
        headers = self.login()
        chats = self.app.state.runtime["chats"]
        bodies = [{"text": ""}, {"attachments": []}, {"text": "read", "attachments": [REFERENCE] * 6},
                  {"text": "read", "attachments": [{**REFERENCE, "mediaType": "image/png"}]},
                  {"text": "read", "attachments": [{"path": "/etc/passwd", "name": "passwd"}]}]
        with patch.object(chats, "send", AsyncMock()) as send:
            for body in bodies:
                self.assertEqual(self.client.post(f"/api/workspaces/{WORKSPACE}/chat/message", headers=headers, json=body).status_code, 422)
            send.assert_not_awaited()
