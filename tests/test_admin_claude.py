"""Admin-only auth transports, secret redaction, and shared credential handoff."""
import asyncio
from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from starlette.websockets import WebSocketDisconnect

from backend.admin_claude import AdminClaude, AuthTerminal, extract_setup_token
from backend.app import ADMIN_COOKIE
from backend.auth import Sessions
from backend.docker_backend import BrokerError
import test_admin as fixtures

TOKEN = "sk-ant-oat01-" + "aB0_-" * 25
PREFIX = "/api/admin/claude"


class FakeAdminTerminals:
    def __init__(self, broker, terminal_factory=None):
        self.lock = asyncio.Lock()
        self.require_policy = lambda: None
        self.items = {}
        self.term = None
        self.attach_count = 0

    def active(self, workspace):
        return bool(self.term and not self.term.finished)

    async def attach(self, workspace):
        async with self.lock:
            self.require_policy()
            self.attach_count += 1
            if self.term is None or self.term.finished:
                self.term = SimpleNamespace(
                    watch=asyncio.get_running_loop().create_future(), proc=SimpleNamespace(returncode=None),
                    private_output="", output_overflow=False, replay=deque(), replay_size=0,
                    finished=False, input=AsyncMock(), resize=Mock(), detach=Mock())
                self.items[workspace] = self.term
            self.queue = asyncio.Queue()
            self.queue.put_nowait({"type": "ready"})
            return self.term, self.queue

    async def stop_locked(self, workspace, restart=False):
        if self.term and not self.term.finished:
            self.term.finished = True
            self.term.proc.returncode = -15
            self.term.watch.set_result(None)
            self.queue.put_nowait({"type": "disconnect"})
        self.items.clear()


class AdminClaudeApiTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AdminApiTests.setUpClass.__func__)
    start = fixtures.AdminApiTests.start
    login_admin = fixtures.AdminApiTests.login_admin
    login_user = fixtures.AdminApiTests.login_user

    def setUp(self):
        fixtures.AdminApiTests.setUp(self)
        self.facade = SimpleNamespace(workspace_id="admin-auth", auth_status=AsyncMock(return_value={"authenticated": True}))
        self.broker.admin_auth = Mock(return_value=self.facade)
        self.admin_browsers = SimpleNamespace(
            status=AsyncMock(return_value={"active": False}), start=AsyncMock(return_value={"active": True}),
            stop=AsyncMock(), close=AsyncMock(), open_channel=AsyncMock(), release_channel=AsyncMock())
        self.term_patch = patch("backend.admin_claude.Terminals", FakeAdminTerminals)
        self.browser_patch = patch("backend.admin_claude.LoginBrowsers", return_value=self.admin_browsers)
        self.term_patch.start()
        self.browser_patch.start()

    def tearDown(self):
        fixtures.AdminApiTests.tearDown(self)
        self.browser_patch.stop()
        self.term_patch.stop()

    def connect(self):
        return self.client.websocket_connect("ws://localhost:8000" + PREFIX + "/terminal",
                                             headers={"Origin": fixtures.ORIGIN})

    def test_user_cannot_read_or_open_admin_login_endpoints(self):
        user = self.login_user()
        headers = {"Origin": fixtures.ORIGIN, "X-CSRF-Token": user.json()["csrfToken"]}
        for suffix in ("/status", "/login-browser"):
            self.assertEqual(self.client.get(PREFIX + suffix).status_code, 401)
        for suffix, body in (("/terminal/stop", None), ("/login-browser/stop", None), ("/login-browser", {"url": "https://claude.com/cai/oauth/authorize?x=1"})):
            self.assertEqual(self.client.post(PREFIX + suffix, headers=headers, json=body).status_code, 401)
        for suffix in ("/terminal", "/login-browser/display"):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("ws://localhost:8000" + PREFIX + suffix, headers={"Origin": fixtures.ORIGIN}):
                    pass
        self.broker.admin_auth.assert_not_called()

    def test_status_is_configuration_boolean_without_account_or_token_details(self):
        self.login_admin()
        self.facade.auth_status.return_value = {"authenticated": True, "token": TOKEN, "email": "hidden@example.test"}
        response = self.client.get(PREFIX + "/status")
        self.assertEqual(response.json(), {"authenticated": True, "terminalActive": False, "importing": False, "error": None})
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn("hidden@", response.text)
        self.login_user()
        self.assertTrue(self.client.get("/api/status").json()["claudeConfigured"])

    def test_mutations_require_admin_origin_and_csrf(self):
        good = self.login_admin()
        for bad in ({}, {"Origin": fixtures.ORIGIN}, {**good, "Origin": "https://evil.example"}):
            for suffix, body in (("/terminal/stop", None), ("/login-browser/stop", None), ("/login-browser", {"url": "https://claude.com/cai/oauth/authorize?x=1"})):
                self.assertEqual(self.client.post(PREFIX + suffix, headers=bad, json=body).status_code, 403)
        self.broker.admin_auth.assert_not_called()

    def test_terminal_requires_csrf_and_origin_before_launch(self):
        self.login_admin()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("ws://localhost:8000" + PREFIX + "/terminal", headers={"Origin": "https://evil.example"}):
                pass
        for token in ("wrong", "恶意令牌"):
            with self.connect() as ws:
                ws.send_json({"type": "auth", "csrfToken": token})
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
        self.broker.admin_auth.assert_not_called()

    def test_admin_logout_stops_login_process_and_rejects_late_terminal_input(self):
        headers = self.login_admin()
        with self.connect() as ws:
            ws.send_json({"type": "auth", "csrfToken": headers["X-CSRF-Token"]})
            self.assertEqual(ws.receive_json(), {"type": "ready"})
            service = self.app.state.runtime["admin_claude"]
            self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
            ws.send_json({"type": "input", "data": "late authorization code"})
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
            service.terminals.term.input.assert_not_awaited()
            self.assertFalse(service.terminals.active("admin-auth"))
            self.admin_browsers.stop.assert_awaited()

    def test_expired_session_cannot_receive_new_terminal_output(self):
        headers = self.login_admin()
        with self.connect() as ws:
            ws.send_json({"type": "auth", "csrfToken": headers["X-CSRF-Token"]})
            self.assertEqual(ws.receive_json(), {"type": "ready"})
            service = self.app.state.runtime["admin_claude"]
            self.app.state.admin_sessions.remove(self.client.cookies.get(ADMIN_COOKIE))
            self.client.portal.call(service.terminals.queue.put, {"type": "output", "data": "private"})
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()

    def test_browser_owner_and_binary_channel_are_admin_scoped_and_revoked(self):
        headers = self.login_admin()
        with self.connect() as terminal:
            terminal.send_json({"type": "auth", "csrfToken": headers["X-CSRF-Token"]})
            self.assertEqual(terminal.receive_json(), {"type": "ready"})
            self.assertEqual(self.client.post(PREFIX + "/login-browser", headers=headers,
                                              json={"url": "https://claude.com/cai/oauth/authorize?x=1"}).status_code, 200)
            proc = SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))

            async def open_channel(_):
                proc.stdout = asyncio.StreamReader()
                proc.stdout.feed_data(b"RFB 003.008\n")
                return proc

            self.admin_browsers.open_channel.side_effect = open_channel
            with self.client.websocket_connect("ws://localhost:8000" + PREFIX + "/login-browser/display",
                                               headers={"Origin": fixtures.ORIGIN}) as ws:
                ws.send_json({"type": "auth", "csrfToken": headers["X-CSRF-Token"]})
                self.assertEqual(ws.receive_bytes(), b"RFB 003.008\n")
                self.app.state.admin_sessions.remove(self.client.cookies.get(ADMIN_COOKIE))
                ws.send_bytes(b"late keys")
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_bytes()
            proc.stdin.write.assert_not_called()
            self.admin_browsers.release_channel.assert_awaited_once_with("admin-auth", proc)


