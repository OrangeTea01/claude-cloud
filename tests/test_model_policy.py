"""Durable administrator model policy and in-flight revocation boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from starlette.websockets import WebSocketDisconnect

from backend.app import ADMIN_COOKIE
from backend.auth import hash_password
from backend.credentials import CredentialStore
from backend.docker_backend import BrokerError
import test_admin as admin_fixtures
import test_chat as chat_fixtures


class ModelPolicyApiTests(unittest.TestCase):
    setUpClass = classmethod(admin_fixtures.AdminApiTests.setUpClass.__func__)
    setUp = admin_fixtures.AdminApiTests.setUp
    start = admin_fixtures.AdminApiTests.start
    tearDown = admin_fixtures.AdminApiTests.tearDown
    login_admin = admin_fixtures.AdminApiTests.login_admin
    login_user = admin_fixtures.AdminApiTests.login_user

    def change(self, allowed, headers):
        return self.client.put("/api/admin/model-policy", headers=headers, json={"fableAllowed": allowed})

    def test_default_denial_auth_origin_csrf_and_strict_shape(self):
        self.assertEqual(self.client.get("/api/admin/model-policy").status_code, 401)
        user = self.login_user()
        user_headers = {"Origin": admin_fixtures.ORIGIN, "X-CSRF-Token": user.json()["csrfToken"]}
        self.assertEqual(self.change(True, user_headers).status_code, 401)
        headers = self.login_admin()
        self.assertEqual(self.client.get("/api/admin/model-policy").json(),
                         {"fableAllowed": False, "revision": 0, "enforcementReady": True})
        for bad in ({}, {"Origin": admin_fixtures.ORIGIN}, user_headers,
                    {**headers, "Origin": admin_fixtures.ORIGIN + "/"}):
            self.assertEqual(self.change(True, bad).status_code, 403)
        for bad in (1, 0, "true", "false", None, [], {}):
            self.assertEqual(self.change(bad, headers).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/model-policy", headers=headers,
                                        json={"fableAllowed": True, "extra": True}).status_code, 422)
        self.assertFalse(self.app.state.runtime["credentials"].fable_allowed)

    def test_global_policy_persists_independently_of_password_and_reapplies_on_restart(self):
        headers = self.login_admin()
        self.assertEqual(self.change(True, headers).json(),
                         {"fableAllowed": True, "revision": 1, "enforcementReady": True})
        store = self.app.state.runtime["credentials"]
        self.assertEqual(store.revision, 0)
        self.assertEqual(self.change(True, headers).json()["revision"], 1)
        self.client.__exit__(None, None, None)
        self.start()
        self.broker.apply_model_policy.assert_awaited_with(True)
        self.login_admin()
        self.assertTrue(self.client.get("/api/admin/model-policy").json()["fableAllowed"])
        self.assertEqual(self.login_user().status_code, 200)

    def test_disable_failure_is_durable_denial_and_same_value_retry_recovers(self):
        headers = self.login_admin()
        self.assertEqual(self.change(True, headers).status_code, 200)
        chats = self.app.state.runtime["chats"]
        self.broker.get = Mock(return_value={"id": admin_fixtures.WORKSPACE})
        session = self.client.portal.call(chats.get, admin_fixtures.WORKSPACE)
        self.app.state.runtime["terminals"].stop_locked = AsyncMock()
        snapshots = []

        async def fail_once(allowed):
            snapshots.append(session.snapshot())
            raise BrokerError("test policy volume unavailable", 503)

        self.broker.apply_model_policy.side_effect = fail_once
        self.assertEqual(self.change(False, headers).status_code, 503)
        self.assertEqual(self.client.get("/api/admin/model-policy").json(),
                         {"fableAllowed": False, "revision": 2, "enforcementReady": False})
        with self.assertRaises(BrokerError):
            chats.require_model(None)
        self.broker.apply_model_policy.side_effect = lambda _: snapshots.append(session.snapshot())
        self.assertEqual(self.change(False, headers).status_code, 200)
        final = session.snapshot()
        self.assertFalse(final["policySyncPending"])
        self.assertGreater(final["revision"], snapshots[-1]["revision"])
        self.assertEqual(final["policyRevision"], 2)

    def test_enable_database_failure_restores_denial_and_hides_details(self):
        headers = self.login_admin()
        with patch.object(self.app.state.runtime["credentials"], "replace_model_policy",
                          side_effect=sqlite3.OperationalError("private sql details")):
            response = self.change(True, headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.broker.apply_model_policy.assert_awaited_with(False)
        self.assertEqual(self.client.get("/api/admin/model-policy").json(),
                         {"fableAllowed": False, "revision": 0, "enforcementReady": True})

    def test_failed_rollback_closes_legacy_raw_sessions(self):
        headers = self.login_admin()
        terminals = self.app.state.runtime["terminals"]
        terminals.term.close = AsyncMock()
        terminals.items[admin_fixtures.WORKSPACE] = terminals.term
        self.broker.apply_model_policy.side_effect = BrokerError("uncertain helper write", 503)
        self.assertEqual(self.change(True, headers).status_code, 503)
        self.assertFalse(self.client.get("/api/admin/model-policy").json()["enforcementReady"])
        self.broker.stop.assert_awaited_with(admin_fixtures.WORKSPACE)
        terminals.term.close.assert_awaited_once()
        terminals.term.input.assert_not_awaited()

    def test_logout_while_enable_is_awaiting_cannot_commit_grant(self):
        headers = self.login_admin()
        started, resume = threading.Event(), threading.Event()

        async def delayed(allowed):
            if allowed:
                started.set()
                while not resume.is_set():
                    await asyncio.sleep(0.001)

        self.broker.apply_model_policy.side_effect = delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.change, True, headers)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
            finally:
                resume.set()
            self.assertEqual(pending.result(timeout=5).status_code, 401)
        self.assertFalse(self.app.state.runtime["credentials"].fable_allowed)
        self.broker.apply_model_policy.assert_awaited_with(False)

    def test_disable_storage_failure_keeps_prior_grant_and_tasks(self):
        headers = self.login_admin()
        self.assertEqual(self.change(True, headers).status_code, 200)
        self.broker.apply_model_policy.reset_mock()
        with patch.object(self.app.state.runtime["credentials"], "replace_model_policy",
                          side_effect=sqlite3.OperationalError("full")):
            self.assertEqual(self.change(False, headers).status_code, 503)
        self.assertTrue(self.client.get("/api/admin/model-policy").json()["fableAllowed"])
        self.broker.stop.assert_not_awaited()
        self.broker.apply_model_policy.assert_not_awaited()


class ModelPolicyStoreTests(unittest.TestCase):
    def test_migration_preserves_existing_password_and_revision(self):
        original, seed = hash_password("old-user-password"), hash_password("new-env-password")
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "auth.sqlite3")
            db.execute("CREATE TABLE dashboard_credentials (id INTEGER PRIMARY KEY, password_hash TEXT, revision INTEGER)")
            db.execute("INSERT INTO dashboard_credentials VALUES (1,?,7)", (original,))
            db.commit()
            db.close()
            store = CredentialStore(directory, seed)
            self.assertEqual(store.snapshot(), (original, 7))
            self.assertEqual(store.model_policy(), {"fableAllowed": False, "revision": 0})
            store.replace_model_policy(True)
            store.replace(seed)
            self.assertEqual(store.model_policy(), {"fableAllowed": True, "revision": 1})
            store.close()
            store = CredentialStore(directory, original)
            self.assertEqual(store.snapshot(), (seed, 8))
            self.assertEqual(store.model_policy(), {"fableAllowed": True, "revision": 1})
            store.close()

    def test_policy_commit_failure_rolls_back_cached_and_durable_grant(self):
        seed = hash_password("seed-user-password")
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(directory, seed)
            store.db = Mock(wraps=store.db)
            store.db.commit.side_effect = sqlite3.OperationalError("full")
            with self.assertRaises(sqlite3.OperationalError):
                store.replace_model_policy(True)
            self.assertFalse(store.fable_allowed)
            store.close()
            store = CredentialStore(directory, seed)
            self.assertEqual(store.model_policy(), {"fableAllowed": False, "revision": 0})
            store.close()


class FableChatTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = chat_fixtures.ChatTests.asyncSetUp
    asyncTearDown = chat_fixtures.ChatTests.asyncTearDown
    connected = chat_fixtures.ChatTests.connected
    start_model_switch = chat_fixtures.ChatTests.start_model_switch

    def policy(self, allowed):
        self.chats.policy = SimpleNamespace(fable_allowed=allowed, policy_revision=1)

    async def deny(self):
        self.policy(False)
        self.chats.policy_changed()
        async with self.chats.lock:
            return await self.chats.revoke_fable_locked()

    async def test_default_denial_visible_option_and_forged_model_or_send_blocked(self):
        option = next(x for x in self.session.snapshot()["modelOptions"] if x["value"] == "fable")
        self.assertTrue(option["disabled"])
        with self.assertRaises(BrokerError) as error:
            await self.chats.set_model(chat_fixtures.WORKSPACE_A, "fable")
        self.assertEqual(error.exception.status, 403)
        self.session.selected_model = "fable"
        self.session.changed()
        with self.assertRaises(BrokerError) as error:
            await self.chats.send(chat_fixtures.WORKSPACE_A, "你好")
        self.assertEqual(error.exception.status, 403)
        self.assertTrue(self.session.snapshot()["modelBlocked"])
        self.translator.translate.assert_not_awaited()
        self.broker.chat_command.assert_not_awaited()

    async def test_revocation_stops_idle_and_busy_fable_preserving_history_and_other_models(self):
        for phase in ("idle", "thinking"):
            with self.subTest(phase=phase):
                self.policy(True)
                self.session.selected_model = "fable"
                self.session.session_id = chat_fixtures.SESSION_A
                self.session.message("assistant", original="Preserved reply")
                fable = self.connected()
                self.session.phase = phase
                other = self.chats.get(chat_fixtures.WORKSPACE_B)
                other.selected_model = "sonnet"
                other.active_model = "claude-sonnet-4-6"
                sonnet = self.connected(other)
                self.broker.stop.reset_mock()
                self.broker.stop.side_effect = lambda workspace: fable.finish() if workspace == chat_fixtures.WORKSPACE_A else None
                await self.deny()
                self.broker.stop.assert_awaited_once_with(chat_fixtures.WORKSPACE_A)
                self.assertIsNone(self.session.proc)
                self.assertIsNone(sonnet.returncode)
                self.assertTrue(other.active)
                self.assertEqual(self.session.selected_model, "fable")
                self.assertTrue(self.session.snapshot()["modelBlocked"])
                self.assertEqual(self.session.session_id, chat_fixtures.SESSION_A)
                self.assertEqual(self.chats.store.load_model(chat_fixtures.WORKSPACE_A), "fable")
                self.assertEqual(self.session.messages[-1]["original"], "Preserved reply")
                sonnet.finish()

    async def test_actual_fable_and_unknown_default_are_stopped_but_known_default_sonnet_survives(self):
        self.policy(True)
        self.session.selected_model = "default"
        fable = self.connected()
        self.session.active_model = "claude-fable-20261101"
        self.broker.stop.side_effect = lambda _: fable.finish()
        await self.deny()
        self.broker.stop.assert_awaited_once()
        self.broker.stop.reset_mock()
        self.policy(True)
        known = self.connected()
        self.session.active_model = "claude-sonnet-4-6"
        await self.deny()
        self.broker.stop.assert_not_awaited()
        known.finish()
        self.policy(True)
        unknown = self.connected()
        self.session.active_model = None
        self.broker.stop.side_effect = lambda _: unknown.finish()
        await self.deny()
        self.broker.stop.assert_awaited_once()

    async def test_pending_fable_ack_cannot_restore_grant_after_revocation(self):
        self.policy(True)
        process, task, request_id = await self.start_model_switch("fable", selected="sonnet")
        self.broker.stop.side_effect = lambda _: process.finish()
        await self.deny()
        with self.assertRaises(BrokerError):
            await task
        self.assertEqual(self.chats.store.load_model(chat_fixtures.WORKSPACE_A), "sonnet")
        self.assertNotEqual(self.session.selected_model, "fable")
        self.assertIsNone(self.session.proc)

    async def test_revocation_cancels_pending_launch_before_sending_fable(self):
        self.policy(True)
        self.session.selected_model = "fable"
        entered, release = asyncio.Event(), asyncio.Event()
        async def launch():
            entered.set()
            await release.wait()
        with patch.object(self.session, "launch", AsyncMock(side_effect=launch)):
            await self.chats.send(chat_fixtures.WORKSPACE_A, "中文")
            await entered.wait()
            task = self.session.input_task
            await self.deny()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.broker.chat_command.assert_not_awaited()
        self.assertFalse(self.session.messages[0]["sent"])

    async def test_known_non_fable_write_continues_during_policy_sync(self):
        self.policy(True)
        self.session.selected_model = "sonnet"
        process = self.connected()
        self.session.phase = "idle"
        self.session.active_model = "claude-sonnet-4-6"
        await self.session.write_lock.acquire()
        await self.chats.send(chat_fixtures.WORKSPACE_A, "中文")
        await chat_fixtures.until(lambda: self.session.phase == "thinking")
        self.chats.policy_ready = False
        await self.deny()
        self.session.write_lock.release()
        await self.session.input_task
        self.assertEqual(process.stdin.messages[-1]["message"]["content"], "中文")
        self.assertTrue(self.session.messages[0]["sent"])
        self.broker.stop.assert_not_awaited()

    async def test_raw_terminal_guard_is_checked_after_waiting_for_shared_lock(self):
        await self.chats.lock.acquire()
        pending = asyncio.create_task(self.terminals.attach(chat_fixtures.WORKSPACE_A))
        await asyncio.sleep(0)
        self.chats.policy_ready = False
        self.chats.lock.release()
        with self.assertRaises(BrokerError) as error:
            await pending
        self.assertEqual(error.exception.status, 503)
        self.broker.terminal_command.assert_not_awaited()

    async def test_all_raw_sessions_are_closed_and_stop_failure_is_retryable(self):
        raw = SimpleNamespace(close=AsyncMock())
        self.terminals.items[chat_fixtures.WORKSPACE_B] = raw
        self.broker.stop.side_effect = BrokerError("docker unavailable", 503)
        with self.assertRaises(BrokerError):
            await self.deny()
        raw.close.assert_awaited_once()
        self.assertIn(chat_fixtures.WORKSPACE_B, self.chats.revocation_pending)
        self.broker.stop.side_effect = None
        self.assertIn(chat_fixtures.WORKSPACE_B, await self.deny())
        self.assertEqual(self.chats.revocation_pending, set())

    async def test_reported_forbidden_model_fails_closed(self):
        process = self.connected()
        self.session.selected_model = "default"
        self.broker.stop.side_effect = lambda _: process.finish()
        self.session.reader = asyncio.create_task(self.session.read())
        process.emit({"type": "system", "subtype": "init", "session_id": chat_fixtures.SESSION_A,
                      "model": "claude-fable-20261101"})
        await chat_fixtures.until(lambda: self.session.phase == "error")
        self.assertIsNone(self.session.proc)
        self.broker.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
