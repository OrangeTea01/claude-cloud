"""Real authentication/ownership routes with an isolated in-memory Docker fixture."""
import asyncio
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.app import COOKIE, ADMIN_COOKIE
from multiuser_fixture import legacy_app as create_app
from backend.auth import hash_password
from backend.docker_backend import Config, BrokerError, DockerBroker

ORIGIN = "http://localhost:8000"
USER_PASSWORD = "private-user-password-123"
ADMIN_PASSWORD = "private-admin-password-456"
LOCK_PASSWORD = "workspace-pass-789"
USER_HASH, ADMIN_HASH = hash_password(USER_PASSWORD), hash_password(ADMIN_PASSWORD)
LEGACY = "a" * 32


class MemoryBroker:
    def __init__(self):
        self.records = {LEGACY: {"id": LEGACY, "name": "existing", "createdAt": "now", "ownerId": "",
                                  "passwordHash": None, "passwordRevision": 0, "running": False}}
        self.fail_delete = set()
        self.deleted = []
        self.file_calls = []
        self.apply_model_policy = AsyncMock()
        self.reconcile_admin_auth = AsyncMock()
        self.shared_auth_status = AsyncMock(return_value={"authenticated": True})
        self.chat_command = AsyncMock()
        self.close = Mock()

    def assign_legacy_owner(self, user_id):
        for item in self.records.values():
            if not item["ownerId"]:
                item["ownerId"] = user_id

    def raw(self, workspace_id):
        if workspace_id not in self.records:
            raise BrokerError("工作区不存在", 404)
        return self.records[workspace_id]

    def get(self, workspace_id):
        item = self.raw(workspace_id)
        return {**{key: item[key] for key in ("id", "name", "createdAt", "ownerId", "running")},
                "passwordProtected": bool(item["passwordHash"]), "mode": item.get("mode", "code")}

    def workspace_access(self, workspace_id):
        return {key: self.raw(workspace_id)[key] for key in ("ownerId", "passwordHash", "passwordRevision")}

    def set_workspace_password(self, workspace_id, encoded):
        item = self.raw(workspace_id)
        item["passwordHash"] = encoded
        item["passwordRevision"] += 1
        return item["passwordRevision"]

    async def workspace(self, workspace_id):
        return self.get(workspace_id)

    async def list(self, owner_id=None):
        return [self.get(key) for key, item in self.records.items() if owner_id is None or item["ownerId"] == owner_id]

    async def create(self, name, owner_id=None, password_hash=None, mode="code"):
        key = uuid.uuid4().hex
        self.records[key] = {"id": key, "name": name, "createdAt": "now", "ownerId": owner_id,
                             "passwordHash": password_hash, "passwordRevision": int(bool(password_hash)), "running": True, "mode": mode}
        return self.get(key)

    async def start(self, workspace_id):
        self.raw(workspace_id)["running"] = True
        return self.get(workspace_id)

    async def stop(self, workspace_id):
        self.raw(workspace_id)["running"] = False
        return self.get(workspace_id)

    async def delete(self, workspace_id):
        if workspace_id in self.fail_delete:
            raise BrokerError("fixture failure", 503)
        self.raw(workspace_id)
        self.records.pop(workspace_id)
        self.deleted.append(workspace_id)

    async def files(self, workspace_id, operation, path, **kwargs):
        self.raw(workspace_id)
        self.file_calls.append((workspace_id, operation, path))
        return {"path": path, "content": "private contents", "encoding": "utf-8", "size": 16, "entries": []}


class MultiuserApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.broker = MemoryBroker()
        self.browsers = SimpleNamespace(reconcile=AsyncMock(), stop=AsyncMock(), close=AsyncMock())
        self.app = create_app(Config(data_dir=self.directory.name, public_origin=ORIGIN,
                                    password_hash=USER_HASH, management_password_hash=ADMIN_HASH, cookie_secure=False),
                              broker=self.broker, login_browsers=self.browsers)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        self.runtime = self.app.state.runtime

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def login(self, username="owner", password=USER_PASSWORD):
        response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrfToken"]
        return response.json()

    def admin(self):
        response = self.client.post("/api/admin/login", headers={"Origin": ORIGIN}, json={"password": ADMIN_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.admin_csrf = response.json()["csrfToken"]

    def mutate(self, method, path, body=None, admin=False):
        return self.client.request(method, path, headers={"Origin": ORIGIN, "X-CSRF-Token": self.admin_csrf if admin else self.csrf}, json=body)

    def add_user(self, username="alice"):
        self.admin()
        response = self.mutate("POST", "/api/admin/users", {"username": username, "password": USER_PASSWORD, "confirmPassword": USER_PASSWORD}, admin=True)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"]

    def create_workspace(self, password=""):
        response = self.mutate("POST", "/api/workspaces", {"name": "my project", "password": password})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_migration_owner_and_account_private_lists(self):
        owner = self.login()["user"]
        self.assertEqual(self.client.get("/api/workspaces").json()["workspaces"][0]["id"], LEGACY)
        self.assertEqual(self.broker.workspace_access(LEGACY)["ownerId"], owner["id"])
        self.add_user()
        self.login("alice")
        self.assertEqual(self.client.get("/api/workspaces").json(), {"workspaces": []})
        mine = self.create_workspace()
        self.assertEqual(self.client.get("/api/workspaces").json()["workspaces"][0]["id"], mine["id"])
        self.login()
        self.assertEqual([item["id"] for item in self.client.get("/api/workspaces").json()["workspaces"]], [LEGACY])

    def test_every_private_transport_rejects_another_owner(self):
        self.add_user()
        self.login("alice")
        routes = [("GET", "/files", None), ("GET", "/file?path=a", None), ("GET", "/chat", None),
                  ("PUT", "/file", {"path": "x", "content": "x"}), ("POST", "/attachments", {"name": "x", "content": "eA=="}),
                  ("POST", "/directory", {"path": "a"}), ("DELETE", "/file?path=a", None),
                  ("POST", "/start", None), ("POST", "/stop", None), ("DELETE", "", None),
                  ("POST", "/chat/message", {"text": "read files"}), ("PUT", "/chat/model", {"model": "sonnet"}),
                  ("POST", "/chat/permissions/req", {"allow": True}), ("POST", "/chat/stop", None),
                  ("POST", "/unlock", {"password": LOCK_PASSWORD}), ("POST", "/lock", None),
                  ("PUT", "/password", {"newPassword": LOCK_PASSWORD, "confirmPassword": LOCK_PASSWORD})]
        for method, suffix, body in routes:
            with self.subTest(method=method, suffix=suffix):
                response = self.mutate(method, f"/api/workspaces/{LEGACY}{suffix}", body)
                self.assertEqual(response.status_code, 404, response.text)
        self.assertFalse(self.broker.file_calls)
        self.assertFalse(self.broker.deleted)
        self.broker.chat_command.assert_not_called()

    def test_workspace_lock_blocks_reads_and_writes_until_correct_unlock(self):
        self.login()
        item = self.create_workspace(LOCK_PASSWORD)
        path = f"/api/workspaces/{item['id']}"
        self.assertFalse(item["locked"])
        self.assertEqual(self.mutate("POST", path + "/lock").status_code, 200)
        for method, suffix, body in [("GET", "/chat", None), ("GET", "/file?path=x", None),
                                     ("POST", "/attachments", {"name": "x", "content": "eA=="}),
                                     ("POST", "/chat/message", {"text": "do"}), ("DELETE", "", None)]:
            self.assertEqual(self.mutate(method, path + suffix, body).status_code, 423)
        self.assertEqual(self.mutate("POST", path + "/unlock", {"password": "wrong"}).status_code, 403)
        result = self.mutate("POST", path + "/unlock", {"password": LOCK_PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["locked"])
        self.assertEqual(self.client.get(path + "/file?path=x").status_code, 200)
        self.login()
        self.assertEqual(self.client.get(path + "/chat").status_code, 423)

    def test_workspace_password_change_revokes_other_sessions_not_running_task(self):
        self.login()
        item = self.create_workspace(LOCK_PASSWORD)
        first = self.app.state.sessions.get(self.client.cookies.get(COOKIE))
        self.client.cookies.delete(COOKIE)  # Simulate another device, not account switching.
        self.login()
        path = f"/api/workspaces/{item['id']}"
        self.mutate("POST", path + "/unlock", {"password": LOCK_PASSWORD})
        response = self.mutate("PUT", path + "/password", {"currentPassword": LOCK_PASSWORD,
                               "newPassword": "new-workspace-password", "confirmPassword": "new-workspace-password"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(item["id"], first.unlocked_workspaces)
        self.assertTrue(self.broker.raw(item["id"])["running"])
        self.assertEqual(self.mutate("PUT", path + "/password", {"currentPassword": LOCK_PASSWORD}).status_code, 403)
        self.assertEqual(self.mutate("PUT", path + "/password", {"currentPassword": "new-workspace-password"}).status_code, 200)
        self.login()
        self.assertEqual(self.client.get(path + "/chat").status_code, 200)

    def test_account_reset_only_revokes_target_and_users_cannot_admin(self):
        alice = self.add_user()
        owner = self.login()["user"]
        owner_token = self.client.cookies.get(COOKIE)
        self.client.cookies.delete(COOKIE)  # Keep the first device's owner session alive.
        self.login("alice")
        alice_token = self.client.cookies.get(COOKIE)
        self.client.cookies.delete(ADMIN_COOKIE)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 401)
        self.admin()
        response = self.mutate("PUT", f"/api/admin/users/{alice['id']}/password",
                               {"newPassword": "new-alice-password", "confirmPassword": "new-alice-password"}, admin=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.app.state.sessions.get(alice_token))
        self.assertEqual(self.app.state.sessions.get(owner_token).user_id, owner["id"])
        self.assertEqual(self.client.get("/api/workspaces").status_code, 401)
        self.login("alice", "new-alice-password")

    def test_admin_clear_all_requires_confirmation_and_reports_partial_failure(self):
        self.add_user()
        self.login("alice")
        own = self.create_workspace(LOCK_PASSWORD)
        metadata = self.client.get("/api/admin/workspaces")
        self.assertEqual(metadata.status_code, 200)
        self.assertNotIn("passwordHash", metadata.text)
        self.assertEqual(len(metadata.json()["workspaces"]), 2)
        self.assertEqual(self.mutate("POST", "/api/admin/workspaces/delete-all", {"confirmation": "no"}, admin=True).status_code, 400)
        self.assertFalse(self.broker.deleted)
        self.broker.fail_delete.add(own["id"])
        result = self.mutate("POST", "/api/admin/workspaces/delete-all", {"confirmation": "DELETE_ALL_WORKSPACES"}, admin=True)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["deleted"], 1)
        self.assertEqual(result.json()["remaining"], 1)
        self.assertEqual(result.json()["failed"][0]["id"], own["id"])
        self.broker.fail_delete.clear()
        result = self.mutate("POST", "/api/admin/workspaces/delete-all", {"confirmation": "DELETE_ALL_WORKSPACES"}, admin=True)
        self.assertEqual(result.json(), {"deleted": 1, "failed": [], "remaining": 0})
        self.assertEqual(len(self.client.get("/api/admin/users").json()["users"]), 2)
        self.assertNotIn("admin-auth", self.broker.deleted)

    def test_locked_during_async_file_read_discards_private_response(self):
        self.login()
        item = self.create_workspace(LOCK_PASSWORD)
        session = self.app.state.sessions.get(self.client.cookies.get(COOKIE))
        async def read_then_lock(*args, **kwargs):
            session.unlocked_workspaces.clear()
            return {"content": "DO_NOT_RETURN_PRIVATE_CONTENT"}
        with patch.object(self.broker, "files", side_effect=read_then_lock):
            response = self.client.get(f"/api/workspaces/{item['id']}/file?path=x")
        self.assertEqual(response.status_code, 423, response.text)
        self.assertNotIn("DO_NOT_RETURN_PRIVATE_CONTENT", response.text)

    def test_password_reset_during_verification_cannot_mint_old_session(self):
        original = self.login()["user"]
        def reset_during_check(password, encoded):
            self.runtime["credentials"].replace_user_password(original["id"], USER_HASH)
            return True
        # Run reset on the server event loop before the delayed verification returns.
        async def mock_to_thread(function, *args):
            return reset_during_check(*args)
        with patch("backend.app.asyncio.to_thread", side_effect=mock_to_thread):
            response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": USER_PASSWORD})
        self.assertEqual(response.status_code, 401)

    def test_history_delete_failure_retains_workspace_for_retry(self):
        self.admin()
        with patch.object(self.runtime["chats"], "forget", side_effect=OSError("storage failure")):
            result = self.mutate("POST", "/api/admin/workspaces/delete-all", {"confirmation": "DELETE_ALL_WORKSPACES"}, admin=True)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["remaining"], 1)
        self.assertIn(LEGACY, self.broker.records)
        self.assertFalse(self.broker.deleted)
        result = self.mutate("POST", "/api/admin/workspaces/delete-all", {"confirmation": "DELETE_ALL_WORKSPACES"}, admin=True)
        self.assertEqual(result.json()["remaining"], 0)

    def test_account_admin_mutations_require_csrf_and_no_secrets_in_list(self):
        self.admin()
        body = {"username": "alice", "password": USER_PASSWORD, "confirmPassword": USER_PASSWORD}
        for headers in ({}, {"Origin": "https://other.example", "X-CSRF-Token": self.admin_csrf}):
            self.assertEqual(self.client.post("/api/admin/users", headers=headers, json=body).status_code, 403)
        result = self.client.get("/api/admin/users")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("password", result.text.lower())
        self.assertNotIn("scrypt", result.text)


class LateFileAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_inspection_cannot_execute_after_authorization_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = DockerBroker(Config(data_dir=directory))
            broker.workspace = AsyncMock(return_value={"running": True})
            broker.command = AsyncMock()
            try:
                def denied():
                    raise BrokerError("locked", 423)
                with self.assertRaises(BrokerError):
                    await broker.files(LEGACY, "write", "a.txt", content="x", authorize=denied)
                broker.command.assert_not_called()
            finally:
                broker.close()

    async def test_waiting_docker_command_rechecks_grant_before_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = DockerBroker(Config(data_dir=directory))
            broker.commands = asyncio.Semaphore(0)
            try:
                def denied():
                    raise BrokerError("expired", 401)
                with patch("backend.docker_backend.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                    task = asyncio.create_task(broker.command("exec", "fixture", authorize=denied))
                    await asyncio.sleep(0)
                    broker.commands.release()
                    with self.assertRaises(BrokerError):
                        await task
                    spawn.assert_not_called()
            finally:
                broker.close()