class SetupTokenTests(unittest.TestCase):
    def test_unique_bounded_token_after_ansi_is_extracted(self):
        self.assertEqual(extract_setup_token("Success\n\x1b[32m" + TOKEN + "\x1b[0m\n"), TOKEN)
        self.assertEqual(extract_setup_token(TOKEN + "\n" + TOKEN), TOKEN)
        self.assertIsNone(extract_setup_token(TOKEN + "\n" + TOKEN + "different"))
        self.assertIsNone(extract_setup_token("sk-ant-oat01-short"))
        self.assertIsNone(extract_setup_token("x" * 262145 + TOKEN))
        self.assertIsNone(extract_setup_token(TOKEN[:90] + "\n" + TOKEN[90:] + "\n"))

    def test_every_chunk_boundary_redacts_token_before_replay_or_socket(self):
        text = "Open https://claude.ai/oauth and paste code\r\n" + TOKEN + "\r\nKeep secret"
        for boundary in range(len(text) + 1):
            term = AuthTerminal(["test"])
            visible = term.filter_output(text[:boundary]) + term.filter_output(text[boundary:])
            self.assertNotIn(TOKEN, visible)
            self.assertNotIn("aB0_-", visible)
            self.assertIn("https://claude.ai/oauth", visible)
            self.assertEqual(extract_setup_token(term.private_output), TOKEN)

    def test_split_ansi_and_token_prefix_never_reveal_tail(self):
        term = AuthTerminal(["test"])
        text = "Done\n\x1b[32msk-ant-\x1b[0moat01-" + "aB0_-" * 25 + "\n"
        visible = "".join(term.filter_output(char) for char in text)
        self.assertNotIn("aB0_-", visible)
        self.assertIn("凭据已隐藏", visible)
        self.assertEqual(extract_setup_token(term.private_output), TOKEN)


class CredentialHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.sessions = Sessions()
        self.owner = self.sessions.create().token
        self.facade = SimpleNamespace(workspace_id="admin-auth", auth_status=AsyncMock(return_value={"authenticated": True}))
        self.broker = SimpleNamespace(admin_auth=Mock(return_value=self.facade),
                                      list=AsyncMock(return_value=[{"id": "workspace", "running": True}]),
                                      save_shared_auth=AsyncMock(side_effect=lambda _: self.events.append("save")))
        self.runtime = {"broker": self.broker,
                        "terminals": SimpleNamespace(lock=asyncio.Lock(), stop_locked=AsyncMock(side_effect=lambda _: self.events.append("stop"))),
                        "chats": SimpleNamespace(items={}, close_locked=AsyncMock(side_effect=lambda _: self.events.append("close")))}
        self.service = AdminClaude(self.runtime, self.sessions)
        self.service.owner = self.owner
        self.service.browsers = SimpleNamespace(stop=AsyncMock(), close=AsyncMock())
        self.service.terminals.stop_locked = AsyncMock()
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        self.term = SimpleNamespace(watch=done, proc=SimpleNamespace(returncode=0), output_overflow=False,
                                    private_output=TOKEN, replay=deque(), replay_size=0)

    async def test_success_stops_all_user_work_before_saving_and_clears_transcript(self):
        await self.service._capture_token(self.term, self.owner)
        self.assertEqual(self.events, ["close", "stop", "save"])
        self.broker.save_shared_auth.assert_awaited_once_with(TOKEN)
        self.assertEqual(self.term.private_output, "")
        self.assertIsNone(self.service.error)

    async def test_usage_binding_is_revoked_before_shared_token_changes_even_if_later_cleanup_fails(self):
        self.runtime['admin_usage_auth'] = SimpleNamespace(
            revoke_binding=AsyncMock(side_effect=lambda: self.events.append('revoke_usage')))
        self.runtime['quotas'] = SimpleNamespace(clear_windows=Mock(side_effect=RuntimeError('storage failed')))
        self.runtime['shares'] = SimpleNamespace(account_changed=Mock())
        await self.service._capture_token(self.term, self.owner)
        self.assertEqual(self.events, ['close', 'stop', 'revoke_usage', 'save'])
        self.runtime['shares'].account_changed.assert_not_called()
        self.assertIsNotNone(self.service.error)

    async def test_failed_usage_binding_revocation_prevents_shared_token_replacement(self):
        self.runtime['admin_usage_auth'] = SimpleNamespace(
            revoke_binding=AsyncMock(side_effect=BrokerError('private storage failure', 503)))
        await self.service._capture_token(self.term, self.owner)
        self.broker.save_shared_auth.assert_not_awaited()
        self.assertIsNotNone(self.service.error)

    async def test_failure_or_nonzero_exit_never_replaces_existing_shared_auth(self):
        self.term.proc.returncode = 1
        await self.service._capture_token(self.term, self.owner)
        self.broker.save_shared_auth.assert_not_awaited()
        self.term.proc.returncode = 0
        self.term.private_output = TOKEN
        self.runtime["terminals"].stop_locked.side_effect = BrokerError("stop failed", 503)
        await self.service._capture_token(self.term, self.owner)
        self.broker.save_shared_auth.assert_not_awaited()
        self.assertIsNotNone(self.service.error)

    async def test_expiry_while_waiting_for_lifecycle_lock_cannot_save(self):
        await self.runtime["terminals"].lock.acquire()
        pending = asyncio.create_task(self.service._capture_token(self.term, self.owner))
        await asyncio.sleep(0)
        self.sessions.remove(self.owner)
        self.runtime["terminals"].lock.release()
        await pending
        self.broker.save_shared_auth.assert_not_awaited()
        self.service.terminals.stop_locked.assert_awaited_once_with("admin-auth")

    async def test_revocation_during_workspace_stop_prevents_save(self):
        self.runtime["terminals"].stop_locked.side_effect = lambda _: self.sessions.remove(self.owner)
        await self.service._capture_token(self.term, self.owner)
        self.broker.save_shared_auth.assert_not_awaited()

    async def test_attach_rechecks_owner_after_waiting_for_shared_lock(self):
        self.service.owner = None
        await self.runtime["terminals"].lock.acquire()
        pending = asyncio.create_task(self.service.attach(self.owner))
        await asyncio.sleep(0)
        self.sessions.remove(self.owner)
        self.runtime["terminals"].lock.release()
        with self.assertRaises(BrokerError) as caught:
            await pending
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.service.terminals.items, {})

    async def test_cancelled_import_waits_for_atomic_writer_before_returning(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def save(_):
            started.set()
            await release.wait()
            self.events.append("saved")

        self.broker.save_shared_auth.side_effect = save
        pending = asyncio.create_task(self.service._capture_token(self.term, self.owner))
        await started.wait()
        pending.cancel()
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.events[-1], "saved")
        self.assertEqual(self.term.private_output, "")

    async def test_reconnect_cannot_replace_finished_terminal_while_capture_waits_for_lock(self):
        self.term.finished = True
        self.service.capture_term = self.term
        await self.runtime["terminals"].lock.acquire()
        capture = asyncio.create_task(self.service._capture_token(self.term, self.owner))
        self.service.capture = capture
        self.service.terminals.attach = AsyncMock()
        await asyncio.sleep(0)
        try:
            with self.assertRaises(BrokerError) as caught:
                await self.service.attach(self.owner)
            self.assertEqual(caught.exception.status, 409)
            self.assertTrue((await self.service.status())["importing"])
            self.service.terminals.attach.assert_not_awaited()
            self.assertIs(self.service.capture, capture)
        finally:
            self.runtime["terminals"].lock.release()
        await capture
        self.assertEqual(self.events, ["close", "stop", "save"])
        self.assertFalse((await self.service.status())["importing"])


if __name__ == "__main__":
    unittest.main()
