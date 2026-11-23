"""Durable user/workspace ownership migration and session isolation."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from backend.auth import Sessions, hash_password
from backend.credentials import normalize_username
from backend.docker_backend import BrokerError, Config, DockerBroker
from multiuser_fixture import legacy_credential_store as CredentialStore


class UserCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.initial = hash_password("original-owner-password")
        cls.other = hash_password("second-account-password")
        cls.replacement = hash_password("replacement-password-test")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def test_legacy_migration_uses_saved_password_and_keeps_stable_owner(self):
        path = Path(self.directory.name) / "auth.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE dashboard_credentials (id INTEGER PRIMARY KEY, password_hash TEXT, revision INTEGER)")
            db.execute("INSERT INTO dashboard_credentials VALUES (1,?,7)", (self.initial,))
        db.close()
        store = CredentialStore(self.directory.name, self.other)
        owner_id = store.legacy_user_id
        owner = store.user_by_username(" OWNER ")
        self.assertEqual(owner["id"], owner_id)
        self.assertEqual(owner["passwordHash"], self.initial)
        self.assertEqual(owner["revision"], 7)
        self.assertEqual(store.users(), [store.public_user(owner)])
        self.assertNotIn("passwordHash", store.users()[0])
        store.close()
        reopened = CredentialStore(self.directory.name, self.replacement)
        try:
            self.assertEqual(reopened.legacy_user_id, owner_id)
            self.assertEqual(reopened.snapshot(), (self.initial, 7))
            self.assertEqual(len(reopened.users()), 1)
        finally:
            reopened.close()

    def test_accounts_are_normalized_unique_and_have_separate_passwords(self):
        store = CredentialStore(self.directory.name, self.initial)
        try:
            created = store.create_user(" Alice_01 ", self.other)
            self.assertEqual(created["username"], "alice_01")
            self.assertEqual(set(created), {"id", "username", "createdAt", "deleting"})
            self.assertFalse(created["deleting"])
            self.assertEqual(store.user_by_username("ALICE_01")["passwordHash"], self.other)
            self.assertEqual(store.user(store.legacy_user_id)["passwordHash"], self.initial)
            with self.assertRaisesRegex(ValueError, "已存在"):
                store.create_user("ALICE_01", self.initial)
            self.assertEqual(len(store.users()), 2)
            self.assertIsNone(store.user("does-not-exist"))
            self.assertIsNone(store.user_by_username("missing"))
        finally:
            store.close()

    def test_invalid_names_and_plain_password_hashes_are_rejected(self):
        store = CredentialStore(self.directory.name, self.initial)
        try:
            for username in ("a", "a" * 33, "a/b", "a b", "账户", "a;id", None, 12):
                with self.subTest(username=username), self.assertRaises(ValueError):
                    store.create_user(username, self.other)
            with self.assertRaises(ValueError):
                store.create_user("valid", "plaintext-password")
            self.assertEqual(len(store.users()), 1)
            self.assertEqual(normalize_username("AbC-1_2"), "abc-1_2")
        finally:
            store.close()

    def test_resets_change_only_selected_account_and_legacy_helpers_stay_synced(self):
        store = CredentialStore(self.directory.name, self.initial)
        try:
            user = store.create_user("second-user", self.other)
            public = store.replace_user_password(user["id"], self.replacement)
            self.assertEqual(public, user)
            self.assertEqual(store.user(user["id"])["revision"], 1)
            self.assertEqual(store.snapshot(), (self.initial, 0))
            store.replace(self.other)
            self.assertEqual(store.user(store.legacy_user_id)["passwordHash"], self.other)
            self.assertEqual(store.user(store.legacy_user_id)["revision"], 1)
            store.replace_user_password(store.legacy_user_id, self.replacement)
            self.assertEqual(store.snapshot(), (self.replacement, 2))
            self.assertEqual(store.user(user["id"])["revision"], 1)
        finally:
            store.close()

    def test_failed_reset_rolls_back_both_legacy_and_user_tables(self):
        store = CredentialStore(self.directory.name, self.initial)
        owner_id = store.legacy_user_id
        connection = store.db
        store.db = Mock(wraps=connection)
        store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        with self.assertRaises(sqlite3.OperationalError):
            store.replace_user_password(owner_id, self.replacement)
        self.assertEqual(store.snapshot(), (self.initial, 0))
        self.assertEqual(store.user(owner_id)["passwordHash"], self.initial)
        store.db.rollback.assert_called_once()
        store.close()
        reopened = CredentialStore(self.directory.name, self.other)
        try:
            self.assertEqual(reopened.snapshot(), (self.initial, 0))
            self.assertEqual(reopened.user(owner_id)["revision"], 0)
        finally:
            reopened.close()

    def test_existing_multiuser_schema_gains_deletion_state_without_reset(self):
        path = Path(self.directory.name) / "auth.sqlite3"
        owner_id = "a" * 32
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE dashboard_credentials (id INTEGER PRIMARY KEY, password_hash TEXT, revision INTEGER)")
            db.execute("INSERT INTO dashboard_credentials VALUES (1,?,9)", (self.other,))
            db.execute("CREATE TABLE dashboard_users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
                       "password_hash TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL)")
            db.execute("INSERT INTO dashboard_users VALUES (?,?,?,9,'yesterday')", (owner_id, "owner", self.other))
            db.execute("CREATE TABLE credential_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO credential_meta VALUES ('legacy_user_id',?)", (owner_id,))
        db.close()
        store = CredentialStore(self.directory.name, self.initial)
        try:
            self.assertEqual(store.legacy_user_id, owner_id)
            self.assertEqual(store.snapshot(), (self.other, 9))
            self.assertFalse(store.user(owner_id)["deleting"])
            self.assertEqual(store.users()[0]["createdAt"], "yesterday")
        finally:
            store.close()

    def test_deletion_pending_survives_restart_blocks_reset_and_is_retryable(self):
        store = CredentialStore(self.directory.name, self.initial)
        alice = store.create_user("alice", self.other)
        try:
            pending = store.begin_user_deletion(alice["id"])
            self.assertTrue(pending["deleting"])
            self.assertNotIn("passwordHash", pending)
            self.assertEqual(store.user(alice["id"])["revision"], 1)
            self.assertEqual(store.begin_user_deletion(alice["id"]), pending)
            self.assertEqual(store.user(alice["id"])["revision"], 1)
            self.assertEqual(store.snapshot(), (self.initial, 0))
        finally:
            store.close()
        reopened = CredentialStore(self.directory.name, self.replacement)
        try:
            self.assertTrue(reopened.user_by_username("alice")["deleting"])
            self.assertTrue(next(user for user in reopened.users() if user["id"] == alice["id"])["deleting"])
            with self.assertRaisesRegex(ValueError, "正在删除"):
                reopened.replace_user_password(alice["id"], self.replacement)
            with self.assertRaisesRegex(ValueError, "已存在"):
                reopened.create_user("alice", self.replacement)
            self.assertTrue(reopened.delete_user(alice["id"]))
            self.assertFalse(reopened.delete_user(alice["id"]))
            self.assertIsNone(reopened.user_by_username("alice"))
            self.assertEqual(len(reopened.users()), 1)
        finally:
            reopened.close()

    def test_unmarked_account_cannot_be_deleted(self):
        store = CredentialStore(self.directory.name, self.initial)
        try:
            with self.assertRaisesRegex(ValueError, "必须先"):
                store.delete_user(store.legacy_user_id)
            self.assertFalse(store.user(store.legacy_user_id)["deleting"])
            self.assertFalse(store.delete_user("missing"))
            with self.assertRaisesRegex(ValueError, "不存在"):
                store.begin_user_deletion("missing")
        finally:
            store.close()

    def test_owner_deletion_is_durable_and_new_same_username_gets_new_identity(self):
        store = CredentialStore(self.directory.name, self.initial)
        owner_id = store.legacy_user_id
        try:
            store.begin_user_deletion(owner_id)
            self.assertEqual(store.snapshot(), (self.initial, 1))
        finally:
            store.close()
        pending = CredentialStore(self.directory.name, self.other)
        try:
            self.assertTrue(pending.user(owner_id)["deleting"])
            self.assertTrue(pending.delete_user(owner_id))
            self.assertTrue(pending.legacy_user_deleted)
        finally:
            pending.close()
        empty = CredentialStore(self.directory.name, self.replacement)
        try:
            self.assertEqual(empty.legacy_user_id, owner_id)
            self.assertTrue(empty.legacy_user_deleted)
            self.assertEqual(empty.users(), [])
            self.assertIsNone(empty.user_by_username("owner"))
            with self.assertRaisesRegex(ValueError, "不存在"):
                empty.replace(self.other)
            replacement_owner = empty.create_user("owner", self.other)
            self.assertNotEqual(replacement_owner["id"], owner_id)
            self.assertFalse(replacement_owner["deleting"])
        finally:
            empty.close()
        reopened = CredentialStore(self.directory.name, self.initial)
        try:
            self.assertEqual(reopened.legacy_user_id, owner_id)
            self.assertEqual(reopened.user_by_username("owner")["id"], replacement_owner["id"])
            self.assertIsNone(reopened.user(owner_id))
        finally:
            reopened.close()

    def test_failed_deletion_begin_rolls_back_owner_revision_and_flag(self):
        store = CredentialStore(self.directory.name, self.initial)
        owner_id = store.legacy_user_id
        store.db = Mock(wraps=store.db)
        store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.begin_user_deletion(owner_id)
            self.assertFalse(store.user(owner_id)["deleting"])
            self.assertEqual(store.user(owner_id)["revision"], 0)
            self.assertEqual(store.snapshot(), (self.initial, 0))
        finally:
            store.close()
        reopened = CredentialStore(self.directory.name, self.other)
        try:
            self.assertFalse(reopened.user(owner_id)["deleting"])
            self.assertEqual(reopened.snapshot(), (self.initial, 0))
        finally:
            reopened.close()

    def test_failed_final_delete_preserves_pending_account_and_retries_atomically(self):
        store = CredentialStore(self.directory.name, self.initial)
        owner_id = store.legacy_user_id
        store.begin_user_deletion(owner_id)
        store.db = Mock(wraps=store.db)
        store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.delete_user(owner_id)
            self.assertTrue(store.user(owner_id)["deleting"])
            self.assertFalse(store.legacy_user_deleted)
            self.assertIsNone(store.db.execute("SELECT value FROM credential_meta WHERE key='legacy_user_deleted'").fetchone())
        finally:
            store.close()
        reopened = CredentialStore(self.directory.name, self.other)
        try:
            self.assertTrue(reopened.user(owner_id)["deleting"])
            self.assertEqual(reopened.user(owner_id)["revision"], 1)
            self.assertTrue(reopened.delete_user(owner_id))
        finally:
            reopened.close()

    def test_batch_deletion_checks_every_target_before_changing_any_account(self):
        store = CredentialStore(self.directory.name, self.initial)
        alice = store.create_user("alice", self.other)
        owner_id = store.legacy_user_id
        try:
            for user_ids in ([alice["id"], owner_id, "missing"], [alice["id"], alice["id"]], [], "owner"):
                with self.subTest(user_ids=user_ids), self.assertRaises(ValueError):
                    store.begin_user_deletions(user_ids)
                for user_id in (owner_id, alice["id"]):
                    self.assertFalse(store.user(user_id)["deleting"])
                    self.assertEqual(store.user(user_id)["revision"], 0)
            self.assertEqual(store.snapshot(), (self.initial, 0))
        finally:
            store.close()

    def test_batch_deletion_commit_failure_rolls_back_all_users_and_owner_cache(self):
        store = CredentialStore(self.directory.name, self.initial)
        alice = store.create_user("alice", self.other)
        owner_id = store.legacy_user_id
        store.db = Mock(wraps=store.db)
        store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.begin_user_deletions([alice["id"], owner_id])
            for user_id in (owner_id, alice["id"]):
                self.assertFalse(store.user(user_id)["deleting"])
                self.assertEqual(store.user(user_id)["revision"], 0)
            self.assertEqual(store.snapshot(), (self.initial, 0))
            self.assertEqual(store.db.execute("SELECT revision FROM dashboard_credentials").fetchone()[0], 0)
        finally:
            store.close()
        reopened = CredentialStore(self.directory.name, self.replacement)
        try:
            self.assertTrue(all(not user["deleting"] for user in reopened.users()))
            self.assertEqual(reopened.snapshot(), (self.initial, 0))
        finally:
            reopened.close()

    def test_batch_deletion_is_durable_and_retry_does_not_increment_pending_users(self):
        store = CredentialStore(self.directory.name, self.initial)
        alice = store.create_user("alice", self.other)
        bob = store.create_user("bob", self.other)
        owner_id = store.legacy_user_id
        try:
            store.begin_user_deletion(alice["id"])
            ids = [alice["id"], owner_id]
            result = store.begin_user_deletions(ids)
            self.assertEqual([user["id"] for user in result], ids)
            self.assertTrue(all(user["deleting"] for user in result))
            self.assertEqual(store.begin_user_deletions(ids), result)
            for user_id in ids:
                self.assertEqual(store.user(user_id)["revision"], 1)
            self.assertFalse(store.user(bob["id"])["deleting"])
            self.assertEqual(store.snapshot(), (self.initial, 1))
        finally:
            store.close()
        reopened = CredentialStore(self.directory.name, self.replacement)
        try:
            self.assertTrue(reopened.user(owner_id)["deleting"])
            self.assertTrue(reopened.user(alice["id"])["deleting"])
            self.assertFalse(reopened.user(bob["id"])["deleting"])
        finally:
            reopened.close()


class IdentitySessionTests(unittest.TestCase):
    def test_revocation_and_workspace_grants_are_identity_scoped(self):
        sessions = Sessions()
        alice = sessions.create("alice")
        another_alice = sessions.create("alice")
        bob = sessions.create("bob")
        admin = sessions.create()
        alice.unlocked_workspaces["project"] = 3
        alice.unlocked_workspaces["another"] = 1
        bob.unlocked_workspaces["project"] = 3
        self.assertEqual(another_alice.unlocked_workspaces, {})
        sessions.lock_workspace("project")
        self.assertEqual(alice.unlocked_workspaces, {"another": 1})
        self.assertEqual(bob.unlocked_workspaces, {})
        sessions.revoke_user("alice")
        self.assertIsNone(sessions.get(alice.token))
        self.assertIsNone(sessions.get(another_alice.token))
        self.assertIs(sessions.get(bob.token), bob)
        self.assertIs(sessions.get(admin.token), admin)

    def test_one_account_cannot_evict_others_with_repeat_signins(self):
        sessions = Sessions()
        bob = sessions.create("bob")
        for _ in range(100):
            sessions.create("alice")
        self.assertIs(sessions.get(bob.token), bob)
        self.assertEqual(len(sessions.sessions), 33)
        for index in range(300):
            sessions.create(str(index))
        self.assertEqual(len(sessions.sessions), 256)


class WorkspaceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("private-workspace-password")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace_id = "a" * 32
        self.owner_id = "b" * 32
        self.other_id = "c" * 32
        with sqlite3.connect(Path(self.directory.name) / "registry.sqlite3") as db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO meta VALUES ('owner','originaldockerowner')")
            db.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("INSERT INTO workspaces VALUES (?,?,?)", (self.workspace_id, "已有工作区", "2026-11-15T00:00:00Z"))
        db.close()
        self.broker = DockerBroker(Config(data_dir=self.directory.name))

    def tearDown(self):
        self.broker.close()
        self.directory.cleanup()

    async def test_legacy_owner_migration_preserves_resource_identity(self):
        self.assertEqual(self.broker.owner, "originaldockerowner")
        names = self.broker.names(self.workspace_id)
        self.broker.assign_legacy_owner(self.owner_id)
        self.assertEqual(self.broker.get(self.workspace_id)["ownerId"], self.owner_id)
        self.assertFalse(self.broker.get(self.workspace_id)["passwordProtected"])
        self.broker.assign_legacy_owner(self.other_id)
        self.assertEqual(self.broker.get(self.workspace_id)["ownerId"], self.owner_id)
        self.assertEqual(self.broker.names(self.workspace_id), names)
        self.assertNotIn("passwordHash", self.broker.get(self.workspace_id))

    async def test_workspace_password_persists_and_revision_invalidates_grants(self):
        self.broker.assign_legacy_owner(self.owner_id)
        self.assertEqual(self.broker.workspace_access(self.workspace_id)["passwordRevision"], 0)
        self.assertEqual(self.broker.set_workspace_password(self.workspace_id, self.password_hash), 1)
        self.assertTrue(self.broker.get(self.workspace_id)["passwordProtected"])
        self.assertEqual(self.broker.workspace_access(self.workspace_id)["passwordHash"], self.password_hash)
        self.assertEqual(self.broker.set_workspace_password(self.workspace_id, None), 2)
        self.assertFalse(self.broker.get(self.workspace_id)["passwordProtected"])
        self.broker.close()
        self.broker = DockerBroker(Config(data_dir=self.directory.name))
        self.assertEqual(self.broker.workspace_access(self.workspace_id),
                         {"ownerId": self.owner_id, "passwordHash": None, "passwordRevision": 2})

    async def test_list_filters_ownership_before_any_docker_inspection(self):
        self.broker.assign_legacy_owner(self.owner_id)
        other_workspace = "d" * 32
        self.broker.db.execute("INSERT INTO workspaces (id,name,created_at,owner_id) VALUES (?,?,?,?)",
                               (other_workspace, "another", "2026-11-16T00:00:00Z", self.other_id))
        self.broker.db.commit()
        self.broker.workspace = AsyncMock(side_effect=lambda value: self.broker.get(value))
        own = await self.broker.list(owner_id=self.owner_id)
        self.assertEqual([item["id"] for item in own], [self.workspace_id])
        self.broker.workspace.assert_awaited_once_with(self.workspace_id)
        self.assertEqual(len(await self.broker.list()), 2)

    async def test_failed_password_commit_leaves_old_access_state(self):
        before = self.broker.workspace_access(self.workspace_id)
        connection = self.broker.db
        self.broker.db = Mock(wraps=connection)
        self.broker.db.commit.side_effect = sqlite3.OperationalError("disk full")
        with self.assertRaises(sqlite3.OperationalError):
            self.broker.set_workspace_password(self.workspace_id, self.password_hash)
        self.assertEqual(self.broker.workspace_access(self.workspace_id), before)
        self.broker.db.rollback.assert_called_once()

    async def test_initial_owner_and_password_exist_before_provisioning(self):
        self.broker.check_engine = AsyncMock()
        self.broker.check_running_limit = AsyncMock()

        async def fail_provision(workspace_id):
            access = self.broker.workspace_access(workspace_id)
            self.assertEqual(access, {"ownerId": self.other_id, "passwordHash": self.password_hash, "passwordRevision": 1})
            raise BrokerError("simulated Docker failure")

        self.broker._start = AsyncMock(side_effect=fail_provision)
        with self.assertRaises(BrokerError):
            await self.broker.create("受保护的新工作区", self.other_id, self.password_hash)
        created_id = self.broker._start.call_args.args[0]
        self.broker.close()
        self.broker = DockerBroker(Config(data_dir=self.directory.name))
        self.broker.assign_legacy_owner(self.owner_id)
        self.assertEqual(self.broker.workspace_access(created_id)["ownerId"], self.other_id)
        self.assertTrue(self.broker.get(created_id)["passwordProtected"])

    async def test_invalid_owner_or_password_never_provisions(self):
        self.broker._start = AsyncMock()
        for owner, password in (("bad-owner", None), (self.owner_id, "plaintext")):
            with self.subTest(owner=owner, password=password), self.assertRaises(BrokerError):
                await self.broker.create("new", owner, password)
        with self.assertRaises(BrokerError):
            self.broker.set_workspace_password("d" * 32, self.password_hash)
        self.broker._start.assert_not_called()
