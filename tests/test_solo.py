"""Fresh private installations share one authenticated site session, never public admin access."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.account_browser import AccountBrowser, RESOURCE_ID
from backend.app import create_app, COOKIE, ADMIN_COOKIE
from backend.auth import hash_password, verify_password
from backend.credentials import CredentialStore, InstallationModeError
from backend.docker_backend import Config
from test_multiuser_api import MemoryBroker

ORIGIN = "http://localhost:8000"
PASSWORD = "private-website-password-123"
NEW_PASSWORD = "private-website-password-456"
SITE_HASH = hash_password(PASSWORD)


class SoloCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_single_identity_and_password_survive_restart_and_changed_seed(self):
        store = CredentialStore(self.directory.name, SITE_HASH, app_mode="solo")
        identity = store.solo_user_id
        self.assertEqual(len(store.users()), 1)
        self.assertIsNone(store.legacy_user_id)
        self.assertIsNone(store.user_by_username("owner"))
        new_hash = hash_password(NEW_PASSWORD)
        store.replace_user_password(identity, new_hash)
        with self.assertRaises(ValueError): store.create_user("another", SITE_HASH)
        with self.assertRaises(ValueError): store.begin_user_deletion(identity)
        store.close()
        store = CredentialStore(self.directory.name, SITE_HASH, app_mode="solo")
        self.assertEqual(store.solo_user_id, identity)
        self.assertEqual(store.user(identity)["passwordHash"], new_hash)
        self.assertEqual(store.snapshot(), (new_hash, 1))
        store.close()

    def test_modes_cannot_be_switched_on_existing_data_even_without_accounts(self):
        store = CredentialStore(self.directory.name, SITE_HASH)
        store.close()
        before = (Path(self.directory.name) / "auth.sqlite3").read_bytes()
        with self.assertRaises(InstallationModeError):
            CredentialStore(self.directory.name, SITE_HASH, app_mode="solo")
        self.assertEqual((Path(self.directory.name) / "auth.sqlite3").read_bytes(), before)
        with tempfile.TemporaryDirectory() as other:
            store = CredentialStore(other, SITE_HASH, app_mode="solo")
            store.close()
            with self.assertRaises(InstallationModeError): CredentialStore(other, SITE_HASH)

    def test_orphan_registry_and_corrupt_identity_are_not_silently_adopted(self):
        registry = Path(self.directory.name) / "registry.sqlite3"
        registry.write_bytes(b"existing workspace registry")
        with self.assertRaises(InstallationModeError):
            CredentialStore(self.directory.name, SITE_HASH, app_mode="solo")
        self.assertEqual(registry.read_bytes(), b"existing workspace registry")
        with tempfile.TemporaryDirectory() as other:
            store = CredentialStore(other, SITE_HASH, app_mode="solo")
            store.db.execute("DELETE FROM credential_meta WHERE key='solo_user_id'")
            store.db.commit(); store.close()
            with self.assertRaises(InstallationModeError): CredentialStore(other, SITE_HASH, app_mode="solo")


class SoloApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=self.directory.name, public_origin=ORIGIN,
                             password_hash=SITE_HASH, cookie_secure=False, app_mode="solo")
        self.broker = MemoryBroker(); self.broker.records.clear()
        self.broker.status = AsyncMock(return_value={"docker": True, "workerImage": True})
        self.browsers = SimpleNamespace(reconcile=AsyncMock(), stop=AsyncMock(), close=AsyncMock())
        self.open()

    def open(self):
        self.app = create_app(self.config, broker=self.broker, login_browsers=self.browsers)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        self.runtime = self.app.state.runtime

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def login(self, password=PASSWORD, client=None, path="/api/login"):
        client = client or self.client
        response = client.post(path, headers={"Origin": ORIGIN}, json={"password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrfToken"]}

    def workspace(self, headers):
        result = self.client.post("/api/workspaces", headers=headers, json={"name": "我的项目"})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["id"]

    def install_browser(self):
        self.active = False
        async def start(_resource, _url):
            self.active = True
            return {"active": True, "expiresAt": datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()}
        async def stop(_resource): self.active = False
        self.display = SimpleNamespace(start=AsyncMock(side_effect=start), stop=AsyncMock(side_effect=stop),
            navigate=AsyncMock(), status=AsyncMock(side_effect=lambda _: {"active": self.active}),
            close=AsyncMock(), open_channel=AsyncMock(), release_channel=AsyncMock())
        service = AccountBrowser(self.runtime, self.app.state.admin_sessions,
                                 facade=SimpleNamespace(prepare=AsyncMock(), cleanup_network=AsyncMock()), browsers=self.display)
        self.runtime["account_browser"] = service
        return service

    def test_one_password_session_has_administration_and_private_workspace_access(self):
        self.assertEqual(self.client.get("/api/session").json(), {"authenticated": False, "mode": "solo"})
        for path in ("/api/workspaces", "/api/admin/model-policy", "/api/admin/claude-account-browser"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"password": "bad"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"password": PASSWORD, "username": "owner"}).status_code, 422)
        headers = self.login()
        session = self.client.get("/api/session").json()
        self.assertTrue(session["admin"]); self.assertEqual(session["mode"], "solo")
        self.assertEqual(set(session["user"]), {"id"})
        self.assertIsNone(self.client.cookies.get(ADMIN_COOKIE))
        self.assertIs(self.app.state.sessions, self.app.state.admin_sessions)
        self.assertEqual(self.client.get("/api/admin/session").json()["csrfToken"], session["csrfToken"])
        self.assertEqual(self.client.get("/api/admin/model-policy").status_code, 200)
        key = self.workspace(headers)
        self.assertEqual(self.broker.workspace_access(key)["ownerId"], session["user"]["id"])
        view = self.client.get("/api/workspaces").json()["workspaces"][0]
        self.assertFalse(view["locked"]); self.assertFalse(view["passwordProtected"])
        self.assertTrue(self.client.get("/api/status").json()["claudeAuthenticated"])

    def test_removed_multiuser_and_workspace_password_endpoints_cannot_mutate(self):
        headers = self.login(); key = self.workspace(headers)
        paths = ["/api/admin/users", "/api/admin/users/delete-batch", "/api/admin/users/something/delete",
                 "/api/admin/quotas", "/api/admin/quotas/automatic", "/api/admin/quotas/distribute",
                 "/api/admin/user-password", *[f"/api/workspaces/{key}/{name}" for name in ("lock", "unlock", "password")]]
        for path in paths:
            for method in ("GET", "POST", "PUT"):
                self.assertEqual(self.client.request(method, path, headers=headers, json={}).status_code, 404, path)
        self.assertEqual(self.client.post("/api/workspaces", headers=headers, json={"name": "locked", "password": "work-secret"}).status_code, 422)
        self.assertEqual(self.client.portal.call(lambda: len(self.runtime["credentials"].users())), 1)
        self.assertIsNone(self.broker.workspace_access(key)["passwordHash"])

    def test_admin_mutations_still_require_same_origin_and_current_csrf(self):
        headers = self.login()
        for bad in ({}, {"Origin": ORIGIN}, {**headers, "Origin": "https://evil.example"}, {**headers, "X-CSRF-Token": "old"}):
            response = self.client.put("/api/admin/model-policy", headers=bad, json={"fableAllowed": True})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.runtime["credentials"].fable_allowed)

    def test_password_change_is_durable_revokes_all_devices_and_keeps_project(self):
        headers = self.login(); key = self.workspace(headers)
        other = TestClient(self.app, base_url=ORIGIN)
        other.portal = self.client.portal
        self.login(client=other)
        body = {"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD, "confirmPassword": NEW_PASSWORD}
        self.assertEqual(self.client.post("/api/settings/password", json=body).status_code, 403)
        self.assertEqual(self.client.post("/api/settings/password", headers=headers, json={**body, "currentPassword": "wrong"}).status_code, 400)
        result = self.client.post("/api/settings/password", headers=headers, json=body)
        self.assertEqual(result.json(), {"ok": True, "sessionsRevoked": True})
        self.assertFalse(other.get("/api/session").json()["authenticated"])
        self.assertEqual(other.get("/api/admin/model-policy").status_code, 401)
        self.assertIn(key, self.broker.records)
        self.client.__exit__(None, None, None)
        self.open()
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"password": PASSWORD}).status_code, 401)
        self.login(NEW_PASSWORD, path="/api/admin/login")
        self.assertEqual(self.client.get("/api/workspaces").json()["workspaces"][0]["id"], key)

    def test_failed_password_save_keeps_old_password_and_sessions(self):
        headers = self.login()
        with patch.object(self.runtime["credentials"], "replace_user_password", side_effect=OSError("disk")):
            result = self.client.post("/api/settings/password", headers=headers, json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD, "confirmPassword": NEW_PASSWORD})
        self.assertEqual(result.status_code, 503)
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        self.assertTrue(verify_password(PASSWORD, self.client.portal.call(self.runtime["credentials"].snapshot)[0]))

    def test_no_site_quota_allocation_is_required_or_enforced(self):
        headers = self.login(); first = self.workspace(headers); second = self.workspace(headers)
        user_id = self.runtime["credentials"].solo_user_id
        control = self.runtime["usage"]
        def limited():
            self.runtime["quotas"].set_limits(user_id, 0, 0)
            self.runtime["shares"].configure([user_id], official_snapshot=self.runtime["official"].snapshot())
            self.runtime["chats"].get(first).phase = "thinking"
            control.admit(second)
            return control.quota(second)
        self.assertFalse(self.client.portal.call(limited)["exhausted"])
        report = self.client.get("/api/account/usage").json()
        self.assertIsNone(report["quota"]); self.assertEqual(report["mode"], "solo")
        self.assertIn("official", report); self.assertEqual(report["activeTasks"][0]["workspaceId"], first)

    def test_registration_display_uses_unified_cookie_and_revokes_on_logout(self):
        service = self.install_browser(); headers = self.login()
        prefix = "/api/admin/claude-account-browser"
        result = self.client.post(prefix, headers=headers, json={"purpose": "billing"})
        self.assertEqual(result.status_code, 200, result.text)
        lease = result.json()["browserId"]
        proc = SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))
        async def channel(_):
            proc.stdout = asyncio.StreamReader(); proc.stdout.feed_data(b"RFB 003.008\n")
            return proc
        self.display.open_channel.side_effect = channel
        with self.client.websocket_connect("ws://localhost:8000"+prefix+"/display", headers={"Origin": ORIGIN}) as ws:
            ws.send_json({"type": "auth", "csrfToken": headers["X-CSRF-Token"], "browserId": lease})
            self.assertEqual(ws.receive_bytes(), b"RFB 003.008\n")
            self.app.state.sessions.revoke_all()
            ws.send_bytes(b"late input")
            with self.assertRaises(WebSocketDisconnect): ws.receive_bytes()
        proc.stdin.write.assert_not_called()
        headers = self.login()
        self.client.post(prefix, headers=headers, json={"purpose": "register"})
        self.assertTrue(self.active)
        self.assertEqual(self.client.post("/api/logout", headers=headers).status_code, 200)
        self.assertFalse(self.active); self.assertIsNone(service.owner)


if __name__ == "__main__": unittest.main()
