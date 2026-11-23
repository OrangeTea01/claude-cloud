"""Bounded failover for read-only official meter queries."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.official_usage import UsageUnavailable
from backend.usage_control import UsageControl


class UsageSourceTests(unittest.IsolatedAsyncioTestCase):
    def control(self, sessions=(), workspaces=()):
        control = object.__new__(UsageControl)
        control.official_fetcher = None
        control.chats = SimpleNamespace(items={str(index): session for index, session in enumerate(sessions)})
        control.broker = SimpleNamespace(list=AsyncMock(return_value=workspaces),
                                        chat_command=AsyncMock(return_value=['docker', '/usr/bin/timeout', '--signal=TERM', '--kill-after=5s', '6h', 'fixed-helper']))
        return control

    def session(self, *, result=None, error=None, initializing=False):
        ready = asyncio.get_running_loop().create_future()
        if not initializing:
            ready.set_result(True)
        return SimpleNamespace(proc=SimpleNamespace(returncode=None), closing=False,
                               init_future=ready, get_usage=AsyncMock(return_value=result, side_effect=error))

    async def test_broken_first_live_client_does_not_hide_healthy_second(self):
        first = self.session(error=asyncio.TimeoutError())
        second = self.session(result={'rate_limits_available': True})
        control = self.control([first, second])
        self.assertEqual(await control.fetch_official(), {'rate_limits_available': True})
        first.get_usage.assert_awaited_once()
        second.get_usage.assert_awaited_once()
        control.broker.list.assert_not_awaited()

    async def test_failure_is_bounded_to_two_sources_and_never_echoes_raw_error(self):
        sessions = [self.session(error=RuntimeError('private-auth-token')) for _ in range(5)]
        control = self.control(sessions)
        with self.assertRaises(UsageUnavailable) as caught:
            await control.fetch_official()
        self.assertEqual(caught.exception.code, 'query_failed')
        self.assertNotIn('private-auth-token', str(caught.exception))
        self.assertEqual(sum(session.get_usage.await_count for session in sessions), 2)

    async def test_one_shot_fallback_retains_short_container_deadline(self):
        first = self.session(error=RuntimeError('closed client'))
        control = self.control([first], [{'id':'test-workspace','running':True}])
        with patch('backend.usage_control.fetch_usage_command', new_callable=AsyncMock, return_value={'ok': True}) as query:
            self.assertEqual(await control.fetch_official(), {'ok': True})
        command = query.await_args.args[0]
        self.assertIn('25s', command)
        self.assertNotIn('6h', command)

    async def test_no_running_workspace_has_specific_safe_error(self):
        control = self.control(workspaces=[{'id':'test-workspace','running':False}])
        with self.assertRaises(UsageUnavailable) as caught:
            await control.fetch_official()
        self.assertEqual(caught.exception.code, 'no_running_workspace')
        control.broker.chat_command.assert_not_awaited()

    async def test_cancellation_does_not_launch_alternate_process(self):
        first = self.session(error=asyncio.CancelledError())
        second = self.session(result={'ok':True})
        control = self.control([first, second])
        with self.assertRaises(asyncio.CancelledError):
            await control.fetch_official()
        second.get_usage.assert_not_awaited()

    async def test_failed_standalone_worker_can_use_second_running_workspace(self):
        control = self.control(workspaces=[{'id':'first','running':True},{'id':'second','running':True}])
        with patch('backend.usage_control.fetch_usage_command', new_callable=AsyncMock,
                   side_effect=[UsageUnavailable(code='query_timeout'), {'ok': True}]) as query:
            self.assertEqual(await control.fetch_official(), {'ok': True})
            self.assertEqual(query.await_count, 2)

    async def test_private_admin_query_needs_no_user_workspaces(self):
        control = self.control()
        control.official_fetcher = AsyncMock(return_value={'rate_limits_available': True})
        self.assertEqual(await control.fetch_official(), {'rate_limits_available': True})
        control.broker.list.assert_not_awaited()
        control.broker.chat_command.assert_not_awaited()

    async def test_missing_private_authorization_never_falls_back_to_worker_token(self):
        session = self.session(result={'rate_limits_available': True})
        control = self.control([session])
        control.official_fetcher = AsyncMock(side_effect=UsageUnavailable(code='usage_authorization_required'))
        with self.assertRaises(UsageUnavailable) as caught:
            await control.fetch_official()
        self.assertEqual(caught.exception.code, 'usage_authorization_required')
        session.get_usage.assert_not_awaited()
        control.broker.list.assert_not_awaited()
