"""Administrator credential separation and durable user-password changes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import ADMIN_COOKIE, COOKIE
from multiuser_fixture import legacy_app as create_app
from backend.auth import hash_password, verify_password
from multiuser_fixture import legacy_credential_store as CredentialStore
from backend.docker_backend import Config
from multiuser_fixture import attach_ownership

ORIGIN = "http://localhost:8000"
USER_PASSWORD = "initial-user-password-test"
ADMIN_PASSWORD = "fixed-admin-password-test"
NEW_PASSWORD = "changed-user-password-test"
WORKSPACE = "a" * 32


class FakeTerminals:
    def __init__(self, broker):
        self.lock = asyncio.Lock()
        self.items = {}
        self.term = SimpleNamespace(input=AsyncMock(), resize=Mock(), detach=Mock())

    async def attach(self, workspace):
        queue = asyncio.Queue()
        queue.put_nowait({"type": "ready"})
        return self.term, queue

    def active(self, workspace):
        return False

    async def shutdown(self):
        pass


class AdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user_hash = hash_password(USER_PASSWORD)
        cls.admin_hash = hash_password(ADMIN_PASSWORD)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(public_origin=ORIGIN, password_hash=self.user_hash,
                             management_password_hash=self.admin_hash, cookie_secure=False, data_dir=self.temp.name)
        self.broker = SimpleNamespace(list=AsyncMock(return_value=[]), stop=AsyncMock(), close=Mock(),
                                      apply_model_policy=AsyncMock(), reconcile_admin_auth=AsyncMock(), shared_auth_status=AsyncMock(return_value={"authenticated": True}),
                                      status=AsyncMock(return_value={"docker": True}))
        attach_ownership(self.broker, [WORKSPACE])
        self.browsers = SimpleNamespace(reconcile=AsyncMock(), close=AsyncMock(), stop=AsyncMock())
        self.patch = patch("backend.app.Terminals", FakeTerminals)
        self.patch.start()
        self.start()

    def start(self):
        self.app = create_app(self.config, broker=self.broker, login_browsers=self.browsers)
        self.client = TestClient(self.app, base_url=self.config.public_origin)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.temp.cleanup()

    def login_user(self, password=USER_PASSWORD):
        return self.client.post("/api/login", headers={"Origin": self.config.public_origin}, json={"username": "owner", "password": password})

    def login_admin(self):
        response = self.client.post("/api/admin/login", headers={"Origin": self.config.public_origin},
                                    json={"password": ADMIN_PASSWORD})
        self.assertEqual(response.status_code, 200)
        return {"Origin": self.config.public_origin, "X-CSRF-Token": response.json()["csrfToken"]}

    def reset(self, headers, password=NEW_PASSWORD):
        return self.client.put("/api/admin/user-password", headers=headers,
                               json={"newPassword": password, "confirmPassword": password})

    def test_disabled_admin_has_no_password_fallback(self):
        self.config.management_password_hash = ""
        self.assertEqual(self.client.get("/api/admin/session").json(), {"enabled": False, "authenticated": False})
        response = self.client.post("/api/admin/login", headers={"Origin": ORIGIN}, json={"password": USER_PASSWORD})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.reset({"Origin": ORIGIN}).status_code, 503)
        self.assertEqual(self.login_user().status_code, 200)

    def test_admin_and_user_sessions_and_passwords_are_not_interchangeable(self):
        self.assertEqual(self.login_user().status_code, 200)
        self.assertEqual(self.reset({"Origin": ORIGIN}).status_code, 401)
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.client.post("/api/admin/login", headers={"Origin": ORIGIN},
                                          json={"password": USER_PASSWORD}).status_code, 401)
        self.client.cookies.clear()
        headers = self.login_admin()
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/session").json(), {"authenticated": False})
        admin_token = self.client.cookies.get(ADMIN_COOKIE)
        self.assertEqual(self.client.get("/api/status", headers={"Cookie": f"{COOKIE}={admin_token}"}).status_code, 401)
        self.assertEqual(self.login_user(ADMIN_PASSWORD).status_code, 401)
        self.assertTrue(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.reset(headers).status_code, 200)

    def test_admin_mutations_require_exact_origin_and_own_csrf(self):
        for origin in (None, ORIGIN + "/", "https://evil.example"):
            headers = {"Origin": origin} if origin else {}
            self.assertEqual(self.client.post("/api/admin/login", headers=headers,
                                              json={"password": ADMIN_PASSWORD}).status_code, 403)
        headers = self.login_admin()
        self.login_user()
        user_csrf = self.client.get("/api/session").json()["csrfToken"]
        for bad in ({}, {"Origin": ORIGIN}, {**headers, "Origin": ORIGIN + "/"},
                    {**headers, "X-CSRF-Token": user_csrf}):
            with self.subTest(headers=bad):
                self.assertEqual(self.reset(bad).status_code, 403)
                self.assertEqual(self.client.post("/api/admin/logout", headers=bad).status_code, 403)
        self.assertEqual(self.app.state.runtime["credentials"].revision, 0)

    def test_password_change_revokes_user_sessions_and_preserves_admin_and_processes(self):
        self.assertEqual(self.login_user().status_code, 200)
        old_token = self.client.cookies.get(COOKIE)
        another = self.app.state.sessions.create(self.app.state.runtime["credentials"].legacy_user_id)
        headers = self.login_admin()
        self.assertEqual(self.reset(headers).json(), {"ok": True, "sessionsRevoked": True})
        self.assertIsNone(self.app.state.sessions.get(old_token))
        self.assertIsNone(self.app.state.sessions.get(another.token))
        self.assertFalse(self.client.get("/api/session").json()["authenticated"])
        self.assertTrue(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.login_user().status_code, 401)
        self.assertEqual(self.login_user(NEW_PASSWORD).status_code, 200)
        self.broker.stop.assert_not_awaited()
        self.browsers.stop.assert_not_awaited()
        content = (Path(self.temp.name) / "auth.sqlite3").read_bytes()
        self.assertNotIn(NEW_PASSWORD.encode(), content)
        self.assertNotIn(ADMIN_PASSWORD.encode(), content)

    def test_restart_uses_saved_password_instead_of_environment_seed(self):
        headers = self.login_admin()
        self.assertEqual(self.reset(headers).status_code, 200)
        self.client.__exit__(None, None, None)
        self.start()
        self.assertEqual(self.login_user().status_code, 401)
        self.assertEqual(self.login_user(NEW_PASSWORD).status_code, 200)
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        self.login_admin()

    def test_validation_errors_never_reflect_passwords(self):
        headers = self.login_admin()
        secret = "never-reflect-this-secret"
        invalid = ({"newPassword": "short", "confirmPassword": "short"},
                   {"newPassword": secret, "confirmPassword": secret + "different"},
                   {"newPassword": secret, "confirmPassword": secret, "adminPassword": secret},
                   {"newPassword": [secret], "confirmPassword": secret},
                   {"newPassword": secret * 100, "confirmPassword": secret * 100})
        for body in invalid:
            response = self.client.put("/api/admin/user-password", headers=headers, json=body)
            self.assertIn(response.status_code, (400, 422))
            self.assertNotIn(secret, response.text)
        self.assertEqual(self.app.state.runtime["credentials"].revision, 0)

    def test_user_password_cannot_be_reset_to_admin_password(self):
        self.assertEqual(self.login_user().status_code, 200)
        token = self.client.cookies.get(COOKIE)
        headers = self.login_admin()
        response = self.reset(headers, ADMIN_PASSWORD)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(ADMIN_PASSWORD, response.text)
        self.assertEqual(self.app.state.runtime["credentials"].revision, 0)
        self.assertIsNotNone(self.app.state.sessions.get(token))

    def test_store_failure_preserves_old_password_sessions_and_hides_details(self):
        self.login_user()
        token = self.client.cookies.get(COOKIE)
        headers = self.login_admin()
        with patch.object(self.app.state.runtime["credentials"], "replace",
                          side_effect=sqlite3.OperationalError("sensitive raw sql or hash")):
            response = self.reset(headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive", response.text)
        self.assertIsNotNone(self.app.state.sessions.get(token))
        self.assertEqual(self.login_user().status_code, 200)
        self.assertEqual(self.login_user(NEW_PASSWORD).status_code, 401)

    def test_old_login_in_flight_cannot_recreate_revoked_session(self):
        headers = self.login_admin()
        started, resume = threading.Event(), threading.Event()
        real_verify = verify_password
        def delayed(password, encoded):
            if password == USER_PASSWORD:
                started.set()
                if not resume.wait(5):
                    raise RuntimeError("test verifier gate timed out")
            return real_verify(password, encoded)
        with patch("backend.app.verify_password", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.login_user)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(self.reset(headers).status_code, 200)
            finally:
                resume.set()
            self.assertEqual(pending.result(timeout=5).status_code, 401)
        self.assertEqual(self.app.state.sessions.sessions, {})

    def test_logout_during_hashing_prevents_password_change(self):
        headers = self.login_admin()
        started, resume = threading.Event(), threading.Event()
        real_hash = hash_password
        def delayed(password):
            started.set()
            if not resume.wait(5):
                raise RuntimeError("test hash gate timed out")
            return real_hash(password)
        with patch("backend.app.hash_password", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.reset, headers)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
            finally:
                resume.set()
            self.assertEqual(pending.result(timeout=5).status_code, 401)
        self.assertEqual(self.app.state.runtime["credentials"].revision, 0)

    def test_admin_login_rate_limit_is_independent_from_user_limit(self):
        for _ in range(8):
            response = self.client.post("/api/admin/login", headers={"Origin": ORIGIN}, json={"password": "wrong"})
            self.assertEqual(response.status_code, 401)
        with patch("backend.app.verify_password") as verifier:
            self.assertEqual(self.client.post("/api/admin/login", headers={"Origin": ORIGIN},
                                              json={"password": ADMIN_PASSWORD}).status_code, 429)
            verifier.assert_not_called()
        self.assertEqual(self.login_user().status_code, 200)

    def test_admin_cookie_attributes_expiry_and_logout(self):
        headers = self.login_admin()
        token = self.client.cookies.get(ADMIN_COOKIE)
        cookie = next(c for c in self.client.cookies.jar if c.name == ADMIN_COOKIE)
        self.assertEqual(cookie.path, "/api/admin")
        response = self.client.get("/api/admin/session")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["csrfToken"], headers["X-CSRF-Token"])
        self.app.state.admin_sessions.get(token).expires = 0
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.reset(headers).status_code, 401)
        headers = self.login_admin()
        self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])

    def test_https_admin_cookie_is_scoped_secure_and_short_lived(self):
        self.client.__exit__(None, None, None)
        self.config.public_origin = "https://admin.test"
        self.config.cookie_secure = True
        self.start()
        response = self.client.post("/api/admin/login", headers={"Origin": self.config.public_origin},
                                    json={"password": ADMIN_PASSWORD})
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("httponly", "secure", "samesite=strict", "path=/api/admin", "max-age=1800"):
            self.assertIn(attribute, cookie)

    def test_user_login_cannot_access_removed_claude_terminal(self):
        self.login_user()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"ws://localhost:8000/api/workspaces/{WORKSPACE}/terminal", headers={"Origin": ORIGIN}):
                pass
        self.app.state.runtime["terminals"].term.input.assert_not_awaited()


class CredentialStoreTests(unittest.TestCase):
    def test_failed_commit_rolls_back_and_restart_keeps_previous_credential(self):
        initial, replacement = hash_password(USER_PASSWORD), hash_password(NEW_PASSWORD)
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(directory, initial)
            connection = store.db
            store.db = Mock(wraps=connection)
            store.db.commit.side_effect = sqlite3.OperationalError("disk full")
            with self.assertRaises(sqlite3.OperationalError):
                store.replace(replacement)
            self.assertEqual(store.snapshot(), (initial, 0))
            store.db.rollback.assert_called_once()
            store.close()
            reopened = CredentialStore(directory, replacement)
            try:
                self.assertEqual(reopened.snapshot(), (initial, 0))
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
