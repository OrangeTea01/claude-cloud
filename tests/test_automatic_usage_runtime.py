"""Shared official meters gate parallel users without replacing the token ledger."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
import unittest

from backend.docker_backend import BrokerError
from backend.official_shares import OfficialShares
from backend.official_usage import OfficialUsage
from backend.quotas import iso
from test_multiuser_api import LEGACY
import test_usage_runtime as runtime_fixtures
from test_usage_runtime import OTHER


class AutomaticUsageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    connected = runtime_fixtures.UsageRuntimeTests.connected
    frame = runtime_fixtures.UsageRuntimeTests.frame

    async def asyncSetUp(self):
        await runtime_fixtures.UsageRuntimeTests.asyncSetUp(self)
        self.official = OfficialUsage(self.directory.name, clock=lambda: self.now.timestamp())
        self.shares = OfficialShares(self.quotas)
        self.usage.official, self.usage.shares = self.official, self.shares
        self.five, self.week = self.now + timedelta(hours=5), self.now + timedelta(days=7)
        self.report()
        self.shares.configure([self.owner, self.other], official_snapshot=self.official.snapshot())

    async def asyncTearDown(self):
        await self.usage.close()
        self.official.close()
        await runtime_fixtures.UsageRuntimeTests.asyncTearDown(self)

    def report(self, five=0, week=0, *, ingest=True):
        payload = {"rate_limits": {
            "five_hour": {"utilization": five, "resets_at": iso(self.five)},
            "seven_day": {"utilization": week, "resets_at": iso(self.week)}}}
        if ingest:
            self.official.ingest(payload)
        return payload

    async def settle_enforcement(self):
        if self.usage.enforcement_task:
            await self.usage.enforcement_task

    async def test_rate_event_waits_for_assistant_tokens_before_attribution(self):
        session, _ = self.connected()
        self.now += timedelta(seconds=1)
        self.usage.observe(session, self.report(20, 10, ingest=False))
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 0)
        self.assertEqual(self.shares.snapshot(self.owner)["fiveHour"]["sharedAdjustment"], 0)
        await session.handle(self.frame(100))
        await self.settle_enforcement()
        own, other = self.usage.quota(LEGACY), self.usage.quota(OTHER)
        self.assertEqual(own["fiveHour"]["used"], 20)
        self.assertEqual(own["weekly"]["used"], 10)
        self.assertEqual(other["fiveHour"]["used"], 0)
        self.assertEqual(own["fiveHour"]["remaining"], 30)
        # Token data remains available for migration back to manual mode.
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 100)
        await session.handle(self.frame(100))
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 20)

    async def test_one_user_exhausted_pauses_only_its_task_and_preserves_history(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        self.now += timedelta(seconds=1)
        self.report(50, 10)
        await first.handle(self.frame(100))
        await self.settle_enforcement()
        self.assertTrue(first.quota_paused)
        self.assertFalse(second.quota_paused)
        self.assertEqual(first.messages[-1]["text"], "已完成部分内容")
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["remaining"], 50)

    async def test_official_global_exhaustion_pauses_both_parallel_tasks_before_token_return(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        self.now += timedelta(seconds=1)
        await first.handle(self.report(100, 20, ingest=False))
        await self.settle_enforcement()
        self.assertTrue(first.quota_paused)
        self.assertTrue(second.quota_paused)
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["remaining"], 0)

    async def test_stale_official_data_blocks_start_and_refresh_pauses_existing_tasks(self):
        first, _ = self.connected()
        self.now += timedelta(minutes=6)
        quota = self.usage.quota(LEGACY)
        self.assertTrue(quota["exhausted"])
        self.assertIsNone(quota["fiveHour"]["remaining"])
        self.assertIn("同步", quota["blockedReason"])
        with self.assertRaises(BrokerError) as rejected:
            self.usage.admit(OTHER)
        self.assertEqual(rejected.exception.status, 429)
        self.official.refresh = AsyncMock(return_value=self.official.snapshot())
        await self.usage.refresh()
        self.assertTrue(first.quota_paused)

    async def test_refresh_attributes_pending_parallel_usage_and_enforces_all_users(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        await first.handle(self.frame(300, "a"))
        await second.handle(self.frame(100, "b"))
        await self.settle_enforcement()
        self.now += timedelta(seconds=1)
        self.report(80, 40)
        self.official.refresh = AsyncMock(return_value=self.official.snapshot())
        await self.usage.refresh()
        self.assertTrue(first.quota_paused)
        self.assertFalse(second.quota_paused)
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 60)
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 20)

    async def test_expired_share_requires_new_official_cycle_not_just_local_clock(self):
        self.now = self.five
        self.assertTrue(self.usage.quota(LEGACY)["exhausted"])
        self.assertIsNone(self.usage.quota(LEGACY)["fiveHour"]["remaining"])
        self.five = self.now + timedelta(hours=5)
        self.report(0, 0)
        self.official.refresh = AsyncMock(return_value=self.official.snapshot())
        await self.usage.refresh()
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["remaining"], 50)
        self.assertFalse(self.usage.quota(LEGACY)["exhausted"])

    async def test_disabling_automatic_uses_original_manual_limits_and_used_values(self):
        self.quotas.set_limits(self.owner, 100, 1000)
        self.quotas.charge(self.owner, "old", 60)
        self.shares.configure(enabled=False, official_snapshot=self.official.snapshot())
        state = self.usage.quota(LEGACY)
        self.assertEqual(state["unit"], "tokens")
        self.assertEqual(state["fiveHour"]["remaining"], 40)

    async def test_completed_final_result_at_limit_does_not_create_false_resume(self):
        session, _ = self.connected()
        self.now += timedelta(seconds=1)
        self.report(50, 10)
        await session.handle({"type": "result", "result": "完成", "is_error": False,
                              "modelUsage": {"sonnet": {"inputTokens": 100}}})
        await self.settle_enforcement()
        self.assertFalse(session.quota_paused)
        self.assertEqual(session.phase, "idle")
        self.assertTrue(self.usage.quota(LEGACY)["exhausted"])

    async def test_other_user_response_waits_for_origin_of_early_meter_frame(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        self.now += timedelta(seconds=1)
        await first.handle({"type": "rate_limit_event", **self.report(20, 10, ingest=False)})
        await second.handle(self.frame(300, "second"))
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 0)
        await first.handle(self.frame(100, "first"))
        await self.settle_enforcement()
        self.assertEqual(self.usage.quota(LEGACY)["fiveHour"]["used"], 5)
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 15)

    async def test_meter_source_wait_is_bounded_and_does_not_block_global_cap(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        self.now += timedelta(seconds=1)
        await first.handle({"type": "rate_limit_event", **self.report(20, 10, ingest=False)})
        await second.handle(self.frame(100, "second"))
        self.now += timedelta(seconds=31)
        self.official.refresh = AsyncMock(return_value=self.official.snapshot())
        await self.usage.refresh()
        self.assertEqual(self.usage.pending_meter_sources, {})
        self.assertEqual(self.usage.quota(OTHER)["fiveHour"]["used"], 20)

    async def test_cross_user_enforcement_leaves_short_final_reply_tail_to_finish(self):
        first, _ = self.connected()
        second, _ = self.connected(OTHER)
        self.now += timedelta(seconds=1)
        self.report(50, 10)
        frame = self.frame(100, "first")
        frame["message"]["stop_reason"] = "end_turn"
        await first.handle(frame)
        await second.handle(self.frame(1, "second"))
        await self.settle_enforcement()
        self.assertFalse(first.quota_paused)
        await first.handle({"type": "result", "result": "完成", "is_error": False,
                            "modelUsage": {"sonnet": {"inputTokens": 100}}})
        await self.settle_enforcement()
        self.assertFalse(first.quota_paused)
        self.assertEqual(first.phase, "idle")

    async def test_stuck_final_reply_tail_is_not_unbounded_exemption(self):
        first, _ = self.connected()
        self.now += timedelta(seconds=1)
        self.report(50, 10)
        frame = self.frame(100)
        frame["message"]["stop_reason"] = "end_turn"
        await first.handle(frame)
        await self.settle_enforcement()
        self.assertFalse(first.quota_paused)
        self.now += timedelta(seconds=6)
        await self.usage.enforce()
        self.assertTrue(first.quota_paused)
