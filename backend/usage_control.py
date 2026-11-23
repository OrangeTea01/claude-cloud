"""Account-wide admission, official usage refresh, and idle workspace lifecycle."""
import asyncio
import logging
import sqlite3
import time
import uuid
from pathlib import Path

from .docker_backend import BrokerError
from .quotas import UsageTracker
from .official_usage import UsageUnavailable, fetch_usage_command

logger = logging.getLogger("claude-cloud")
IDLE_SECONDS = 30 * 60


class UsageControl:
    def __init__(self, broker, credentials, quotas, official, data_dir, *, clock=time.time, shares=None, solo=False):
        self.broker, self.credentials = broker, credentials
        self.quotas, self.official = quotas, official
        self.shares = shares
        self.solo = solo
        self.tracker = UsageTracker(quotas)
        self.clock = clock
        self.chats = None
        self.official_fetcher = None
        self.task = None
        self.enforcement_task = None
        self.pending_meter_sources = {}
        self.db = sqlite3.connect(Path(data_dir) / "activity.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS activity (workspace TEXT PRIMARY KEY, last_active REAL NOT NULL)")
        self.db.commit()

    def owner(self, workspace_id):
        return self.broker.workspace_access(workspace_id)["ownerId"]

    def quota(self, workspace_id=None, *, user_id=None):
        if self.solo:
            # The private installation has no site allocation. Official service
            # limits remain enforced by Claude itself; stale meters never block
            # a private task or turn a display failure into a zero allowance.
            return {"mode": "solo", "exhausted": False, "blockedReason": None}
        official = self.official.snapshot()
        self.quotas.sync_windows(official)
        user_id = user_id or self.owner(workspace_id)
        if self.shares and self.shares.config()["mode"] == "official_share":
            result = self.shares.snapshot(user_id)
            # Read-only summaries must never settle usage: a rate-limit frame can
            # precede the corresponding assistant usage. Still enforce account
            # exhaustion and freshness immediately, without waiting for billing.
            for bucket in ("fiveHour", "weekly"):
                item = official.get(bucket, {})
                percent = item.get("usedPercent")
                if item.get("stale", True) or not isinstance(percent, (int, float)):
                    result.update(exhausted=True, blockedReason="正在等待官方额度同步，请稍后刷新后继续")
                    result[bucket]["remaining"] = None
                elif item.get("status") == "rejected":
                    # A server rejection is authoritative even when the latest
                    # numeric meter is still zero at the start of a window.
                    result.update(exhausted=True, blockedReason="官方账号当前额度已受限，请等待恢复或刷新官方额度")
                    result[bucket]["remaining"] = 0.0
                elif percent >= 100:
                    result.update(exhausted=True, blockedReason="官方账号总额度已用完，请等待对应窗口恢复")
                    result[bucket]["remaining"] = 0.0
            return result
        return {**self.quotas.snapshot(user_id), "mode": "tokens", "unit": "tokens"}

    def tasks(self, user_id):
        tasks = []
        if self.chats:
            for key, session in self.chats.items.items():
                if not self.task_running(session):
                    continue
                try:
                    if self.owner(key) == user_id:
                        item = self.broker.get(key)
                        tasks.append({"workspaceId": key, "name": item.get("name", "工作区"), "phase": session.phase})
                except BrokerError:
                    continue
        return tasks

    @staticmethod
    def task_running(session):
        return bool(session.busy or (session.input_task and not session.input_task.done()))

    def admit(self, workspace_id):
        user_id = self.owner(workspace_id)
        user = self.credentials.user(user_id)
        if not user or user.get("deleting"):
            raise BrokerError("用户账号不可用", 403)
        if self.chats and any(self.owner(key) == user_id for key in self.chats.quota_stop_pending):
            raise BrokerError("正在确认旧任务已停止，请稍后再试或联系管理员检查 Docker", 503)
        quota = self.quota(user_id=user_id)
        if quota["exhausted"]:
            raise BrokerError(quota.get("blockedReason") or "站内额度已用完，请等待官方窗口恢复或联系管理员增加额度", 429)
        if not self.solo and any(item["workspaceId"] != workspace_id for item in self.tasks(user_id)):
            raise BrokerError("你的其他工作区已有 Claude 任务运行；每人同时只能运行 1 个任务", 409)

    def begin_session(self, session, fresh):
        if self.solo:
            return
        self.tracker.register_session(self.owner(session.workspace_id), session.usage_key, fresh=fresh)

    def observe(self, session, payload):
        changed = self.official.ingest(payload)
        if self.solo:
            return False
        self.quotas.sync_windows(self.official.snapshot())
        user_id = self.owner(session.workspace_id)
        # Session IDs are confirmed by the official init event before prompts.
        key = session.usage_key or session.session_id
        if key:
            if payload.get("type") == "assistant":
                self.tracker.assistant(user_id, payload, key)
            elif payload.get("type") == "result":
                self.tracker.result(user_id, payload, key)
        if self.shares and self.shares.config()["mode"] == "official_share":
            if changed and payload.get("type") == "rate_limit_event" and self.task_running(session):
                self.pending_meter_sources.setdefault(session.workspace_id, self.clock())
            if payload.get("type") in ("assistant", "result"):
                self.pending_meter_sources.pop(session.workspace_id, None)
                self.settle_shares(self.official.snapshot())
            if changed or payload.get("type") in ("assistant", "result"):
                self.queue_enforcement(session.workspace_id)
        return self.quota(user_id=user_id)["exhausted"]

    def settle_shares(self, official):
        # Let the source of an early rate frame supply its usage before a
        # different user's response can take the whole debit. Bound the wait;
        # account-wide metering is asynchronous and may never send that frame.
        for key, started in list(self.pending_meter_sources.items()):
            session = self.chats.items.get(key) if self.chats else None
            if not session or not self.task_running(session) or self.clock() - started >= 30:
                self.pending_meter_sources.pop(key, None)
        if not self.pending_meter_sources:
            self.shares.settle(official)

    def should_pause(self, session):
        # A final text response can precede its result/cumulative-usage frame.
        # Allow a short tail to complete without manufacturing a resume task.
        final_at = getattr(session, "final_reply_at", None)
        if final_at is not None and self.clock() - final_at < 5:
            return False
        return self.task_running(session) and self.quota(session.workspace_id)["exhausted"]

    async def enforce(self, *, exclude=None):
        if not self.chats:
            return
        async with self.chats.lock:
            for session in list(self.chats.items.values()):
                if session.workspace_id == exclude:
                    continue
                if self.should_pause(session):
                    await self.chats.pause_quota_locked(session)

    def queue_enforcement(self, origin):
        # Response text has already been saved. Include every active session so
        # coalesced updates cannot omit the originating user's shared adjustment.
        if self.enforcement_task and not self.enforcement_task.done():
            return
        async def run():
            try:
                await self.enforce()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Shared quota enforcement will retry")
        self.enforcement_task = asyncio.create_task(run())

    def touch(self, workspace_id):
        self.broker.get(workspace_id)
        self.db.execute("INSERT OR REPLACE INTO activity VALUES (?,?)", (workspace_id, self.clock()))
        self.db.commit()

    def forget(self, workspace_id):
        self.pending_meter_sources.pop(workspace_id, None)
        self.db.execute("DELETE FROM activity WHERE workspace=?", (workspace_id,))
        self.db.commit()

    async def fetch_official(self):
        if self.official_fetcher is not None:
            return await self.official_fetcher()
        # Reuse a live official process when possible. This control request does
        # not submit a prompt or ask a model to interpret subscription figures.
        # Try at most one alternate source after a failed client, all within the
        # OfficialUsage refresh deadline. Never fan out across all workspaces.
        attempts, failure = 0, None
        for session in list(self.chats.items.values()):
            if (session.proc and session.proc.returncode is None and not session.closing
                    and (not session.init_future or session.init_future.done())):
                attempts += 1
                try:
                    return await session.get_usage()
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    failure = UsageUnavailable(code="query_timeout")
                except Exception:
                    failure = UsageUnavailable(code="query_failed")
                if attempts >= 2:
                    raise failure
        for workspace in await self.broker.list():
            if workspace.get("running"):
                attempts += 1
                try:
                    command = await self.broker.chat_command(workspace["id"], None, str(uuid.uuid4()), "default")
                    # An orphaned docker-exec client cannot leave a 6-hour reader.
                    command = list(command)
                    if "/usr/bin/timeout" in command:
                        index = command.index("/usr/bin/timeout")
                        if command[index + 1:index + 4] == ["--signal=TERM", "--kill-after=5s", "6h"]:
                            command[index + 3] = "25s"
                    return await fetch_usage_command(command)
                except asyncio.CancelledError:
                    raise
                except UsageUnavailable as exc:
                    failure = exc
                except asyncio.TimeoutError:
                    failure = UsageUnavailable(code="query_timeout")
                except Exception:
                    failure = UsageUnavailable(code="query_failed")
                if attempts >= 2:
                    raise failure
        raise failure or UsageUnavailable(code="no_running_workspace")

    async def refresh(self):
        result = await self.official.refresh(self.fetch_official)
        self.quotas.sync_windows(result)
        if not self.solo and self.shares and self.shares.config()["mode"] == "official_share":
            self.settle_shares(result)
            await self.enforce()
        return result

    async def scan_idle(self):
        async with self.chats.lock:
            for key in list(self.chats.quota_stop_pending):
                session = self.chats.items.get(key)
                if session:
                    await self.chats.pause_quota_locked(session)
            for item in await self.broker.list():
                key = item["id"]
                if not item.get("running"):
                    continue
                session = self.chats.items.get(key)
                if ((session and self.task_running(session)) or self.chats.terminals.active(key)):
                    # Give a completed long task the full idle grace period.
                    self.touch(key)
                    continue
                row = self.db.execute("SELECT last_active FROM activity WHERE workspace=?", (key,)).fetchone()
                if row is None:
                    self.touch(key)
                elif self.clock() - row[0] >= IDLE_SECONDS:
                    try:
                        await self.broker.stop(key)
                        await self.chats.close_locked(key)
                        self.forget(key)
                    except BrokerError:
                        logger.warning("An idle workspace could not stop; will retry")

    async def loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.scan_idle()
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never include remote output or credential-bearing exceptions.
                logger.warning("Usage/idle maintenance will retry")

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.enforcement_task:
            self.enforcement_task.cancel()
            await asyncio.gather(self.enforcement_task, return_exceptions=True)
        self.db.close()
