"""HTTP and websocket authorization tests; no real Claude process is launched."""
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import COOKIE
from multiuser_fixture import legacy_app as create_app
from multiuser_fixture import attach_ownership
from backend.auth import hash_password
from backend.docker_backend import BrokerError, Config

ORIGIN = "http://localhost:8000"
PASSWORD = "test-only-password-very-long"
PASSWORD_HASH = hash_password(PASSWORD)
WORKSPACE_ID = "a" * 32


class FakeTerminals:
    def __init__(self, broker):
        self.lock = asyncio.Lock()
        self.items = {}
        self.attach_count = 0
        self.term = SimpleNamespace(input=AsyncMock(), resize=Mock(), detach=Mock())

    async def attach(self, workspace_id):
        self.attach_count += 1
        queue = asyncio.Queue()
        self.queue = queue
        queue.put_nowait({"type": "ready"})
        return self.term, queue

    def active(self, workspace_id):
        return False

    async def shutdown(self):
        pass


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.broker = SimpleNamespace(
            apply_model_policy=AsyncMock(), reconcile_admin_auth=AsyncMock(), shared_auth_status=AsyncMock(return_value={"authenticated": True}),
            list=AsyncMock(return_value=[]),
            close=Mock(),
            status=AsyncMock(return_value={"docker": True, "workerImage": True, "maxWorkspaces": 3}),
            create=AsyncMock(return_value={"id": WORKSPACE_ID, "name": "project", "createdAt": "now", "running": True}),
            stop=AsyncMock(),
        )
        attach_ownership(self.broker, [WORKSPACE_ID])
        self.patch = patch("backend.app.Terminals", FakeTerminals)
        self.patch.start()
        self.browsers = SimpleNamespace(reconcile=AsyncMock(), close=AsyncMock(), stop=AsyncMock(),
            start=AsyncMock(return_value={"active": True}), status=AsyncMock(return_value={"active": False}),
            open_channel=AsyncMock(), release_channel=AsyncMock())
        self.app = create_app(Config(public_origin=ORIGIN, password_hash=PASSWORD_HASH, cookie_secure=False, data_dir=self.directory.name), broker=self.broker, login_browsers=self.browsers)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.directory.cleanup()

    def login(self):
        response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        return response.json()["csrfToken"]

    def test_private_endpoints_require_authentication(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/session").json(), {"authenticated": False})
        for path in ("/api/status", "/api/workspaces", f"/api/workspaces/{WORKSPACE_ID}/files"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.broker.status.assert_not_called()

    def test_login_requires_exact_origin_and_correct_password(self):
        for origin in (None, "https://evil.example", ORIGIN + ".evil.example", ORIGIN + "/"):
            headers = {} if origin is None else {"Origin": origin}
            with self.subTest(origin=origin):
                self.assertEqual(self.client.post("/api/login", headers=headers, json={"username": "owner", "password": PASSWORD}).status_code, 403)
        response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)

    def test_cookie_is_http_only_strict_and_api_is_not_cached(self):
        response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": PASSWORD})
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/", cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_write_requires_csrf_and_origin_and_logout_revokes_cookie(self):
        csrf = self.login()
        for headers in ({}, {"Origin": ORIGIN}, {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example", "X-CSRF-Token": csrf}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/api/workspaces", headers=headers, json={"name": "project"}).status_code, 403)
        self.broker.create.assert_not_called()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        self.assertEqual(self.client.post("/api/workspaces", headers=headers, json={"name": "project"}).status_code, 201)
        old_cookie = self.client.cookies.get(COOKIE)
        self.assertEqual(self.client.post("/api/logout", headers=headers).status_code, 200)
        self.client.cookies.set(COOKIE, old_cookie)
        self.assertEqual(self.client.get("/api/workspaces").status_code, 401)

    def test_validation_response_does_not_reflect_secret_content(self):
        secret = "SECRET_DO_NOT_ECHO_" * 100
        response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("SECRET_DO_NOT_ECHO", response.text)

    def test_body_limit_rejects_oversized_json_before_broker(self):
        csrf = self.login()
        response = self.client.put(f"/api/workspaces/{WORKSPACE_ID}/file", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json={"path": "large", "content": "x" * (16 * 1024 * 1024)})
        self.assertEqual(response.status_code, 413)

    def test_unknown_command_endpoint_is_not_available(self):
        csrf = self.login()
        response = self.client.post("/api/exec", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json={"command": "cat /etc/passwd"})
        self.assertEqual(response.status_code, 404)

    def test_websocket_rejects_unauthenticated_and_cross_origin(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"ws://localhost:8000/api/workspaces/{WORKSPACE_ID}/terminal", headers={"Origin": ORIGIN}):
                pass
        self.login()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"ws://localhost:8000/api/workspaces/{WORKSPACE_ID}/terminal", headers={"Origin": "https://evil.example"}):
                pass
        self.assertEqual(self.app.state.runtime["terminals"].attach_count, 0)







    def test_login_browser_routes_require_session_and_csrf(self):
        path = f"/api/workspaces/{WORKSPACE_ID}/login-browser"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post(path, json={"url": "https://claude.com/cai/oauth/authorize?state=test"}).status_code, 401)
        csrf = self.login()
        for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://evil.example", "X-CSRF-Token": csrf}):
            self.assertEqual(self.client.post(path, headers=headers, json={"url": "https://claude.com/cai/oauth/authorize?state=test"}).status_code, 403)
            self.assertEqual(self.client.post(path + "/stop", headers=headers).status_code, 403)
        self.browsers.start.assert_not_awaited()
        self.browsers.stop.assert_not_awaited()





    def test_removed_user_login_transports_reject_even_valid_credentials(self):
        csrf = self.login()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        path = f"/api/workspaces/{WORKSPACE_ID}"
        self.assertEqual(self.client.get(path + "/login-browser").status_code, 403)
        for endpoint, body in (("/terminal/stop", None), ("/login-browser", {"url": "https://claude.com/cai/oauth/authorize?state=test"}), ("/login-browser/stop", None)):
            self.assertEqual(self.client.post(path + endpoint, headers=headers, json=body).status_code, 403)
        for suffix in ("/terminal", "/login-browser/display"):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("ws://localhost:8000" + path + suffix, headers={"Origin": ORIGIN}):
                    pass
        self.assertEqual(self.app.state.runtime["terminals"].attach_count, 0)
        self.browsers.open_channel.assert_not_awaited()


class StartupReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []
        self.list_count = 0
        self.running = {"id": WORKSPACE_ID, "name": "stale", "createdAt": "before-restart", "running": True}

        async def list_workspaces(owner_id=None):
            self.list_count += 1
            self.events.append("list")
            if self.list_count == 1:
                raise BrokerError("Docker temporarily unavailable", 503)
            return [dict(self.running), {**self.running, "id": "b" * 32, "running": False}]

        async def stop(workspace_id):
            self.events.append("stop:" + workspace_id)

        async def start(workspace_id):
            self.events.append("start:" + workspace_id)
            return dict(self.running)

        async def create(name, owner_id=None, password_hash=None, mode="code"):
            self.events.append("create:" + name)
            return {**self.running, "name": name}

        self.broker = SimpleNamespace(list=AsyncMock(side_effect=list_workspaces), stop=AsyncMock(side_effect=stop), start=AsyncMock(side_effect=start), create=AsyncMock(side_effect=create), close=Mock(), apply_model_policy=AsyncMock(), reconcile_admin_auth=AsyncMock(), shared_auth_status=AsyncMock(return_value={"authenticated": True}))
        attach_ownership(self.broker, [WORKSPACE_ID, "b" * 32])
        self.patch = patch("backend.app.Terminals", FakeTerminals)
        self.patch.start()
        self.browsers = SimpleNamespace(reconcile=AsyncMock(), close=AsyncMock(), stop=AsyncMock())
        self.app = create_app(Config(public_origin=ORIGIN, password_hash=PASSWORD_HASH, cookie_secure=False, data_dir=self.directory.name), broker=self.broker, login_browsers=self.browsers)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        self.assertFalse(self.app.state.runtime["reconciled"])
        login = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": PASSWORD})
        self.csrf = login.json()["csrfToken"]
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.directory.cleanup()

    def test_later_start_stops_stale_running_workspaces_before_starting(self):
        response = self.client.post(f"/api/workspaces/{WORKSPACE_ID}/start", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events, ["list", "list", "stop:" + WORKSPACE_ID, "start:" + WORKSPACE_ID])
        self.assertTrue(self.app.state.runtime["reconciled"])
        self.assertEqual(self.client.post(f"/api/workspaces/{WORKSPACE_ID}/start", headers=self.headers).status_code, 200)
        self.assertEqual(self.broker.list.await_count, 2)
        self.broker.stop.assert_awaited_once_with(WORKSPACE_ID)

    def test_later_create_reconciles_before_provisioning(self):
        response = self.client.post("/api/workspaces", headers=self.headers, json={"name": "new"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.events, ["list", "list", "stop:" + WORKSPACE_ID, "create:new"])

    def test_failed_reconciliation_blocks_start_and_remains_retryable(self):
        original = self.broker.list.side_effect
        self.broker.list.side_effect = BrokerError("Docker still unavailable", 503)
        response = self.client.post(f"/api/workspaces/{WORKSPACE_ID}/start", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.broker.start.assert_not_called()
        self.assertFalse(self.app.state.runtime["reconciled"])
        self.broker.list.side_effect = original
        response = self.client.post(f"/api/workspaces/{WORKSPACE_ID}/start", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.broker.stop.assert_awaited_once_with(WORKSPACE_ID)
        self.assertTrue(self.app.state.runtime["reconciled"])




if __name__ == "__main__":
    unittest.main()
