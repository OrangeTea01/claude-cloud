"""Registration/payment browsing is admin-only and isolated per admin session."""
import asyncio
from datetime import datetime, timezone
import json
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from starlette.websockets import WebSocketDisconnect

from backend.account_browser import AccountBrowser, AccountBrowserBroker, PurposeBrowsers, PURPOSE_URLS, RESOURCE_ID
from backend.app import COOKIE, ADMIN_COOKIE
from backend.auth import Sessions
from backend.docker_backend import BrokerError, Config, DockerBroker, OWNER_LABEL, RESOURCE_LABEL
import test_admin as fixtures


class AccountBrowserBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = DockerBroker(Config(data_dir=self.temp.name))
        self.info = self.network = None
        self.connected = False
        self.parent.command = AsyncMock(side_effect=self.command)
        self.parent.check_engine = AsyncMock()
        self.facade = AccountBrowserBroker(self.parent)
        self.browsers = PurposeBrowsers(self.facade)
        self.sessions = Sessions()
        self.user = self.sessions.create()
        self.other = self.sessions.create()
        self.runtime = {'broker':self.parent,'terminals':SimpleNamespace(lock=asyncio.Lock()),
                        'admin_claude':None,'admin_usage_auth':None}
        self.manager = AccountBrowser(self.runtime,self.sessions,facade=self.facade,browsers=self.browsers)

    async def asyncTearDown(self):
        if self.info:self.info['Config']['Labels'][RESOURCE_LABEL]='account-browser'
        self.parent.command.side_effect=self.command
        await self.manager.close()
        self.parent.close()
        self.temp.cleanup()

    def command(self,*args,**kwargs):
        if args[:2]==('container','inspect'):
            if args[2]==self.parent.config.egress_container:
                names={self.facade.names(RESOURCE_ID)['network']:{}} if self.connected else {}
                return 0,json.dumps([{'State':{'Running':True},'NetworkSettings':{'Networks':names}}]).encode(),b''
            return (0,json.dumps([self.info]).encode(),b'') if self.info else (1,b'',b'no such container')
        if args[:2]==('network','inspect'):
            return (0,json.dumps([self.network]).encode(),b'') if self.network else (1,b'',b'no such network')
        labels={args[i+1].split('=',1)[0]:args[i+1].split('=',1)[1] for i,a in enumerate(args) if a=='--label'}
        if args[:2]==('network','create'):
            self.network={'Labels':labels,'Internal':True,'Options':{'com.docker.network.bridge.gateway_mode_ipv4':'isolated'}}
        elif args[:2]==('network','connect'):self.connected=True
        elif args[:2]==('network','disconnect'):self.connected=False
        elif args[:2]==('network','rm'):self.network=None
        elif args[:2]==('container','create'):
            self.info={'Config':{'Labels':labels},'State':{'Running':False}}
        elif args[:2]==('container','start'):self.info['State']['Running']=True
        elif args[:2]==('container','rm'):self.info=None
        return 0,b'',b''

    async def test_isolated_browser_same_egress_no_worker_or_persistent_credentials(self):
        metadata=await self.manager.open(self.user,'register')
        self.assertTrue(metadata['active'])
        self.assertEqual(metadata['purpose'],'register')
        self.assertRegex(metadata['browserId'],r'^[a-f0-9]{32}$')
        calls=self.parent.command.call_args_list
        create=next(c.args for c in calls if c.args[:2]==('container','create'))
        self.assertIn('--read-only',create);self.assertIn('--cap-drop',create)
        self.assertNotIn('--mount',create);self.assertNotIn('--volume',create)
        self.assertNotIn('docker.sock',' '.join(create));self.assertNotIn('/workspace',' '.join(create))
        self.assertEqual(create[create.index('--network')+1],self.facade.names(RESOURCE_ID)['network'])
        net=next(c.args for c in calls if c.args[:2]==('network','create'))
        self.assertIn('--internal',net);self.assertIn('com.docker.network.bridge.gateway_mode_ipv4=isolated',net)
        conn=next(c.args for c in calls if c.args[:2]==('network','connect'))
        self.assertEqual(conn[-1],self.parent.config.egress_container)
        self.assertEqual(self.parent.db.execute('SELECT count(*) FROM workspaces').fetchone()[0],0)
        self.assertFalse(any(c.args[0]=='volume' for c in calls))

    async def test_switching_fixed_entry_reuses_profile_and_original_expiry(self):
        first=await self.manager.open(self.user,'register')
        for purpose in ('billing','usage'):
            result=await self.manager.open(self.user,purpose,first['browserId'])
            self.assertEqual(result['browserId'],first['browserId']);self.assertEqual(result['expiresAt'],first['expiresAt'])
        creates=[c for c in self.parent.command.call_args_list if c.args[:2]==('container','create')]
        self.assertEqual(len(creates),1)
        urls=[json.loads(c.kwargs['input_data'])['url'] for c in self.parent.command.call_args_list if c.args[-1]=='open']
        self.assertEqual(urls,[PURPOSE_URLS[p] for p in ('register','billing','usage')])
        for call in self.parent.command.call_args_list:
            self.assertFalse(any(url in call.args for url in urls))

    async def test_cross_admin_sessions_cannot_view_navigate_or_stop(self):
        first=await self.manager.open(self.user,'register')
        same_user=self.sessions.create(self.user.user_id)
        for stranger in (self.other,same_user):
            for operation in (self.manager.status(stranger),self.manager.open(stranger,'billing',first['browserId']),self.manager.stop(stranger,first['browserId'])):
                with self.assertRaises(BrokerError) as error:await operation
                self.assertEqual(error.exception.status,409)
        self.assertTrue(self.manager.valid(self.user,first['browserId']))

    async def test_stale_browser_id_cannot_stop_or_navigate_new_profile(self):
        first=await self.manager.open(self.user,'register')
        await self.manager.stop(self.user,first['browserId'])
        second=await self.manager.open(self.user,'billing')
        self.assertNotEqual(first['browserId'],second['browserId'])
        with self.assertRaises(BrokerError):await self.manager.stop(self.user,first['browserId'])
        with self.assertRaises(BrokerError):await self.manager.open(self.user,'usage',first['browserId'])
        with self.assertRaises(BrokerError):await self.manager.open(self.user,'usage')
        self.assertTrue(self.manager.valid(self.user,second['browserId']))

    async def test_fixed_pages_only_and_shared_auth_validator_still_strict(self):
        for url in ('https://evil.example','https://claude.ai/settings/billing?next=evil','http://127.0.0.1'):
            with self.assertRaises(BrokerError):self.browsers.validate_url(url)
        for purpose in ('http://127.0.0.1','anything','https://claude.ai/login'):
            with self.assertRaises(BrokerError):await self.manager.open(self.user,purpose)
        self.parent.command.assert_not_called()

    async def test_expiry_and_revocation_destroy_container_network_and_cookie_memory(self):
        await self.manager.open(self.user,'register')
        self.manager.expires=time.time()-1
        async with self.manager.lock:await self.manager.prune_locked()
        self.assertIsNone(self.info);self.assertIsNone(self.network);self.assertIsNone(self.manager.owner)
        await self.manager.open(self.user,'billing')
        self.sessions.remove(self.user.token)
        self.manager.start_watcher()
        await asyncio.sleep(1.1)
        self.assertIsNone(self.info);self.assertIsNone(self.network);self.assertIsNone(self.manager.owner)

    async def test_user_identity_is_denied_even_if_added_to_admin_session_store(self):
        ordinary_user=self.sessions.create('a'*32)
        for operation in (self.manager.status(ordinary_user),self.manager.open(ordinary_user,'register'),self.manager.stop(ordinary_user,'b'*32)):
            with self.assertRaises(BrokerError) as error:await operation
            self.assertEqual(error.exception.status,401)
        self.parent.command.assert_not_called()

    async def test_admin_browser_does_not_require_a_dashboard_user(self):
        first=await self.manager.open(self.user,'register')
        self.assertIsNone(self.user.user_id)
        self.assertTrue(self.manager.valid(self.user,first['browserId']))
        async with self.manager.lock:await self.manager.prune_locked()
        self.assertIsNotNone(self.info);self.assertIsNotNone(self.network)

    async def test_foreign_session_store_cannot_impersonate_an_admin(self):
        foreign=Sessions().create()
        with self.assertRaises(BrokerError) as error:await self.manager.open(foreign,'register')
        self.assertEqual(error.exception.status,401)
        self.parent.command.assert_not_called()

    async def test_start_failure_cleans_up_network_without_reusing_profile(self):
        original=self.command
        def fail(*args,**kwargs):
            if args[:2]==('container','start'):raise BrokerError('could not start',503)
            return original(*args,**kwargs)
        self.parent.command.side_effect=fail
        with self.assertRaises(BrokerError):await self.manager.open(self.user,'register')
        self.assertIsNone(self.info);self.assertIsNone(self.network);self.assertIsNone(self.manager.owner)

    async def test_failed_cleanup_retains_busy_owner_never_reassigns_profile(self):
        first=await self.manager.open(self.user,'register')
        original=self.command
        def fail(*args,**kwargs):
            if args[:2]==('container','rm'):raise BrokerError('Docker down',503)
            return original(*args,**kwargs)
        self.parent.command.side_effect=fail
        with self.assertRaises(BrokerError):await self.manager.stop(self.user,first['browserId'])
        self.assertEqual(self.manager.owner,self.user.token)
        with self.assertRaises(BrokerError):await self.manager.open(self.other,'billing')

    async def test_reconcile_cleans_only_dedicated_owned_resource(self):
        await self.manager.open(self.user,'register')
        self.manager.reconciled=False
        await self.manager.reconcile()
        self.assertIsNone(self.info);self.assertIsNone(self.network)
        removals=[c.args for c in self.parent.command.call_args_list if c.args[:2] in (('container','rm'),('network','rm'))]
        self.assertTrue(removals)
        self.assertTrue(all('account-browser' in c[-1] for c in removals))

    async def test_wrong_resource_role_is_never_removed(self):
        first=await self.manager.open(self.user,'register')
        self.info['Config']['Labels'][RESOURCE_LABEL]='admin-auth'
        self.parent.command.reset_mock()
        with self.assertRaises(BrokerError):await self.manager.stop(self.user,first['browserId'])
        self.assertFalse(any(c.args[:2]==('container','rm') for c in self.parent.command.call_args_list))

    async def test_account_and_admin_browser_share_budget(self):
        self.runtime['admin_claude']=SimpleNamespace(workspace_id='admin-auth',browsers=SimpleNamespace(status=AsyncMock(return_value={'active':True})))
        with self.assertRaises(BrokerError) as error:await self.manager.open(self.user,'billing')
        self.assertEqual(error.exception.status,409)
        self.assertFalse(any(c.args[:2]==('container','create') for c in self.parent.command.call_args_list))


class AccountBrowserApiTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.AdminApiTests.setUpClass.__func__)
    start=fixtures.AdminApiTests.start
    login_user=fixtures.AdminApiTests.login_user
    login_admin=fixtures.AdminApiTests.login_admin

    def setUp(self):
        fixtures.AdminApiTests.setUp(self)
        self.facade=SimpleNamespace(prepare=AsyncMock(),cleanup_network=AsyncMock())
        self.active=False
        async def start_browser(resource,url):
            self.active=True
            return {'active':True,'expiresAt':datetime.fromtimestamp(time.time()+1800,timezone.utc).isoformat()}
        async def stop_browser(resource):self.active=False
        self.display=SimpleNamespace(start=AsyncMock(side_effect=start_browser),stop=AsyncMock(side_effect=stop_browser),
            navigate=AsyncMock(),status=AsyncMock(side_effect=lambda _: {'active':self.active}),close=AsyncMock(),
            open_channel=AsyncMock(),release_channel=AsyncMock())
        self.service=AccountBrowser(self.app.state.runtime,self.app.state.admin_sessions,facade=self.facade,browsers=self.display)
        self.app.state.runtime['account_browser']=self.service
        self.prefix='/api/admin/claude-account-browser'

    def tearDown(self):fixtures.AdminApiTests.tearDown(self)

    def auth(self):
        return self.login_admin()

    def test_requires_admin_login_exact_origin_csrf_and_fixed_enum(self):
        self.assertEqual(self.client.get(self.prefix).status_code,401)
        user=self.login_user()
        self.assertEqual(self.client.get(self.prefix).status_code,401)
        headers=self.auth()
        for bad in ({},{'Origin':fixtures.ORIGIN},{**headers,'Origin':'https://evil.example'},
                    {**headers,'X-CSRF-Token':user.json()['csrfToken']}):
            self.assertEqual(self.client.post(self.prefix,headers=bad,json={'purpose':'billing'}).status_code,403)
        for bad in ({'purpose':'invalid'},{'purpose':'register','url':'https://evil.example'},{'purpose':'billing','browserId':'other'}):
            self.assertEqual(self.client.post(self.prefix,headers=headers,json=bad).status_code,422)
        self.display.start.assert_not_awaited()

    def test_user_routes_retired_and_user_cookie_cannot_be_renamed_as_admin(self):
        user=self.login_user()
        user_token=self.client.cookies.get(COOKIE)
        headers={'Origin':fixtures.ORIGIN,'X-CSRF-Token':user.json()['csrfToken']}
        old='/api/account/claude-browser'
        for path,body in ((old,{'purpose':'register'}),(old+'/stop',{'browserId':'a'*32})):
            self.assertEqual(self.client.post(path,headers=headers,json=body).status_code,404)
        self.assertEqual(self.client.get(old).status_code,404)
        for cookies in (f'{COOKIE}={user_token}',f'{ADMIN_COOKIE}={user_token}'):
            auth={**headers,'Cookie':cookies}
            self.assertEqual(self.client.get(self.prefix,headers=auth).status_code,401)
            self.assertEqual(self.client.post(self.prefix,headers=auth,json={'purpose':'register'}).status_code,401)
            self.assertEqual(self.client.post(self.prefix+'/stop',headers=auth,json={'browserId':'a'*32}).status_code,401)
            for path in (old,self.prefix):
                with self.assertRaises(WebSocketDisconnect) as error:
                    with self.client.websocket_connect('ws://localhost:8000'+path+'/display',headers=auth):pass
                self.assertEqual(error.exception.code,4403)
        self.display.start.assert_not_awaited();self.display.open_channel.assert_not_awaited()
        self.facade.prepare.assert_not_awaited()

    def test_disabled_management_rejects_http_and_display_even_with_admin_cookie(self):
        headers=self.auth()
        self.config.management_password_hash=''
        self.assertEqual(self.client.get(self.prefix).status_code,503)
        self.assertEqual(self.client.post(self.prefix,headers=headers,json={'purpose':'billing'}).status_code,503)
        self.assertEqual(self.client.post(self.prefix+'/stop',headers=headers,json={'browserId':'a'*32}).status_code,503)
        with self.assertRaises(WebSocketDisconnect) as error:
            with self.client.websocket_connect('ws://localhost:8000'+self.prefix+'/display',headers={'Origin':fixtures.ORIGIN}):pass
        self.assertEqual(error.exception.code,4403)
        self.display.start.assert_not_awaited();self.display.open_channel.assert_not_awaited()

    def test_user_logout_and_user_revocation_do_not_close_admin_browser(self):
        user=self.login_user()
        headers=self.auth()
        first=self.client.post(self.prefix,headers=headers,json={'purpose':'billing'}).json()
        self.display.stop.reset_mock()
        self.assertEqual(self.client.post('/api/logout',headers={'Origin':fixtures.ORIGIN,'X-CSRF-Token':user.json()['csrfToken']}).status_code,200)
        self.app.state.sessions.revoke_all()
        self.assertEqual(self.client.get(self.prefix).json(),first)
        self.assertTrue(self.active);self.display.stop.assert_not_awaited()
        self.assertEqual(self.client.post('/api/admin/logout',headers=headers).status_code,200)
        self.assertFalse(self.active);self.assertIsNone(self.service.owner)

    def test_display_requires_owner_admin_session_and_exact_origin(self):
        headers=self.auth()
        first=self.client.post(self.prefix,headers=headers,json={'purpose':'register'}).json()
        url='ws://localhost:8000'+self.prefix+'/display'
        for origin in ('https://evil.example',fixtures.ORIGIN+'/'):
            with self.assertRaises(WebSocketDisconnect) as error:
                with self.client.websocket_connect(url,headers={'Origin':origin}):pass
            self.assertEqual(error.exception.code,4403)
        other=self.app.state.admin_sessions.create()
        with self.client.websocket_connect(url,headers={'Origin':fixtures.ORIGIN,'Cookie':f'{ADMIN_COOKIE}={other.token}'}) as ws:
            ws.send_json({'type':'auth','csrfToken':other.csrf,'browserId':first['browserId']})
            with self.assertRaises(WebSocketDisconnect) as error:ws.receive_bytes()
            self.assertEqual(error.exception.code,4403)
        self.display.open_channel.assert_not_awaited()

    def test_state_navigation_session_isolation_and_logout_cleanup(self):
        headers=self.auth()
        self.assertFalse(self.client.get(self.prefix).json()['active'])
        first=self.client.post(self.prefix,headers=headers,json={'purpose':'register'}).json()
        self.assertEqual(self.client.get(self.prefix).json(),first)
        self.assertEqual(self.client.post(self.prefix,headers=headers,json={'purpose':'billing','browserId':first['browserId']}).status_code,200)
        token=self.client.cookies.get(ADMIN_COOKIE)
        session=self.app.state.admin_sessions.get(token)
        self.assertIsNone(session.user_id)
        other=self.app.state.admin_sessions.create()
        other_headers={'Cookie':f'{ADMIN_COOKIE}={other.token}','Origin':fixtures.ORIGIN,'X-CSRF-Token':other.csrf}
        self.assertEqual(self.client.get(self.prefix,headers=other_headers).status_code,409)
        self.assertEqual(self.client.post(self.prefix+'/stop',headers=other_headers,json={'browserId':first['browserId']}).status_code,409)
        self.assertEqual(self.client.post('/api/admin/logout',headers=other_headers).status_code,200)
        self.assertEqual(self.service.owner,token)
        # The synthetic other-admin request deletes the client's admin cookie;
        # restore the actual owner cookie for its explicit logout.
        owner_headers={**headers,'Cookie':f'{ADMIN_COOKIE}={token}'}
        self.assertEqual(self.client.post('/api/admin/logout',headers=owner_headers).status_code,200)
        self.assertIsNone(self.service.owner);self.assertFalse(self.active)

    def test_session_revoked_while_starting_returns_no_browser(self):
        headers=self.auth()
        original=self.display.start.side_effect
        async def start(resource,url):
            result=await original(resource,url)
            self.app.state.admin_sessions.revoke_all()
            return result
        self.display.start.side_effect=start
        self.assertEqual(self.client.post(self.prefix,headers=headers,json={'purpose':'register'}).status_code,401)
        self.assertFalse(self.active);self.assertIsNone(self.service.owner)

    def test_binary_display_scoped_to_session_lease_and_revocation(self):
        headers=self.auth()
        first=self.client.post(self.prefix,headers=headers,json={'purpose':'register'}).json()
        proc=SimpleNamespace(stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()))
        async def channel(_):
            proc.stdout=asyncio.StreamReader();proc.stdout.feed_data(b'RFB 003.008\n')
            return proc
        self.display.open_channel.side_effect=channel
        url='ws://localhost:8000'+self.prefix+'/display'
        for fields in ({'csrfToken':'wrong','browserId':first['browserId']},{'csrfToken':headers['X-CSRF-Token'],'browserId':'a'*32}):
            with self.client.websocket_connect(url,headers={'Origin':fixtures.ORIGIN}) as ws:
                ws.send_json({'type':'auth',**fields})
                with self.assertRaises(WebSocketDisconnect):ws.receive_bytes()
        self.display.open_channel.assert_not_awaited()
        with self.client.websocket_connect(url,headers={'Origin':fixtures.ORIGIN}) as ws:
            ws.send_json({'type':'auth','csrfToken':headers['X-CSRF-Token'],'browserId':first['browserId']})
            self.assertEqual(ws.receive_bytes(),b'RFB 003.008\n')
            self.app.state.admin_sessions.revoke_all()
            ws.send_bytes(b'late payment input')
            with self.assertRaises(WebSocketDisconnect):ws.receive_bytes()
        proc.stdin.write.assert_not_called()
        self.display.release_channel.assert_awaited_once_with(RESOURCE_ID,proc)

    def test_old_workspace_and_admin_authorization_still_inaccessible_to_user(self):
        response=self.login_user()
        headers={'Origin':fixtures.ORIGIN,'X-CSRF-Token':response.json()['csrfToken']}
        self.assertEqual(self.client.get('/api/admin/claude/login-browser').status_code,401)
        self.assertEqual(self.client.get('/api/workspaces/'+fixtures.WORKSPACE+'/login-browser').status_code,403)
        self.assertEqual(self.client.post('/api/workspaces/'+fixtures.WORKSPACE+'/login-browser',headers=headers,json={'url':PURPOSE_URLS['register']}).status_code,403)

    def test_admin_auth_browser_respects_current_account_browser_budget(self):
        headers=self.auth()
        self.assertEqual(self.client.post(self.prefix,headers=headers,json={'purpose':'billing'}).status_code,200)
        admin_headers=headers
        service=SimpleNamespace(owner=self.client.cookies.get(ADMIN_COOKIE),workspace_id='admin-auth',
            terminals=SimpleNamespace(lock=self.service.lock,active=lambda _:True),
            browsers=SimpleNamespace(start=AsyncMock(),status=AsyncMock(return_value={'active':False})),close=AsyncMock())
        self.app.state.runtime['admin_claude']=service
        response=self.client.post('/api/admin/claude/login-browser',headers=admin_headers,json={'url':'https://claude.ai/oauth/authorize?example'})
        self.assertEqual(response.status_code,409)
        service.browsers.start.assert_not_awaited()


if __name__=='__main__':unittest.main()
