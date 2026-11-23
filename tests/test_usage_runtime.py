"""Quota pause/resume, cross-workspace admission, and genuine idle activity."""
import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from backend.chat import Chats
from backend.credentials import CredentialStore
from backend.docker_backend import BrokerError
from backend.quotas import QuotaStore, iso
from backend.terminal import Terminals
from backend.usage_control import UsageControl, IDLE_SECONDS
from test_chat import FakeProcess, permission_frame
from test_multiuser_api import MemoryBroker, USER_HASH, LEGACY


SECOND, OTHER = "b" * 32, "c" * 32


class UsageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        self.credentials = CredentialStore(self.directory.name, USER_HASH)
        self.owner = self.credentials.create_user("first-user", USER_HASH)["id"]
        self.other = self.credentials.create_user("other-user", USER_HASH)["id"]
        self.quotas = QuotaStore(self.directory.name, clock=lambda: self.now)
        self.broker = MemoryBroker()
        self.broker.assign_legacy_owner(self.owner)
        self.broker.records[SECOND] = {**self.broker.raw(LEGACY), "id": SECOND, "name": "second"}
        self.broker.records[OTHER] = {**self.broker.raw(LEGACY), "id": OTHER, "ownerId": self.other, "name": "other"}
        self.official_state = {"stale": True, "updatedAt": None,
                               "fiveHour": {"resetAt": None}, "weekly": {"resetAt": None}}
        self.official = SimpleNamespace(snapshot=lambda: self.official_state, ingest=Mock())
        self.usage = UsageControl(self.broker, self.credentials, self.quotas, self.official,
                                  self.directory.name, clock=lambda: self.now.timestamp())
        self.terminals = Terminals(self.broker)
        self.chats = Chats(self.broker, self.terminals, None, Path(self.directory.name), usage=self.usage)
        self.usage.chats = self.chats
        self.processes = []
        original_stop, original_start = self.broker.stop, self.broker.start
        async def stop(key):
            session = self.chats.items.get(key)
            if session and session.proc:
                session.proc.finish()
            return await original_stop(key)
        self.broker.stop = AsyncMock(side_effect=stop)
        self.broker.start = AsyncMock(side_effect=original_start)

    async def asyncTearDown(self):
        for process in self.processes:
            process.finish()
        await self.chats.shutdown()
        await self.usage.close()
        self.quotas.close()
        self.credentials.close()
        self.directory.cleanup()

    def connected(self, key=LEGACY, phase="thinking"):
        session = self.chats.get(key)
        session.session_id = str(uuid.UUID(key))
        session.usage_key = session.session_id
        self.usage.begin_session(session, fresh=True)
        process = FakeProcess()
        self.processes.append(process)
        session.proc, session.phase = process, phase
        self.broker.raw(key)["running"] = True
        return session, process

    def frame(self, amount=10, message="msg-1"):
        return {"type": "assistant", "message": {"id": message, "model": "sonnet",
                "usage": {"input_tokens": amount, "output_tokens": 0},
                "content": [{"type": "text", "text": "已完成部分内容"}]}}

    async def launch_fake(self, session):
        process = FakeProcess()
        self.processes.append(process)
        session.proc = process
        return process

    async def paused(self):
        session, process = self.connected()
        message = session.message("user", "请修改这个项目")
        message["sent"] = True
        session.changed()
        self.quotas.set_limits(self.owner, 10, 100)
        await session.handle(self.frame())
        return session, process

    async def test_quota_reaching_limit_stops_container_and_preserves_checkpoint_history(self):
        session, process = await self.paused()
        self.broker.stop.assert_awaited_once_with(LEGACY)
        self.assertIsNotNone(process.returncode)
        self.assertFalse(self.broker.raw(LEGACY)["running"])
        self.assertEqual(session.phase, "paused_quota")
        self.assertTrue(session.quota_paused)
        self.assertFalse(session.active)
        checkpoint, history = self.chats.store.load(LEGACY)
        self.assertEqual(checkpoint, str(uuid.UUID(LEGACY)))
        self.assertEqual(history[0]["text"], "请修改这个项目")
        self.assertEqual(history[-1]["text"], "已完成部分内容")
        self.assertTrue(self.chats.store.load_paused(LEGACY))
        self.assertFalse(session.snapshot()["canResume"])

    async def test_paused_state_and_history_survive_chat_manager_restart(self):
        session, _ = await self.paused()
        before = list(session.messages)
        await self.chats.shutdown()
        self.chats = Chats(self.broker, self.terminals, None, Path(self.directory.name), usage=self.usage)
        self.usage.chats = self.chats
        restored = self.chats.get(LEGACY)
        self.assertTrue(restored.quota_paused)
        self.assertEqual(restored.phase, "paused_quota")
        self.assertEqual(restored.messages, before)
        self.assertFalse(restored.snapshot()["canResume"])

    async def test_resume_after_added_allowance_starts_workspace_without_replaying_original_action(self):
        session, _ = await self.paused()
        self.quotas.set_limits(self.owner, 100, 1000)
        self.assertTrue(session.snapshot()["canResume"])
        with patch.object(session, "launch", side_effect=partial(self.launch_fake, session)):
            await self.chats.resume_quota(LEGACY)
            self.assertTrue(session.resume_pending)
            self.assertTrue(self.chats.store.load_paused(LEGACY))
            await session.input_task
            self.broker.start.assert_awaited_once_with(LEGACY)
            sent = session.proc.stdin.messages
            self.assertEqual(len(sent), 1)
            self.assertIn("先检查哪些操作已经完成", sent[0]["message"]["content"])
            self.assertNotEqual(sent[0]["message"]["content"], session.messages[0]["text"])
        self.assertEqual(session.session_id, str(uuid.UUID(LEGACY)))
        self.assertFalse(session.quota_paused)
        self.assertFalse(session.resume_pending)
        self.assertFalse(self.chats.store.load_paused(LEGACY))

    async def test_failed_resume_launch_keeps_durable_continue_entry_and_can_retry(self):
        session, _ = await self.paused()
        self.quotas.set_limits(self.owner, 100, 1000)
        with patch.object(session, "launch", side_effect=BrokerError("launch unavailable", 503)):
            await self.chats.resume_quota(LEGACY)
            self.assertTrue(self.chats.store.load_paused(LEGACY))
            await session.input_task
        self.assertTrue(session.quota_paused)
        self.assertFalse(session.resume_pending)
        self.assertEqual(session.phase, "paused_quota")
        self.assertTrue(session.snapshot()["canResume"])
        self.assertTrue(self.chats.store.load_paused(LEGACY))
        saved = [item for item in session.messages if item["role"] == "user"]
        self.assertFalse(saved[-1]["sent"])
        with patch.object(session, "launch", side_effect=partial(self.launch_fake, session)):
            await self.chats.resume_quota(LEGACY)
            await session.input_task
        self.assertFalse(self.chats.store.load_paused(LEGACY))
        self.assertEqual(len([item for item in session.messages if item["role"] == "user"]), len(saved))
        self.assertEqual(len(session.proc.stdin.messages), 1)

    async def test_pause_before_input_task_runs_preserves_and_resumes_original_unsent_prompt(self):
        session, process = self.connected(phase="idle")
        await self.chats.send(LEGACY, "尚未发送的具体任务")
        queued = session.input_task
        self.assertFalse(session.messages[-1]["sent"])
        self.assertEqual(self.chats.store.load(LEGACY)[1][-1]["text"], "尚未发送的具体任务")
        # Admin revokes allowance before the newly scheduled input coroutine runs.
        self.quotas.set_limits(self.owner, 0, None)
        await self.chats.pause_quota(session)
        self.assertTrue(queued.cancelled())
        self.assertEqual(process.stdin.messages, [])
        self.assertTrue(self.chats.store.load_paused(LEGACY))
        self.quotas.set_limits(self.owner, 100, None)
        with patch.object(session, "launch", side_effect=partial(self.launch_fake, session)):
            await self.chats.resume_quota(LEGACY)
            await session.input_task
        self.assertEqual(session.proc.stdin.messages[0]["message"]["content"], "尚未发送的具体任务")
        self.assertEqual(len(session.messages), 1)
        self.assertTrue(session.messages[0]["sent"])

    async def test_final_result_reconciliation_exhausts_allowance_without_pausing_completed_task(self):
        session, _ = self.connected()
        self.quotas.set_limits(self.owner, 15, 100)
        await session.handle(self.frame(10))
        await session.handle({"type": "result", "is_error": False, "session_id": session.session_id,
                              "modelUsage": {"sonnet": {"inputTokens": 20, "outputTokens": 0}}})
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 20)
        self.assertEqual(session.phase, "idle")
        self.assertFalse(session.quota_paused)
        self.assertFalse(self.chats.store.load_paused(LEGACY))
        self.assertFalse(session.snapshot()["canResume"])
        self.broker.stop.assert_not_awaited()
        with self.assertRaises(BrokerError) as raised:
            await self.chats.send(LEGACY, "new task")
        self.assertEqual(raised.exception.status, 429)

    async def test_end_turn_text_waits_for_result_even_when_allowance_is_exhausted(self):
        session, _ = self.connected()
        self.quotas.set_limits(self.owner, 10, 100)
        frame = self.frame()
        frame["message"]["stop_reason"] = "end_turn"
        await session.handle(frame)
        self.assertFalse(session.quota_paused)
        self.broker.stop.assert_not_awaited()
        await session.handle({"type": "result", "is_error": False, "result": "已完成部分内容",
                              "modelUsage": {"sonnet": {"inputTokens": 10, "outputTokens": 0}}})
        self.assertEqual(session.phase, "idle")
        self.assertFalse(self.chats.store.load_paused(LEGACY))
        self.assertEqual(len(session.messages), 1)
        self.broker.stop.assert_not_awaited()

    async def test_tool_use_at_quota_boundary_still_pauses_incomplete_task(self):
        session, _ = self.connected()
        self.quotas.set_limits(self.owner, 10, 100)
        frame = self.frame()
        frame["message"]["stop_reason"] = "end_turn"
        frame["message"]["content"].append({"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}})
        await session.handle(frame)
        self.assertTrue(session.quota_paused)
        self.assertEqual(session.phase, "paused_quota")
        self.broker.stop.assert_awaited_once_with(LEGACY)

    async def test_official_deadline_restores_allowance_and_resume_starts_stopped_workspace(self):
        reset = self.now + timedelta(hours=5)
        self.official_state = {"stale": False, "updatedAt": iso(self.now),
                               "fiveHour": {"resetAt": iso(reset)},
                               "weekly": {"resetAt": iso(self.now + timedelta(days=7))}}
        session, _ = await self.paused()
        self.now = reset
        self.assertTrue(session.snapshot()["canResume"])
        self.assertEqual(self.quotas.snapshot(self.owner)["fiveHour"]["used"], 0)
        self.assertEqual(self.quotas.snapshot(self.owner)["weekly"]["used"], 10)
        with patch.object(session, "send", new_callable=AsyncMock):
            await self.chats.resume_quota(LEGACY)
            await session.input_task
        self.assertTrue(self.broker.raw(LEGACY)["running"])

    async def test_simultaneous_sends_share_one_account_task_slot_across_workspaces(self):
        first, _ = self.connected(LEGACY, "idle")
        second, _ = self.connected(SECOND, "idle")
        results = await asyncio.gather(self.chats.send(LEGACY, "first"), self.chats.send(SECOND, "second"),
                                       return_exceptions=True)
        rejected = [result for result in results if isinstance(result, BrokerError)]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].status, 409)
        for session in (first, second):
            if session.input_task:
                await session.input_task
        self.assertEqual(len(self.usage.tasks(self.owner)), 1)
        other, _ = self.connected(OTHER, "idle")
        await self.chats.send(OTHER, "independent account")
        await other.input_task
        self.assertEqual(len(self.usage.tasks(self.other)), 1)

    async def test_exhausted_quota_cannot_approve_pending_permission(self):
        session, process = self.connected()
        await session.handle(permission_frame())
        self.quotas.set_limits(self.owner, 0, None)
        with self.assertRaises(BrokerError) as raised:
            await self.chats.permission(LEGACY, "permission-1", True)
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(process.stdin.messages, [])
        self.assertTrue(session.quota_paused)
        self.assertEqual(session.pending, {})

    async def test_failed_stop_blocks_resume_until_maintenance_retries(self):
        session, _ = self.connected()
        self.quotas.set_limits(self.owner, 10, 100)
        stop = self.broker.stop.side_effect
        self.broker.stop.side_effect = BrokerError("Docker offline", 503)
        # Let close reap the local fake process while Docker stop remains failed.
        session.proc.finish()
        await session.handle(self.frame())
        self.assertIn(LEGACY, self.chats.quota_stop_pending)
        self.quotas.set_limits(self.owner, 100, 1000)
        self.assertFalse(session.snapshot()["canResume"])
        with self.assertRaises(BrokerError):
            await self.chats.resume_quota(LEGACY)
        second, second_process = self.connected(SECOND, "idle")
        with self.assertRaises(BrokerError) as raised:
            await self.chats.send(SECOND, "must wait for old container to stop")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(second_process.stdin.messages, [])
        self.assertIsNone(second.input_task)
        other, _ = self.connected(OTHER, "idle")
        await self.chats.send(OTHER, "another account remains independent")
        await other.input_task
        self.broker.stop.side_effect = stop
        await self.usage.scan_idle()
        self.assertNotIn(LEGACY, self.chats.quota_stop_pending)
        self.assertTrue(session.snapshot()["canResume"])

    async def test_one_idle_stop_failure_does_not_prevent_other_workspace_reclamation(self):
        self.connected(LEGACY, "idle")
        self.connected(SECOND, "idle")
        self.usage.touch(LEGACY)
        self.usage.touch(SECOND)
        self.now += timedelta(seconds=IDLE_SECONDS)
        original_stop = self.broker.stop.side_effect
        async def stop(key):
            if key == LEGACY:
                raise BrokerError("one container temporarily unavailable", 503)
            return await original_stop(key)
        self.broker.stop.side_effect = stop
        with self.assertLogs("claude-cloud", level="WARNING"):
            await self.usage.scan_idle()
        self.assertEqual([call.args[0] for call in self.broker.stop.await_args_list], [LEGACY, SECOND])
        self.assertTrue(self.broker.raw(LEGACY)["running"])
        self.assertFalse(self.broker.raw(SECOND)["running"])
        self.assertIsNotNone(self.usage.db.execute("SELECT 1 FROM activity WHERE workspace=?", (LEGACY,)).fetchone())
        self.assertIsNone(self.usage.db.execute("SELECT 1 FROM activity WHERE workspace=?", (SECOND,)).fetchone())

    async def test_idle_shutdown_uses_last_real_activity_and_preserves_files_history(self):
        session, _ = self.connected(phase="idle")
        session.message("user", "saved history")
        session.changed()
        self.usage.touch(LEGACY)
        self.now += timedelta(seconds=IDLE_SECONDS - 1)
        await self.usage.scan_idle()
        self.broker.stop.assert_not_awaited()
        self.usage.touch(LEGACY)  # A real user action extends the grace period.
        self.now += timedelta(seconds=IDLE_SECONDS - 1)
        await self.usage.scan_idle()
        self.broker.stop.assert_not_awaited()
        self.now += timedelta(seconds=1)
        await self.usage.scan_idle()
        self.broker.stop.assert_awaited_once_with(LEGACY)
        self.assertIn(LEGACY, self.broker.records)
        self.assertEqual(self.chats.store.load(LEGACY)[1][0]["text"], "saved history")

    async def test_busy_or_permission_waiting_task_is_not_idle_closed(self):
        session, _ = self.connected()
        self.usage.touch(LEGACY)
        for phase in ("thinking", "awaiting_permission", "switching_model"):
            session.phase = phase
            self.now += timedelta(hours=2)
            await self.usage.scan_idle()
            self.broker.stop.assert_not_awaited()
        session.phase = "idle"
        self.now += timedelta(seconds=IDLE_SECONDS - 1)
        await self.usage.scan_idle()
        self.broker.stop.assert_not_awaited()
        self.now += timedelta(seconds=1)
        await self.usage.scan_idle()
        self.broker.stop.assert_awaited_once_with(LEGACY)

    async def test_forgetting_workspace_never_resets_account_quota(self):
        self.chats.get(LEGACY)
        self.quotas.charge(self.owner, "used", 25)
        self.usage.touch(LEGACY)
        await self.chats.forget(LEGACY)
        self.assertEqual(self.quotas.snapshot(self.owner)["weekly"]["used"], 25)
        self.assertIsNone(self.usage.db.execute("SELECT 1 FROM activity WHERE workspace=?", (LEGACY,)).fetchone())
