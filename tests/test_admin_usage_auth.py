"""Private profile authorization, durable manual binding and query races."""
import asyncio
from collections import deque
import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.admin_usage_auth import AdminUsageAuth, UsageAuthTerminal
from backend.admin_usage_broker import (ADMIN_USAGE_ID, CREDENTIAL_STATUS, LAUNCH_PRIVATE,
                                        AdminUsageBroker)
from backend.auth import Sessions
from backend.docker_backend import BrokerError, Config
from backend.official_usage import UsageUnavailable
import test_admin as fixtures


PROFILE = {"authenticated": True, "profileScope": True, "accountEmail": "owner@example.test"}


def facade():
    return SimpleNamespace(workspace_id=ADMIN_USAGE_ID, auth_status=AsyncMock(return_value=dict(PROFILE)),
                           start=AsyncMock(), stop=AsyncMock(), reconcile=AsyncMock(),
                           terminal_command=AsyncMock(return_value=["fake-login"]),
                           usage_command=Mock(return_value=["fake-query"]))


class UsageAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sessions = Sessions()
        self.owner = self.sessions.create().token
        self.generation = 3
        self.facade = facade()
        self.facade_patch = patch("backend.admin_usage_auth.AdminUsageBroker", return_value=self.facade)
        self.facade_patch.start()
        self.browser_patch = patch("backend.admin_usage_auth.LoginBrowsers", return_value=SimpleNamespace(stop=AsyncMock(), close=AsyncMock()))
        self.browser_patch.start()
        self.runtime = {"broker": SimpleNamespace(config=Config(data_dir=self.temp.name),
                                                 shared_auth_status=AsyncMock(return_value={"authenticated": True})),
                        "terminals": SimpleNamespace(lock=asyncio.Lock()),
                        "shares": SimpleNamespace(config=lambda: {"accountGeneration": self.generation}),
                        "official": SimpleNamespace(invalidate=Mock())}
        self.service = AdminUsageAuth(self.runtime, self.sessions)
        self.service.browsers = SimpleNamespace(stop=AsyncMock(), close=AsyncMock())

    async def asyncTearDown(self):
        await self.service.close()
        self.facade_patch.stop()
        self.browser_patch.stop()
        self.temp.cleanup()

    def done_term(self, code=0):
        watch = asyncio.get_running_loop().create_future()
        watch.set_result(None)
        return SimpleNamespace(watch=watch, proc=SimpleNamespace(returncode=code), finished=True,
                               private_output="should never retain", replay=deque(["login"]), replay_size=5)

    async def complete_login(self, code=0):
        await self.service.prepare(self.owner)
        term = self.done_term(code)
        await self.service._capture_login(term, self.owner, self.generation)
        return term

    async def test_login_requires_explicit_prepare_and_post_login_confirmation(self):
        with self.assertRaises(BrokerError):
            await self.service.attach(self.owner)
        self.facade.terminal_command.assert_not_awaited()
        term = await self.complete_login()
        state = await self.service.status()
        self.assertFalse(state["authenticated"])
        self.assertTrue(state["pendingConfirmation"])
        self.assertEqual(state["accountEmail"], PROFILE["accountEmail"])
        self.assertEqual(term.private_output, "")
        self.assertEqual(list(term.replay), [])
        with self.assertRaises(UsageUnavailable):
            await self.service.fetch_usage()
        await self.service.confirm(self.owner)
        self.assertTrue((await self.service.status())["authenticated"])
        self.assertFalse((await self.service.status())["pendingConfirmation"])
        self.runtime["official"].invalidate.assert_called_with(code="usage_authorization_required")

    async def test_prepare_invalidates_durable_binding_without_resetting_shared_account(self):
        self.service._persist_binding(self.generation, PROFILE["accountEmail"])
        await self.service.prepare(self.owner)
        self.assertIsNone(self.service.binding_generation)
        self.assertEqual(self.service.db.execute("SELECT generation FROM binding").fetchone(), (None,))
        self.assertEqual(self.generation, 3)
        self.assertEqual(self.runtime["official"].invalidate.call_count, 1)

    async def test_confirmed_binding_survives_restart_but_shared_generation_invalidates(self):
        await self.complete_login()
        await self.service.confirm(self.owner)
        await self.service.close()
        self.service = AdminUsageAuth(self.runtime, self.sessions)
        self.service.browsers = SimpleNamespace(stop=AsyncMock(), close=AsyncMock())
        self.assertTrue((await self.service.status())["authenticated"])
        self.generation += 1
        state = await self.service.status()
        self.assertFalse(state["authenticated"])
        self.assertIsNone(state["accountEmail"])
        with self.assertRaises(UsageUnavailable):
            await self.service.fetch_usage()
        self.facade.start.assert_not_awaited()

    async def test_nonzero_login_or_missing_profile_cannot_use_old_credentials(self):
        self.service._persist_binding(self.generation)
        await self.complete_login(code=1)
        self.assertIsNone(self.service.binding_generation)
        self.assertFalse((await self.service.status())["pendingConfirmation"])
        self.facade.auth_status.assert_not_awaited()
        self.facade.auth_status.return_value = {"authenticated": True, "profileScope": False}
        await self.complete_login()
        self.assertFalse((await self.service.status())["pendingConfirmation"])
        with self.assertRaises(BrokerError):
            await self.service.confirm(self.owner)

    async def test_login_generation_change_during_scope_check_cannot_confirm(self):
        await self.service.prepare(self.owner)
        async def scope_check():
            self.generation += 1
            return PROFILE
        self.facade.auth_status.side_effect = scope_check
        await self.service._capture_login(self.done_term(), self.owner, self.generation)
        self.assertIsNone(self.service.binding_generation)
        self.assertFalse((await self.service.status())["pendingConfirmation"])

    async def test_query_requires_new_authorization_after_pending_generation_changes(self):
        await self.complete_login()
        self.generation += 1
        with self.assertRaises(UsageUnavailable) as error:
            await self.service.fetch_usage()
        self.assertEqual(error.exception.code, "usage_authorization_required")

    async def test_expired_admin_after_container_start_cannot_open_login_terminal(self):
        await self.service.prepare(self.owner)
        async def command(_):
            self.sessions.remove(self.owner)
            return ["fake-login"]
        self.facade.terminal_command.side_effect = command
        with patch("backend.admin_usage_auth.UsageAuthTerminal") as terminal:
            with self.assertRaises(BrokerError):
                await self.service.attach(self.owner)
        terminal.assert_not_called()
        self.facade.stop.assert_awaited()

    async def test_confirm_rechecks_admin_and_generation_after_scope_check(self):
        for revoke in ("session", "generation"):
            self.owner = self.sessions.create().token
            self.facade.auth_status.side_effect = None
            await self.complete_login()
            async def mutate():
                if revoke == "session":
                    self.sessions.remove(self.owner)
                else:
                    self.generation += 1
                return PROFILE
            self.facade.auth_status.side_effect = mutate
            with self.assertRaises(BrokerError):
                await self.service.confirm(self.owner)
            self.assertIsNone(self.service.binding_generation)

    async def test_other_administrator_cannot_confirm_another_pending_login(self):
        await self.complete_login()
        another = self.sessions.create().token
        with self.assertRaises(BrokerError):
            await self.service.confirm(another)
        self.assertIsNone(self.service.binding_generation)

    async def test_commit_failure_does_not_create_live_binding(self):
        await self.complete_login()
        actual = self.service.db
        fake = Mock(wraps=actual)
        fake.commit.side_effect = sqlite3.OperationalError("private database detail")
        self.service.db = fake
        try:
            with self.assertRaises(BrokerError) as error:
                await self.service.confirm(self.owner)
            self.assertNotIn("private database detail", str(error.exception))
            self.assertIsNone(self.service.binding_generation)
            self.assertEqual(actual.execute("SELECT generation FROM binding").fetchone(), (None,))
        finally:
            self.service.db = actual

    async def test_fixed_query_works_without_any_workspace_and_stops_reader(self):
        self.service._persist_binding(self.generation)
        reply = {"rate_limits": {"five_hour": {"utilization": 12}}}
        with patch("backend.admin_usage_auth.fetch_usage_command", AsyncMock(return_value=reply)) as query:
            self.assertEqual(await self.service.fetch_usage(), reply)
        query.assert_awaited_once_with(["fake-query"])
        self.facade.start.assert_awaited_once_with(ADMIN_USAGE_ID)
        self.facade.stop.assert_awaited_once_with(ADMIN_USAGE_ID)

    async def test_inflight_old_generation_query_is_discarded(self):
        self.service._persist_binding(self.generation)
        async def query(_):
            self.generation += 1
            return {"rate_limits": {"five_hour": {"utilization": 12}}}
        with patch("backend.admin_usage_auth.fetch_usage_command", side_effect=query):
            with self.assertRaises(UsageUnavailable):
                await self.service.fetch_usage()
        self.facade.stop.assert_awaited_once_with(ADMIN_USAGE_ID)

    async def test_query_and_prepare_serialize_without_lock_order_cycle(self):
        self.service._persist_binding(self.generation)
        started, release = asyncio.Event(), asyncio.Event()
        async def query(_):
            started.set()
            await release.wait()
            return {"rate_limits": {}}
        with patch("backend.admin_usage_auth.fetch_usage_command", side_effect=query):
            query_task = asyncio.create_task(self.service.fetch_usage())
            await started.wait()
            prepare_task = asyncio.create_task(self.service.prepare(self.owner))
            await asyncio.sleep(0)
            self.assertFalse(prepare_task.done())
            with self.assertRaises(UsageUnavailable):
                await self.service.fetch_usage()
            release.set()
            await asyncio.wait_for(asyncio.gather(query_task, prepare_task), 1)
        self.assertIsNone(self.service.binding_generation)

    async def test_query_cancel_stops_container_and_keeps_binding(self):
        self.service._persist_binding(self.generation)
        started = asyncio.Event()
        async def query(_):
            started.set()
            await asyncio.Future()
        with patch("backend.admin_usage_auth.fetch_usage_command", side_effect=query):
            task = asyncio.create_task(self.service.fetch_usage())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.facade.stop.assert_awaited_once_with(ADMIN_USAGE_ID)
        self.assertEqual(self.service.binding_generation, self.generation)

    async def test_reconcile_accepts_callers_lifecycle_lock_and_keeps_binding(self):
        self.service._persist_binding(self.generation)
        async with self.runtime["terminals"].lock:
            await asyncio.wait_for(self.service.reconcile(), 1)
        self.facade.reconcile.assert_awaited_once()
        self.assertEqual(self.service.binding_generation, self.generation)

    async def test_revoke_before_shared_rotation_is_durable_across_restart(self):
        self.service._persist_binding(self.generation, PROFILE["accountEmail"])
        self.service.pending_generation = self.generation
        self.service.pending_owner = self.owner
        async with self.runtime["terminals"].lock:
            await self.service.revoke_binding()
        self.assertIsNone(self.service.pending_generation)
        await self.service.close()
        self.service = AdminUsageAuth(self.runtime, self.sessions)
        self.service.browsers = SimpleNamespace(stop=AsyncMock(), close=AsyncMock())
        self.assertFalse((await self.service.status())["authenticated"])
        self.assertIsNone(self.service.binding_generation)

    async def test_revoke_storage_failure_aborts_before_private_state_changes(self):
        self.service._persist_binding(self.generation)
        with patch.object(self.service, "_persist_binding", side_effect=BrokerError("storage failed", 503)):
            async with self.runtime["terminals"].lock:
                with self.assertRaises(BrokerError):
                    await self.service.revoke_binding()
        self.assertEqual(self.service.binding_generation, self.generation)
        self.facade.stop.assert_not_awaited()

    async def test_database_contains_no_oauth_material(self):
        await self.complete_login()
        await self.service.confirm(self.owner)
        stored = Path(self.temp.name, "usage_auth.sqlite3").read_bytes()
        for marker in (b"accessToken", b"refreshToken", b"sk-ant-oat", b"should never retain"):
            self.assertNotIn(marker, stored)


class UsageBrokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parent = SimpleNamespace(config=Config(data_dir="unused"), owner="a" * 32,
                                      command=AsyncMock(), _ensure_model_policy=AsyncMock())
        self.broker = AdminUsageBroker(self.parent)

    async def test_commands_are_fixed_private_and_profile_scoped(self):
        self.broker.start = AsyncMock()
        login = await self.broker.terminal_command(ADMIN_USAGE_ID)
        query = self.broker.usage_command()
        self.assertEqual(login[-3:], ["auth", "login", "--claudeai"])
        self.assertIn(LAUNCH_PRIVATE, login)
        self.assertIn(LAUNCH_PRIVATE, query)
        self.assertIn("25s", query)
        self.assertIn("--strict-mcp-config", query)
        self.assertEqual(query[query.index("--tools") + 1], "")
        self.assertNotIn("--bare", query)
        self.assertFalse(self.broker._uses_shared_auth)
        self.assertNotIn("/run/claude-cloud-auth", " ".join(login + query))
        with self.assertRaises(BrokerError):
            await self.broker.terminal_command("b" * 32)

    async def test_auth_status_whitelists_helper_fields_and_hides_raw_secret_output(self):
        self.parent.command.return_value = (0, json.dumps({**PROFILE, "accessToken": "private"}).encode(), b"private stderr")
        self.assertEqual(await self.broker.auth_status(), PROFILE)
        call = self.parent.command.call_args
        self.assertIn(CREDENTIAL_STATUS, call.args)
        self.parent.command.return_value = (0, b"private invalid reply", b"private stderr")
        with self.assertRaises(BrokerError) as error:
            await self.broker.auth_status()
        self.assertNotIn("private", str(error.exception))

    async def test_profile_facade_cannot_adopt_other_resource_role(self):
        from backend.docker_backend import OWNER_LABEL, WORKSPACE_LABEL, RESOURCE_LABEL
        info = {"Labels": {OWNER_LABEL: self.parent.owner, WORKSPACE_LABEL: ADMIN_USAGE_ID, RESOURCE_LABEL: "admin-auth"}}
        self.parent.command.return_value = (0, json.dumps([info]).encode(), b"")
        with self.assertRaises(BrokerError):
            await self.broker.inspect_owned("volume", "fixed-home", ADMIN_USAGE_ID)

    async def test_private_container_mounts_only_owned_private_and_policy_volumes(self):
        self.parent.command.return_value = (0, b"", b"")
        self.broker.inspect_owned = AsyncMock(return_value=None)
        self.broker._ensure_workspace_egress = AsyncMock()
        self.broker._check_model_policy_support = AsyncMock()
        await self.broker._start(ADMIN_USAGE_ID)
        create = next(call.args for call in self.parent.command.call_args_list if call.args[:2] == ("container", "create"))
        mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(any(ADMIN_USAGE_ID + "-home,dst=/home/claude" in item for item in mounts))
        self.assertFalse(any("shared-auth" in item or "docker.sock" in item for item in mounts))
        self.assertIn("--read-only", create)
        self.assertIn("1000:1000", create)

    def test_scope_helper_emits_only_boolean_status_and_email(self):
        secret = "sk-ant-oat01-private-access-value"
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as credentials:
            json.dump({"claudeAiOauth": {"scopes": ["user:profile", "user:inference"],
                                        "accessToken": secret, "refreshToken": "private-refresh"}}, credentials)
            credentials.flush()
            def private_open(path, flags):
                self.assertEqual(path, "/home/claude/.claude/.credentials.json")
                os.lseek(credentials.fileno(), 0, os.SEEK_SET)
                return os.dup(credentials.fileno())
            output = io.StringIO()
            status = {"loggedIn": True, "authMethod": "claude.ai", "email": PROFILE["accountEmail"], "token": secret}
            env = {"ANTHROPIC_API_KEY": "private-key", "CLAUDE_CODE_OAUTH_TOKEN": secret, "CLAUDE_CODE_SIMPLE": "1"}
            with patch("os.open", side_effect=private_open), patch.object(os, "O_NOFOLLOW", 0, create=True), \
                    patch.dict(os.environ, env), patch("sys.stdout", output), \
                    patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(status).encode())) as command:
                exec(CREDENTIAL_STATUS, {})
            self.assertEqual(json.loads(output.getvalue()), PROFILE)
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn("private-refresh", output.getvalue())
            clean = command.call_args.kwargs["env"]
            for key in env:
                self.assertNotIn(key, clean)

    def test_terminal_redacts_bearer_without_retaining_private_output(self):
        terminal = UsageAuthTerminal(["fake"])
        token = "sk-ant-oat01-" + "a" * 100
        visible = "".join(terminal.filter_output(char) for char in "hello\n" + token)
        self.assertNotIn(token, visible)
        self.assertEqual(terminal.private_output, "")


class UsageAuthorizationApiTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AdminApiTests.setUpClass.__func__)
    start = fixtures.AdminApiTests.start
    login_admin = fixtures.AdminApiTests.login_admin
    login_user = fixtures.AdminApiTests.login_user

    def setUp(self):
        fixtures.AdminApiTests.setUp(self)
        self.broker.config = self.config
        self.facade = facade()
        self.facade_patch = patch("backend.admin_usage_auth.AdminUsageBroker", return_value=self.facade)
        self.facade_patch.start()
        self.browser_patch = patch("backend.admin_usage_auth.LoginBrowsers", return_value=SimpleNamespace(stop=AsyncMock(), close=AsyncMock()))
        self.browser_patch.start()

    def tearDown(self):
        fixtures.AdminApiTests.tearDown(self)
        self.facade_patch.stop()
        self.browser_patch.stop()

    def test_prepare_and_confirm_require_admin_origin_csrf_and_exact_body(self):
        user = self.login_user().json()
        user_headers = {"Origin": fixtures.ORIGIN, "X-CSRF-Token": user["csrfToken"]}
        endpoints = (("prepare", "AUTHORIZE_USAGE_FOR_SHARED_ACCOUNT"), ("confirm", "CONFIRM_SAME_CLAUDE_ACCOUNT"))
        for path, confirmation in endpoints:
            route = "/api/admin/usage-auth/" + path
            self.assertEqual(self.client.post(route, headers=user_headers, json={"confirmation": confirmation}).status_code, 401)
        headers = self.login_admin()
        for path, confirmation in endpoints:
            route = "/api/admin/usage-auth/" + path
            for bad in ({}, {"Origin": fixtures.ORIGIN}, {**headers, "Origin": "https://other.test"}):
                self.assertEqual(self.client.post(route, headers=bad, json={"confirmation": confirmation}).status_code, 403)
            for body in ({"confirmation": "wrong"}, {"confirmation": confirmation, "token": "never echo me"}):
                result = self.client.post(route, headers=headers, json=body)
                self.assertEqual(result.status_code, 422)
                self.assertNotIn("never echo me", result.text)
        self.assertEqual(self.client.post("/api/admin/usage-auth/prepare", headers=headers,
                                         json={"confirmation": endpoints[0][1]}).status_code, 200)
        state = self.client.get("/api/admin/usage-auth/status").json()
        self.assertFalse(state["authenticated"])
        self.assertFalse(state["pendingConfirmation"])
        self.assertEqual(self.client.post("/api/admin/usage-auth/confirm", headers=headers,
                                         json={"confirmation": endpoints[1][1]}).status_code, 409)
