"""The new profile authorization surface stays exclusively administrator-only."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from starlette.websockets import WebSocketDisconnect

from backend.app import ADMIN_COOKIE
import test_admin as fixtures

PREFIX = '/api/admin/usage-auth'


class AdminUsageTransportTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AdminApiTests.setUpClass.__func__)
    start = fixtures.AdminApiTests.start
    login_admin = fixtures.AdminApiTests.login_admin
    login_user = fixtures.AdminApiTests.login_user
    tearDown = fixtures.AdminApiTests.tearDown

    def setUp(self):
        fixtures.AdminApiTests.setUp(self)
        self.service = SimpleNamespace(
            workspace_id='admin-usage-auth', owner=None, browser_owner=None,
            terminals=SimpleNamespace(lock=self.app.state.runtime['terminals'].lock, active=Mock(return_value=True)),
            browsers=SimpleNamespace(status=AsyncMock(return_value={'active': False}),
                                     start=AsyncMock(return_value={'active': True}), stop=AsyncMock()),
            status=AsyncMock(return_value={'authenticated': False, 'terminalActive': False,
                                         'importing': False, 'error': None, 'pendingConfirmation': True,
                                         'accountEmail': 'private-account@example.test'}),
            prepare=AsyncMock(), confirm=AsyncMock(), attach=AsyncMock(), stop_locked=AsyncMock(),
            logout=AsyncMock(), close=AsyncMock())
        self.app.state.runtime['admin_usage_auth'] = self.service

    def test_regular_user_cannot_access_profile_auth_or_identity(self):
        response = self.login_user()
        headers = {'Origin': fixtures.ORIGIN, 'X-CSRF-Token': response.json()['csrfToken']}
        for suffix in ('/status', '/login-browser'):
            self.assertEqual(self.client.get(PREFIX + suffix).status_code, 401)
        for suffix, body in (
            ('/prepare', {'confirmation': 'AUTHORIZE_USAGE_FOR_SHARED_ACCOUNT'}),
            ('/confirm', {'confirmation': 'CONFIRM_SAME_CLAUDE_ACCOUNT'}),
            ('/terminal/stop', None), ('/login-browser/stop', None),
            ('/login-browser', {'url': 'https://claude.com/cai/oauth/authorize?x=1'})):
            self.assertEqual(self.client.post(PREFIX + suffix, headers=headers, json=body).status_code, 401)
        for suffix in ('/terminal', '/login-browser/display'):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect('ws://localhost:8000' + PREFIX + suffix,
                                                   headers={'Origin': fixtures.ORIGIN}):
                    pass
        self.service.status.assert_not_awaited()
        self.service.prepare.assert_not_awaited()
        self.service.confirm.assert_not_awaited()
        self.service.attach.assert_not_awaited()

    def test_mutations_require_exact_origin_and_admin_csrf(self):
        good = self.login_admin()
        for bad in ({}, {'Origin': fixtures.ORIGIN}, {**good, 'Origin': 'https://evil.example'}):
            for suffix, body in (
                ('/prepare', {'confirmation': 'AUTHORIZE_USAGE_FOR_SHARED_ACCOUNT'}),
                ('/confirm', {'confirmation': 'CONFIRM_SAME_CLAUDE_ACCOUNT'}),
                ('/terminal/stop', None), ('/login-browser/stop', None)):
                self.assertEqual(self.client.post(PREFIX + suffix, headers=bad, json=body).status_code, 403)
        self.service.prepare.assert_not_awaited()
        self.service.confirm.assert_not_awaited()

    def test_admin_identity_status_and_logout_are_separate_from_worker_auth(self):
        headers = self.login_admin()
        token = self.client.cookies.get(ADMIN_COOKIE)
        self.assertEqual(self.client.get(PREFIX + '/status').json()['accountEmail'], 'private-account@example.test')
        self.assertEqual(self.client.post('/api/admin/logout', headers=headers).status_code, 200)
        self.service.logout.assert_awaited_once_with(token)
        self.assertEqual(self.client.get(PREFIX + '/status').status_code, 401)

    def test_two_authorization_services_cannot_start_two_remote_browsers(self):
        headers = self.login_admin()
        self.service.owner = self.client.cookies.get(ADMIN_COOKIE)
        other = SimpleNamespace(workspace_id='admin-auth',
                                browsers=SimpleNamespace(status=AsyncMock(return_value={'active': True})),
                                close=AsyncMock())
        self.app.state.runtime['admin_claude'] = other
        response = self.client.post(PREFIX + '/login-browser', headers=headers,
                                    json={'url': 'https://claude.com/cai/oauth/authorize?x=1'})
        self.assertEqual(response.status_code, 409)
        self.service.browsers.start.assert_not_awaited()

