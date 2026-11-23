"""OAuth routing and temporary browser Docker boundary regression checks."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from backend.docker_backend import Config, DockerBroker, BrokerError, OWNER_LABEL, WORKSPACE_LABEL
from backend.login_browser import LoginBrowsers, ROLE_LABEL, EXPIRY_LABEL, ROLE, validate_login_url

URL = ('https://claude.com/cai/oauth/authorize?response_type=code&client_id=test-client'
       '&state=random-state&code_challenge=example-challenge&code_challenge_method=S256'
       '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback')


class LoginURLTests(unittest.TestCase):
    def test_current_official_url_is_preserved_exactly(self):
        self.assertEqual(validate_login_url(URL), URL)

    def test_rejects_arbitrary_or_ambiguous_navigation(self):
        variants = [
            URL.replace('claude.com', 'claude.com.evil.example', 1),
            URL.replace('https://', 'http://', 1),
            URL.replace('claude.com', 'user@claude.com', 1),
            URL.replace('claude.com', 'claude.com:8443', 1),
            URL.replace('/cai/oauth/authorize', '/login', 1),
            URL.replace('/cai/oauth/authorize', '/cai/oauth/%61uthorize', 1),
            URL.replace('claude.com', '127.0.0.1', 1),
            URL.replace('claude.com', 'claude.com\\@evil.example', 1),
            URL.replace('claude.com', 'claude.com%00.evil.example', 1),
            URL + '#fragment', URL + '\n', URL + '&state=duplicate',
            URL.replace('response_type=code', 'response_type=token'),
            URL.replace('&state=random-state', ''),
            URL.replace('code_challenge_method=S256', 'code_challenge_method=plain'),
            URL.replace('state=random-state', 'state=%0a'),
            URL.replace('example-challenge', '%zz'),
            URL.replace('platform.claude.com%2Foauth%2Fcode%2Fcallback', 'evil.example%2Fcallback'),
            URL + 'a' * 17000,
        ]
        for url in variants:
            with self.subTest(url=url[:150]), self.assertRaises(BrokerError):
                validate_login_url(url)

    def test_validation_errors_identify_problem_without_exposing_values(self):
        secret = "private-session-value-never-in-error"
        examples = [
            (URL.replace('&client_id=', 'client_id='), "client_id"),
            (URL + '&state=' + secret, "重复参数：state"),
            (URL.replace('&code_challenge=', '&code_challenge=%zz'), "百分号编码"),
            (URL + '\r\n', "空白或无效字符"),
        ]
        for url, expected in examples:
            with self.subTest(expected=expected), self.assertRaises(BrokerError) as raised:
                validate_login_url(url.replace('random-state', secret))
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn('https://', str(raised.exception))


class LoginBrowserBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.broker = DockerBroker(Config(data_dir=self.temporary.name))
        self.workspace_id = 'a' * 32
        self.broker.db.execute('INSERT INTO workspaces (id,name,created_at) VALUES (?,?,?)', (self.workspace_id, 'test', 'now'))
        self.broker.db.commit()
        self.manager = LoginBrowsers(self.broker)
        self.info = None
        self.pending = None
        self.broker.workspace = AsyncMock(return_value={'running': True})
        self.broker.inspect_owned = AsyncMock(side_effect=self.inspect)
        self.broker.command = AsyncMock(side_effect=self.command)

    def inspect(self, kind, name, workspace_id):
        if kind == 'network':
            return {'Internal': True, 'Options': {'com.docker.network.bridge.gateway_mode_ipv4': 'isolated'}}
        return self.info

    def command(self, *args, **kwargs):
        if args[:2] == ('container', 'create'):
            labels = {args[i + 1].split('=', 1)[0]: args[i + 1].split('=', 1)[1]
                      for i, arg in enumerate(args) if arg == '--label'}
            self.info = {'Config': {'Labels': labels}, 'State': {'Running': False}}
        elif args[:2] == ('container', 'start'):
            self.info['State']['Running'] = True
        elif args[:2] == ('container', 'rm'):
            self.info = None
        return 0, b'', b''

    async def asyncTearDown(self):
        # A test may deliberately change the role label; restore it for cleanup.
        if self.info:
            self.info['Config']['Labels'][ROLE_LABEL] = ROLE
        await self.manager.close()
        self.broker.close()
        self.temporary.cleanup()

    async def test_start_limits_container_and_passes_url_only_via_stdin(self):
        result = await self.manager.start(self.workspace_id, URL)
        self.assertTrue(result['active'])
        calls = self.broker.command.call_args_list
        create = next(call.args for call in calls if call.args[:2] == ('container', 'create'))
        self.assertEqual(create[create.index('--user') + 1], '1000:1000')
        self.assertEqual(create[create.index('--cap-drop') + 1], 'ALL')
        self.assertEqual(create[create.index('--network') + 1], self.broker.names(self.workspace_id)['network'])
        self.assertEqual(create[create.index('--memory') + 1], '768m')
        self.assertIn('--init', create)
        self.assertIn('--read-only', create)
        self.assertIn('no-new-privileges=true', create)
        self.assertIn('seccomp=' + self.manager.seccomp_path, create)
        for forbidden in ('--publish', '--mount', '--volume', '--privileged', '--cap-add', '--ipc', '--pid'):
            self.assertNotIn(forbidden, create)
        self.assertFalse(any('docker.sock' in arg for arg in create))
        for call in calls:
            self.assertNotIn(URL, call.args)
        navigation = next(call for call in calls if call.args[-1] == 'open')
        self.assertEqual(json.loads(navigation.kwargs['input_data']), {'url': URL})

    async def test_bad_url_and_unknown_workspace_never_reach_docker(self):
        with self.assertRaises(BrokerError):
            await self.manager.start(self.workspace_id, 'https://evil.example/')
        with self.assertRaises(BrokerError):
            await self.manager.start('b' * 32, URL)
        self.broker.command.assert_not_called()

    async def test_stopped_workspace_cannot_start_browser(self):
        self.broker.workspace.return_value = {'running': False}
        with self.assertRaises(BrokerError):
            await self.manager.start(self.workspace_id, URL)
        self.broker.command.assert_not_called()

    async def test_role_mismatch_is_never_removed(self):
        await self.manager.start(self.workspace_id, URL)
        self.info['Config']['Labels'][ROLE_LABEL] = 'something-else'
        self.broker.command.reset_mock()
        with self.assertRaises(BrokerError):
            await self.manager.stop(self.workspace_id)
        self.broker.command.assert_not_called()

    async def test_expired_browser_is_removed_and_not_reconnected(self):
        await self.manager.start(self.workspace_id, URL)
        self.info['Config']['Labels'][EXPIRY_LABEL] = str(time.time() - 1)
        self.assertFalse((await self.manager.status(self.workspace_id))['active'])
        self.assertIsNone(self.info)
        with self.assertRaises(BrokerError):
            await self.manager.open_channel(self.workspace_id)

    async def test_unready_sandbox_is_removed_on_failed_start(self):
        original = self.command
        def failed(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('container', 'start'):
                self.info['State']['Running'] = False
            return result
        self.broker.command.side_effect = failed
        with self.assertRaises(BrokerError):
            await self.manager.start(self.workspace_id, URL)
        self.assertIsNone(self.info)
        self.assertFalse(self.manager.leases)

    async def test_reconcile_accepts_existing_registry_snapshot(self):
        await self.manager.start(self.workspace_id, URL)
        self.broker.list = AsyncMock()
        await self.manager.reconcile([{'id': self.workspace_id}])
        self.broker.list.assert_not_called()
        self.assertIsNone(self.info)

    async def test_shutdown_cancels_lease_even_when_docker_is_unavailable(self):
        await self.manager.start(self.workspace_id, URL)
        lease = self.manager.leases[self.workspace_id]
        self.broker.inspect_owned.side_effect = BrokerError('Docker unavailable', 503)
        await self.manager.close()
        self.assertTrue(lease.done())
        self.assertFalse(self.manager.leases)
        self.assertFalse(self.manager.channels)
        self.broker.inspect_owned.side_effect = self.inspect

    async def test_removal_checks_broker_ownership_before_channel_changes(self):
        await self.manager.start(self.workspace_id, URL)
        self.broker.inspect_owned.side_effect = BrokerError('owner mismatch', 409)
        self.broker.command.reset_mock()
        with self.assertRaises(BrokerError):
            await self.manager.stop(self.workspace_id)
        self.broker.command.assert_not_called()
        self.broker.inspect_owned.side_effect = self.inspect


class BrowserSandboxConfigurationTests(unittest.TestCase):
    def test_default_deny_and_user_namespace_profile_remain_enabled(self):
        path = Path(__file__).resolve().parents[1] / 'deploy/login_browser/seccomp.json'
        profile = json.loads(path.read_text())
        self.assertEqual(profile['defaultAction'], 'SCMP_ACT_ERRNO')
        namespace = next(rule for rule in profile['syscalls'] if rule.get('comment') == 'Allow create user namespaces')
        self.assertEqual(set(namespace['names']), {'clone', 'setns', 'unshare'})
        self.assertEqual(namespace['action'], 'SCMP_ACT_ALLOW')
        clone3 = next(rule for rule in profile['syscalls'] if rule['names'] == ['clone3'])
        self.assertEqual(clone3['errnoRet'], 38)
        chroot = next(rule for rule in profile['syscalls'] if rule['names'] == ['chroot'])
        self.assertEqual(chroot['action'], 'SCMP_ACT_ALLOW')
        self.assertEqual(chroot['includes'], {})


if __name__ == '__main__':
    unittest.main()
