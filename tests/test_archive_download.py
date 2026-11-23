"""ZIP HTTP ownership, bounded process output, and private temporary-file lifetime."""
import asyncio
import base64
import io
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from fastapi import HTTPException

from backend import archive_download as downloads
from backend.app import COOKIE
from backend.docker_backend import BrokerError
import test_multiuser_api as fixtures


def zip_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("项目/README.md", "# Hello 世界")
        archive.writestr("uploads/image.bin", b"\x00\xff\x80")
    return output.getvalue()


def artifact_for(data):
    artifact = downloads.ArchiveArtifact(io.BytesIO(data), Mock())
    artifact.size = len(data)
    return artifact


class ArchiveApiTests(unittest.TestCase):
    setUp = fixtures.MultiuserApiTests.setUp
    tearDown = fixtures.MultiuserApiTests.tearDown
    login = fixtures.MultiuserApiTests.login
    admin = fixtures.MultiuserApiTests.admin
    mutate = fixtures.MultiuserApiTests.mutate
    add_user = fixtures.MultiuserApiTests.add_user
    create_workspace = fixtures.MultiuserApiTests.create_workspace

    def test_zip_requires_owner_and_unlock_and_preserves_binary_download(self):
        self.login()
        workspace = self.create_workspace(fixtures.LOCK_PASSWORD)
        key = workspace["id"]
        path = f"/api/workspaces/{key}/archive"
        self.broker.raw(key)["name"] = '中文项目 " / test'
        data = zip_bytes()
        artifact = artifact_for(data)
        self.broker.archive = AsyncMock(return_value=artifact)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["content-length"], str(len(data)))
        self.assertIn("filename*=UTF-8''%E4%B8%AD", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(artifact.closed)
        artifact.release.assert_called_once()
        self.assertEqual(self.mutate("POST", f"/api/workspaces/{key}/lock").status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 423)
        self.add_user()
        self.login("alice")
        self.assertEqual(self.client.get(path).status_code, 404)
        self.client.cookies.delete(COOKIE)
        self.assertEqual(self.client.get(path).status_code, 401)
        self.broker.archive.assert_awaited_once()

    def test_revoked_account_during_preparation_discards_archive(self):
        self.login()
        artifact = artifact_for(zip_bytes())
        session = self.app.state.sessions.get(self.client.cookies.get(COOKIE))
        async def revoke(*args, **kwargs):
            self.app.state.sessions.revoke_user(session.user_id)
            return artifact
        self.broker.archive = AsyncMock(side_effect=revoke)
        response = self.client.get(f"/api/workspaces/{fixtures.LEGACY}/archive")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(b"PK", response.content)
        self.assertTrue(artifact.closed)


class ArchiveProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.broker = SimpleNamespace(config=SimpleNamespace(data_dir=self.directory.name),
                                      commands=asyncio.Semaphore(1), workspace=AsyncMock(return_value={"running": True}),
                                      names=Mock(return_value={"container": "known-workspace"}))
        self.exports = downloads.ArchiveExports(self.broker)
        self.allowed = True
        self.real_spawn = asyncio.create_subprocess_exec
        self.processes = []
        self.arguments = []

    async def asyncTearDown(self):
        self.exports.close()
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        self.directory.cleanup()

    def authorize(self):
        if not self.allowed:
            raise HTTPException(401, "expired")

    def child(self, script):
        async def spawn(*args, **kwargs):
            self.arguments.append(args)
            process = await self.real_spawn(sys.executable, "-I", "-c", script, **kwargs)
            self.processes.append(process)
            return process
        return patch("backend.archive_download.asyncio.create_subprocess_exec", side_effect=spawn)

    def valid_script(self):
        value = base64.b64encode(zip_bytes()).decode()
        return f"import sys,base64;sys.stdout.buffer.write(base64.b64decode({value!r}))"

    async def test_complete_zip_keeps_one_slot_until_download_closes(self):
        with self.child(self.valid_script()):
            artifact = await self.exports.prepare("workspace", self.authorize)
        self.assertEqual(artifact.file.read(), zip_bytes())
        self.assertTrue(self.exports.slots.locked())
        with self.assertRaises(BrokerError) as error:
            await self.exports.prepare("workspace", self.authorize)
        self.assertEqual(error.exception.status, 429)
        command = self.arguments[0]
        self.assertEqual(command[:5], ("docker", "exec", "--user", "1000:1000", "known-workspace"))
        self.assertIn("/usr/bin/timeout", command)
        self.assertIn("180s", command)
        self.assertNotIn("--privileged", command)
        artifact.close()
        artifact.close()
        self.assertFalse(self.exports.slots.locked())

    async def test_partial_or_invalid_zip_is_never_returned_and_releases_slot(self):
        scripts = ["import sys;sys.stdout.buffer.write(b'partial');sys.stderr.write('untrusted-secret-path');sys.exit(1)",
                   "import sys;sys.stdout.buffer.write(b'not-a-zip')"]
        for script in scripts:
            with self.subTest(script=script), self.child(script):
                with self.assertRaises(BrokerError) as error:
                    await self.exports.prepare("workspace", self.authorize)
            self.assertNotIn("untrusted-secret", str(error.exception))
            self.assertTrue(self.exports.active.closed)
            self.assertFalse(self.exports.slots.locked())

    async def test_oversize_output_is_killed_and_temporary_file_removed(self):
        with self.child("import sys,time;sys.stdout.buffer.write(b'x'*4096);sys.stdout.flush();time.sleep(10)"), patch.object(downloads, "MAX_ZIP_BYTES", 100):
            with self.assertRaises(BrokerError) as error:
                await self.exports.prepare("workspace", self.authorize)
        self.assertEqual(error.exception.status, 413)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertTrue(self.exports.active.closed)
        self.assertFalse(self.exports.slots.locked())

    async def test_cancel_during_worker_output_kills_cli_and_releases_slot(self):
        with self.child("import time;time.sleep(10)"):
            task = asyncio.create_task(self.exports.prepare("workspace", self.authorize))
            for _ in range(100):
                if self.processes:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.processes)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertFalse(self.exports.slots.locked())
        self.assertTrue(self.exports.active.closed)

    async def test_waiting_command_rechecks_permission_and_stopped_workspace_is_rejected(self):
        self.broker.commands = asyncio.Semaphore(0)
        with self.child(self.valid_script()):
            task = asyncio.create_task(self.exports.prepare("workspace", self.authorize))
            await asyncio.sleep(0)
            self.allowed = False
            self.broker.commands.release()
            with self.assertRaises(HTTPException):
                await task
        self.assertFalse(self.processes)
        self.assertFalse(self.exports.slots.locked())
        self.allowed = True
        self.broker.workspace.return_value = {"running": False}
        with self.assertRaises(BrokerError) as error:
            await self.exports.prepare("workspace", self.authorize)
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.exports.slots.locked())


class ArchiveLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_loss_during_transmission_cannot_finish_download(self):
        artifact = artifact_for(b"x" * (downloads.CHUNK_SIZE + 1))
        allowed = True
        def authorize():
            if not allowed:
                raise HTTPException(423, "locked")
        response = downloads.ArchiveResponse(artifact, "project", "a" * 32, authorize)
        iterator = response.body_iterator
        self.assertEqual(len(await anext(iterator)), downloads.CHUNK_SIZE)
        allowed = False
        with self.assertRaises(HTTPException):
            await anext(iterator)
        self.assertTrue(artifact.closed)
        artifact.release.assert_called_once()

    async def test_disconnect_while_preparing_cancels_preparation(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def prepare():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def disconnected():
            return entered.is_set()
        with self.assertRaises(HTTPException) as error:
            await downloads.prepare_connected(SimpleNamespace(is_disconnected=disconnected), prepare)
        self.assertEqual(error.exception.status_code, 499)
        self.assertTrue(cancelled.is_set())

    async def test_download_timeout_always_closes_artifact(self):
        artifact = artifact_for(zip_bytes())
        response = downloads.ArchiveResponse(artifact, "project", "a" * 32, lambda: None)
        async def blocked_send(message):
            await asyncio.Event().wait()
        with patch.object(downloads, "DOWNLOAD_TIMEOUT", 0.01), self.assertRaises(TimeoutError):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), blocked_send)
        self.assertTrue(artifact.closed)
        artifact.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
