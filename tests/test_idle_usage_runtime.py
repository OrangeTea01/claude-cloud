"""Official idle windows admit a first task without losing its later debit."""
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch

from backend.docker_backend import BrokerError
from backend.official_shares import OfficialShares
from backend.official_usage import OfficialUsage
from backend.quotas import QuotaStore, UsageTracker, iso
from test_chat import FakeProcess, permission_frame, until
from test_multiuser_api import LEGACY, USER_HASH
import test_usage_runtime as runtime_fixtures
from test_usage_runtime import OTHER


class IdleUsageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    connected = runtime_fixtures.UsageRuntimeTests.connected
    frame = runtime_fixtures.UsageRuntimeTests.frame

    async def asyncSetUp(self):
        await runtime_fixtures.UsageRuntimeTests.asyncSetUp(self)
        self.official = OfficialUsage(self.directory.name, clock=lambda: self.now.timestamp(), refresh_interval=0)
        self.shares = OfficialShares(self.quotas)
        self.usage.official, self.usage.shares = self.official, self.shares
        self.selected = [self.owner, self.other] + [self.credentials.create_user(f"idle-user-{index}", USER_HASH)["id"]
                                                  for index in range(3)]
        self.week = self.now + timedelta(days=7)
        self.five = self.now + timedelta(hours=5)
        self.fetch = AsyncMock(return_value=self.report())
        self.usage.official_fetcher = self.fetch
        self.shares.configure(self.selected)
        await self.usage.refresh()

    async def asyncTearDown(self):
        self.official.close()
        await runtime_fixtures.UsageRuntimeTests.asyncTearDown(self)

    def report(self, five=0, week=6, *, active=False):
        return {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": five, "resets_at": iso(self.five) if active else None},
            "seven_day": {"utilization": week, "resets_at": iso(self.week)}}}

    async def refresh(self, payload=None):
        self.now += timedelta(seconds=1)
        self.fetch.return_value = self.report() if payload is None else payload
        return await self.usage.refresh()

    async def enforcement(self):
        if self.usage.enforcement_task:
            await self.usage.enforcement_task

    def result(self, amount=100):
        return {"type": "result", "is_error": False,
                "modelUsage": {"sonnet": {"inputTokens": amount, "outputTokens": 0}}}

    async def test_confirmed_idle_admits_and_writes_first_real_user_message(self):
        quota = self.usage.quota(LEGACY)
        self.assertFalse(quota["exhausted"])
        self.assertEqual(quota["fiveHour"]["state"], "idle")
        self.assertEqual(quota["fiveHour"]["limit"], 20)
        self.assertEqual(quota["fiveHour"]["remaining"], 20)
        self.assertIsNone(quota["fiveHour"]["resetAt"])
        self.assertEqual(quota["weekly"]["remaining"], 18.8)
        self.usage.admit(LEGACY)
        session, process = self.connected(phase="idle")
        await self.chats.send(LEGACY, "开始第一个任务")
        await session.input_task
        self.assertFalse(session.quota_paused)
        self.assertEqual(len(process.stdin.messages), 1)
        self.assertEqual(process.stdin.messages[0]["type"], "user")
        self.assertEqual(process.stdin.messages[0]["message"]["content"], "开始第一个任务")
        self.assertTrue(session.messages[0]["sent"])
        self.broker.stop.assert_not_awaited()

    async def test_init_control_and_partial_rate_headers_do_not_pause_idle_first_task(self):
        session, _ = self.connected(phase="starting")
        idle_since = self.official.snapshot()["fiveHour"]["idleSince"]
        for frame in (
            {"type": "system", "subtype": "init", "session_id": session.session_id, "model": "sonnet"},
            {"type": "control_response", "response": {"subtype": "success", "request_id": "other-control", "response": {}}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                                                  "resetsAt": self.five.timestamp()}},
        ):
            self.now += timedelta(seconds=1)
            await session.handle(frame)
            await self.enforcement()
            self.assertFalse(session.quota_paused)
            self.assertFalse(self.usage.quota(LEGACY)["exhausted"])
        # A real deadline can bind before numeric usage arrives, but must not
        # turn the original zero measurement into a newer observation.
        self.assertEqual(self.official.snapshot()["fiveHour"]["updatedAt"], idle_since)
        self.assertEqual(self.official.snapshot()["fiveHour"]["resetAt"], iso(self.five))
        await session.handle(permission_frame())
        self.assertEqual(session.phase, "awaiting_permission")
        self.assertFalse(session.quota_paused)
        self.broker.stop.assert_not_awaited()

    async def test_new_cli_initialization_delivers_first_prompt_and_tracks_its_first_result(self):
        session = self.chats.get(LEGACY)
        self.broker.raw(LEGACY)["running"] = True
        self.broker.chat_command = AsyncMock(return_value=["fake-claude"])
        process = FakeProcess()
        self.processes.append(process)
        with patch("backend.chat.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            await self.chats.send(LEGACY, "启动全新会话")
            await until(lambda: bool(process.stdin.messages))
            initialize = process.stdin.messages[0]
            self.assertEqual(initialize["request"]["subtype"], "initialize")
            session_id = self.broker.chat_command.call_args.args[2]
            process.emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "sonnet"})
            process.emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": initialize["request_id"], "response": {}}})
            await session.input_task
            self.assertEqual(process.stdin.messages[-1]["message"]["content"], "启动全新会话")
            self.assertFalse(session.quota_paused)
            process.emit({**self.result(100), "session_id": session_id})
            await until(lambda: session.phase == "idle")
            await self.enforcement()
        await self.refresh(self.report(5, 8, active=True))
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 5)
        self.assertEqual(self.usage.quota(LEGACY)["weekly"]["used"], 2)
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 100)
        self.assertFalse(session.quota_paused)

    async def test_first_active_meter_waits_for_assistant_and_keeps_original_allowance(self):
        session, _ = self.connected()
        self.now += timedelta(seconds=1)
        await session.handle({"type": "rate_limit_event", **self.report(5, 8, active=True)})
        await self.enforcement()
        self.assertFalse(session.quota_paused)
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 0)
        self.assertIn(LEGACY, self.usage.pending_meter_sources)
        await self.refresh(self.report(5, 8, active=True))
        self.assertEqual(self.shares.snapshot(self.owner)["fiveHour"]["used"], 0)
        await session.handle(self.frame(100, "first-idle-message"))
        await self.enforcement()
        own = self.usage.quota(LEGACY)
        self.assertEqual(own["fiveHour"]["limit"], 20)
        self.assertEqual(own["fiveHour"]["used"], 5)
        self.assertEqual(own["fiveHour"]["remaining"], 15)
        self.assertEqual(own["fiveHour"]["resetAt"], iso(self.five))
        self.assertEqual(own["weekly"]["used"], 2)
        self.assertEqual(own["fiveHour"]["sharedAdjustment"], 0)
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 0)
        await session.handle(self.frame(100, "first-idle-message"))
        await session.handle(self.result(100))
        await self.enforcement()
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 5)
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 100)
        self.assertFalse(session.quota_paused)

    async def test_assistant_and_result_before_activation_survive_repeated_idle_polls(self):
        session, _ = self.connected()
        initial_idle = self.official.snapshot()["fiveHour"]["idleSince"]
        await session.handle(self.frame(100, "before-activation"))
        await session.handle(self.result(150))
        for _ in range(3):
            await self.refresh()
            self.assertEqual(self.official.snapshot()["fiveHour"]["idleSince"], initial_idle)
            self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["limit"], 20)
        await self.refresh(self.report(9, 9, active=True))
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 9)
        self.assertEqual(self.usage.quota(LEGACY)["weekly"]["used"], 3)
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 0)
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 150)
        self.assertFalse(session.quota_paused)

    async def test_parallel_first_tasks_keep_token_weights_until_activation(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        await first.handle(self.frame(300, "first-parallel"))
        await second.handle(self.frame(100, "second-parallel"))
        await self.refresh()
        await self.refresh(self.report(16, 10, active=True))
        one, two = self.usage.quota(LEGACY), self.usage.quota(OTHER)
        self.assertEqual(one["fiveHour"]["used"], 12)
        self.assertEqual(two["fiveHour"]["used"], 4)
        self.assertEqual(one["weekly"]["used"], 3)
        self.assertEqual(two["weekly"]["used"], 1)
        self.assertFalse(first.quota_paused)
        self.assertFalse(second.quota_paused)

    async def test_fresh_result_without_assistant_bills_first_activation_once(self):
        session, _ = self.connected()
        self.now += timedelta(seconds=1)
        self.official.ingest(self.report(4, 7, active=True))
        await session.handle(self.result(100))
        await session.handle(self.result(100))
        await self.enforcement()
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 4)
        self.assertEqual(self.usage.quota(LEGACY)["weekly"]["used"], 1)
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 100)
        self.assertFalse(session.quota_paused)

    async def test_restart_and_replayed_first_message_keep_pending_weight_without_refund(self):
        session, _ = self.connected()
        message = self.frame(100, "restart-pending")
        await session.handle(message)
        await self.enforcement()
        idle_since = self.official.snapshot()["fiveHour"]["idleSince"]
        self.official.close()
        self.quotas.close()
        self.official = OfficialUsage(self.directory.name, clock=lambda: self.now.timestamp(), refresh_interval=0)
        self.quotas = QuotaStore(self.directory.name, clock=lambda: self.now)
        self.shares = OfficialShares(self.quotas)
        self.usage.official, self.usage.quotas, self.usage.shares = self.official, self.quotas, self.shares
        self.usage.tracker = UsageTracker(self.quotas)
        self.assertEqual(self.official.snapshot()["fiveHour"]["idleSince"], idle_since)
        await session.handle(message)
        await self.refresh(self.report(7, 8, active=True))
        await self.refresh(self.report(7, 8, active=True))
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["limit"], 20)
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 7)
        self.assertEqual(self.usage.quota(LEGACY)["weekly"]["used"], 2)
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 100)
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 0)

    async def test_weekly_exhaustion_blocks_even_with_fresh_idle_five_hour(self):
        session, _ = self.connected()
        await self.refresh(self.report(week=100))
        self.assertTrue(session.quota_paused)
        with self.assertRaises(BrokerError) as rejected:
            self.usage.admit(OTHER)
        self.assertEqual(rejected.exception.status, 429)
        self.assertEqual(self.usage.quota(OTHER)["weekly"]["remaining"], 0)

    async def test_weekly_staleness_blocks_despite_new_confirmed_idle_poll(self):
        self.now += timedelta(minutes=6)
        await self.refresh({"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 0, "resets_at": None}}})
        self.assertFalse(self.official.snapshot()["fiveHour"]["stale"])
        self.assertTrue(self.official.snapshot()["weekly"]["stale"])
        with self.assertRaises(BrokerError) as rejected:
            self.usage.admit(LEGACY)
        self.assertEqual(rejected.exception.status, 429)

    async def test_query_error_blocks_and_preserves_idle_allocations(self):
        self.official.invalidate(code="query_failed")
        with self.assertRaises(BrokerError) as rejected:
            self.usage.admit(LEGACY)
        self.assertEqual(rejected.exception.status, 429)
        self.assertEqual(self.shares.snapshot(self.owner)["fiveHour"]["limit"], 20)
        await self.refresh()
        self.usage.admit(LEGACY)
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["remaining"], 20)

    async def test_zero_partial_header_is_not_a_confirmed_idle_authorization(self):
        self.official.clear()
        self.now += timedelta(seconds=1)
        self.official.ingest({"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "utilization": 0, "status": "allowed"}})
        with self.assertRaises(BrokerError):
            self.usage.admit(LEGACY)
        self.assertNotEqual(self.official.snapshot()["fiveHour"].get("state"), "idle")

    async def test_explicit_rejection_at_zero_pauses_running_task_and_blocks_first_send(self):
        active, _ = self.connected()
        first_send, waiting_process = self.connected(OTHER, phase="idle")
        self.now += timedelta(seconds=1)
        await active.handle({"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "status": "rejected", "utilization": 0,
            "resetsAt": self.five.timestamp()}})
        await self.enforcement()
        official = self.official.snapshot()["fiveHour"]
        self.assertEqual(official["status"], "rejected")
        self.assertEqual(official["usedPercent"], 0)
        self.assertFalse(official["stale"])
        self.assertTrue(active.quota_paused)
        self.broker.stop.assert_awaited_once_with(LEGACY)
        self.assertFalse(first_send.quota_paused)
        with self.assertRaises(BrokerError) as rejected:
            await self.chats.send(OTHER, "尚未启动的第一个任务")
        self.assertEqual(rejected.exception.status, 429)
        self.assertEqual(waiting_process.stdin.messages, [])
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["remaining"], 0)
        self.assertEqual(self.shares.snapshot(self.other)["fiveHour"]["used"], 0)

    async def test_critical_severity_alone_does_not_mean_server_rejection(self):
        active, _ = self.connected()
        payload = self.report(0, 6, active=True)
        payload["rate_limits"]["five_hour"]["severity"] = "critical"
        await self.refresh(payload)
        self.assertEqual(self.official.snapshot()["fiveHour"]["status"], "critical")
        self.assertFalse(active.quota_paused)
        self.assertFalse(self.usage.quota(OTHER)["exhausted"])
        self.usage.admit(OTHER)

    async def test_rejection_needs_fresh_complete_profile_meter_before_recovery(self):
        session, _ = self.connected()
        self.now += timedelta(seconds=1)
        await session.handle({"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "status": "rejected", "utilization": 0,
            "resetsAt": self.five.timestamp()}})
        await self.enforcement()
        incomplete_reports = [
            {"rate_limits_available": True, "rate_limits": {"five_hour": {"resets_at": iso(self.five)}}},
            {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 0}}},
            {"rate_limits": {"five_hour": {"utilization": 0, "resets_at": iso(self.five)}}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "rateLimitType": "five_hour", "utilization": 0, "resetsAt": self.five.timestamp()}},
            {"rate_limits_available": True, "rate_limits": {"five_hour": {
                "utilization": 0, "resets_at": iso(self.five), "severity": "unrecognized"}}},
        ]
        for payload in incomplete_reports:
            self.now += timedelta(seconds=1)
            self.official.ingest(payload)
            self.assertEqual(self.official.snapshot()["fiveHour"]["status"], "rejected")
            with self.assertRaises(BrokerError):
                self.usage.admit(OTHER)
        await self.refresh(self.report(0, 6, active=True))
        # The new numeric report does not invent an explicit server allow; it
        # stops treating the prior rejection as the current server reading.
        self.assertEqual(self.official.snapshot()["fiveHour"]["status"], "unknown")
        self.assertFalse(self.usage.quota(OTHER)["exhausted"])
        self.usage.admit(OTHER)
