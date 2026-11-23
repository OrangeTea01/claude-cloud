"""Fresh installations have no default user; legacy accounts remain intact."""
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import hash_password
from backend.credentials import CredentialStore
from backend.docker_backend import Config
from test_multiuser_api import MemoryBroker


ORIGIN = "http://localhost:8000"
ADMIN_PASSWORD = "administrator-secret-new-site"
USER_PASSWORD = "first-created-user-secret"
SEED = hash_password("internal-bootstrap-seed-only")
ADMIN_HASH = hash_password(ADMIN_PASSWORD)
USER_HASH = hash_password(USER_PASSWORD)


class EmptyCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_fresh_database_has_no_default_user_and_remains_empty_on_restart(self):
        for seed in (SEED, USER_HASH):
            store = CredentialStore(self.directory.name, seed)
            try:
                self.assertEqual(store.users(), [])
                self.assertIsNone(store.user_by_username("owner"))
                self.assertIsNone(store.legacy_user_id)
                self.assertFalse(store.legacy_user_deleted)
                self.assertEqual(store.db.execute("SELECT value FROM credential_meta WHERE key='user_bootstrap'").fetchone(),
                                 ("admin_created",))
            finally:
                store.close()

    def test_explicitly_created_users_and_passwords_survive_restart(self):
        store = CredentialStore(self.directory.name, SEED)
        first = store.create_user("alice", USER_HASH)
        store.close()
        store = CredentialStore(self.directory.name, ADMIN_HASH)
        try:
            self.assertEqual(store.users(), [first])
            self.assertIsNone(store.legacy_user_id)
            self.assertEqual(store.user(first["id"])["passwordHash"], USER_HASH)
            self.assertIsNone(store.user_by_username("owner"))
        finally:
            store.close()

    def test_deleting_last_user_never_bootstraps_another_account(self):
        store = CredentialStore(self.directory.name, SEED)
        first = store.create_user("alice", USER_HASH)
        store.begin_user_deletion(first["id"])
        store.delete_user(first["id"])
        store.close()
        store = CredentialStore(self.directory.name, USER_HASH)
        try:
            self.assertEqual(store.users(), [])
            self.assertIsNone(store.legacy_user_id)
            second = store.create_user("alice", ADMIN_HASH)
            self.assertNotEqual(second["id"], first["id"])
        finally:
            store.close()

    def test_owner_is_allowed_only_as_an_explicit_normal_username(self):
        store = CredentialStore(self.directory.name, SEED)
        try:
            owner = store.create_user("owner", USER_HASH)
            store.replace_user_password(owner["id"], ADMIN_HASH)
            self.assertIsNone(store.legacy_user_id)
            self.assertEqual(store.snapshot(), (SEED, 0))
            with self.assertRaisesRegex(ValueError, "不存在"):
                store.replace(USER_HASH)
        finally:
            store.close()

    def test_interrupted_first_initialization_rolls_back_legacy_detection_evidence(self):
        connect = sqlite3.connect
        def interrupted(path):
            connection = connect(path)
            proxy = Mock(wraps=connection)
            def execute(sql, *parameters):
                if sql == "INSERT INTO credential_meta VALUES ('user_bootstrap',?)":
                    raise sqlite3.OperationalError("simulated disk failure")
                return connection.execute(sql, *parameters)
            proxy.execute.side_effect = execute
            return proxy
        with patch("backend.credentials.sqlite3.connect", side_effect=interrupted):
            with self.assertRaises(sqlite3.OperationalError):
                CredentialStore(self.directory.name, SEED)
        with connect(Path(self.directory.name) / "auth.sqlite3") as check:
            self.assertEqual(check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        check.close()
        store = CredentialStore(self.directory.name, SEED)
        try:
            self.assertEqual(store.users(), [])
            self.assertIsNone(store.legacy_user_id)
        finally:
            store.close()

    def test_legacy_single_password_is_adopted_once_with_saved_password(self):
        with sqlite3.connect(Path(self.directory.name) / "auth.sqlite3") as db:
            db.execute("CREATE TABLE dashboard_credentials (id INTEGER PRIMARY KEY, password_hash TEXT, revision INTEGER)")
            db.execute("INSERT INTO dashboard_credentials VALUES (1,?,4)", (USER_HASH,))
        db.close()
        store = CredentialStore(self.directory.name, SEED)
        owner = store.user_by_username("owner")
        self.assertEqual(owner["passwordHash"], USER_HASH)
        self.assertEqual(owner["revision"], 4)
        self.assertEqual(owner["id"], store.legacy_user_id)
        alice = store.create_user("alice", ADMIN_HASH)
        store.close()
        store = CredentialStore(self.directory.name, SEED)
        try:
            self.assertEqual(store.user_by_username("owner"), owner)
            self.assertEqual({item["id"] for item in store.users()}, {owner["id"], alice["id"]})
        finally:
            store.close()

    def test_corrupt_or_conflicting_bootstrap_mode_is_rejected(self):
        store = CredentialStore(self.directory.name, SEED)
        store.db.execute("UPDATE credential_meta SET value='unexpected' WHERE key='user_bootstrap'")
        store.db.commit()
        store.close()
        with self.assertRaisesRegex(ValueError, "bootstrap mode"):
            CredentialStore(self.directory.name, SEED)


class EmptyInstallationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(data_dir=self.directory.name, public_origin=ORIGIN, password_hash=SEED,
                             management_password_hash=ADMIN_HASH, cookie_secure=False)
        self.broker = MemoryBroker()
        self.broker.records.clear()
        self.broker.assign_legacy_owner = Mock(wraps=self.broker.assign_legacy_owner)
        self.browsers = SimpleNamespace(reconcile=AsyncMock(), stop=AsyncMock(), close=AsyncMock())
        self.open()

    def open(self):
        self.app = create_app(self.config, broker=self.broker, login_browsers=self.browsers)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def admin(self):
        response = self.client.post("/api/admin/login", headers={"Origin": ORIGIN}, json={"password": ADMIN_PASSWORD})
        self.assertEqual(response.status_code, 200)
        return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrfToken"]}

    def test_administrator_can_create_first_user_without_any_default_login(self):
        self.broker.assign_legacy_owner.assert_not_called()
        headers = self.admin()
        self.assertEqual(self.client.get("/api/admin/users").json(), {"users": []})
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN},
                                         json={"username": "owner", "password": "internal-bootstrap-seed-only"}).status_code, 401)
        created = self.client.post("/api/admin/users", headers=headers,
                                   json={"username": "alice", "password": USER_PASSWORD, "confirmPassword": USER_PASSWORD})
        self.assertEqual(created.status_code, 201, created.text)
        logged_in = self.client.post("/api/login", headers={"Origin": ORIGIN},
                                     json={"username": "alice", "password": USER_PASSWORD})
        self.assertEqual(logged_in.status_code, 200)
        user_id = logged_in.json()["user"]["id"]
        self.assertEqual(user_id, created.json()["user"]["id"])
        csrf = {"Origin": ORIGIN, "X-CSRF-Token": logged_in.json()["csrfToken"]}
        workspace = self.client.post("/api/workspaces", headers=csrf, json={"name": "first project"})
        self.assertEqual(workspace.status_code, 201, workspace.text)
        self.assertEqual(self.broker.records[workspace.json()["id"]]["ownerId"], user_id)
        self.client.__exit__(None, None, None)
        self.open()
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN},
                                         json={"username": "alice", "password": USER_PASSWORD}).status_code, 200)
        self.broker.assign_legacy_owner.assert_not_called()

    def test_missing_username_is_rejected_before_or_after_user_creation(self):
        headers = self.admin()
        for create in (False, True):
            if create:
                self.client.post("/api/admin/users", headers=headers,
                                 json={"username": "owner", "password": USER_PASSWORD, "confirmPassword": USER_PASSWORD})
            response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"password": USER_PASSWORD})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(USER_PASSWORD, response.text)
            self.assertFalse(self.client.get("/api/session").json()["authenticated"])

    def test_legacy_reset_route_does_not_choose_first_created_user(self):
        headers = self.admin()
        for created in (False, True):
            if created:
                self.client.post("/api/admin/users", headers=headers,
                                 json={"username": "alice", "password": USER_PASSWORD, "confirmPassword": USER_PASSWORD})
            response = self.client.put("/api/admin/user-password", headers=headers,
                                       json={"newPassword": "new-user-password-1234", "confirmPassword": "new-user-password-1234"})
            self.assertEqual(response.status_code, 404)
            self.assertIn("创建用户", response.json()["detail"])
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN},
                                         json={"username": "alice", "password": USER_PASSWORD}).status_code, 200)
