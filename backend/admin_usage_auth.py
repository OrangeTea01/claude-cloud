"""Administrator-confirmed profile authorization, separate from inference."""
import asyncio
from pathlib import Path
import sqlite3
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from .admin_claude import AuthTerminal
from .admin_usage_broker import AdminUsageBroker
from .docker_backend import BrokerError
from .login_browser import LoginBrowsers
from .official_usage import UsageUnavailable, fetch_usage_command
from .terminal import Terminals


class UsageAuthTerminal(AuthTerminal):
    def filter_output(self, output):
        visible = super().filter_output(output)
        self.private_output = ""
        return visible


class AdminUsageAuth:
    def __init__(self, runtime, admin_sessions):
        self.runtime, self.admin_sessions = runtime, admin_sessions
        self.broker = AdminUsageBroker(runtime["broker"])
        self.workspace_id = self.broker.workspace_id
        self.terminals = Terminals(self.broker, terminal_factory=UsageAuthTerminal)
        self.terminals.lock = runtime["terminals"].lock
        self.browsers = LoginBrowsers(self.broker)
        self.connect_lock, self.operation_lock = asyncio.Lock(), asyncio.Lock()
        self.owner = self.browser_owner = self.connection = None
        self.capture = self.capture_term = None
        self.importing = False
        self.error = None
        self.prepared_generation = self.pending_generation = None
        self.pending_owner = None
        self.account_email = None
        path = Path(runtime["broker"].config.data_dir) / "usage_auth.sqlite3"
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER, email TEXT)")
        self.db.commit()
        row = self.db.execute("SELECT generation,email FROM binding WHERE id=1").fetchone()
        self.binding_generation, self.bound_email = row if row else (None, None)
        self.binding_revision = 0
        self.closed = False

    def generation(self):
        return self.runtime["shares"].config()["accountGeneration"]

    def require_admin(self, owner):
        if not self.admin_sessions.get(owner):
            raise BrokerError("管理员登录已过期，请重新登录", 401)

    def _require_generation(self, generation):
        if generation != self.generation():
            raise BrokerError("共享 Claude 授权已变更，请重新完成额度查询授权", 409)

    def _persist_binding(self, generation, email=None):
        try:
            self.db.execute("INSERT OR REPLACE INTO binding VALUES (1,?,?)", (generation, email))
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise BrokerError("额度授权关联无法保存，请检查服务器存储", 503) from None
        self.binding_generation, self.bound_email = generation, email
        self.binding_revision += 1

    async def status(self):
        generation = self.generation()
        bound = self.binding_generation is not None and self.binding_generation == generation
        pending = self.pending_generation is not None and self.pending_generation == generation
        finalizing = bool(self.capture and not self.capture.done() and self.capture_term and self.capture_term.finished)
        return {"authenticated": bound, "terminalActive": self.terminals.active(self.workspace_id),
                "importing": self.importing or finalizing, "error": self.error,
                "pendingConfirmation": pending, "accountEmail": self.account_email if pending else self.bound_email if bound else None}

    async def prepare(self, owner):
        async with self.terminals.lock:
            self.require_admin(owner)
            async with self.operation_lock:
                generation = self.generation()
                status = await self.runtime["broker"].shared_auth_status()
                self.require_admin(owner)
                self._require_generation(generation)
                if not status.get("authenticated"):
                    raise BrokerError("请先完成共享 Claude 授权，再登录同一账号读取额度", 409)
                # Commit invalidation before login can overwrite private OAuth.
                self._persist_binding(None)
                self.runtime["official"].invalidate(code="usage_authorization_required")
                await self._stop_operation_locked()
                self.require_admin(owner)
                self._require_generation(generation)
                self.owner, self.prepared_generation = owner, generation
                self.error = None
        return await self.status()

    async def attach(self, owner):
        async with self.connect_lock:
            async with self.terminals.lock:
                self.require_admin(owner)
                async with self.operation_lock:
                    self.require_admin(owner)
                    if self.owner != owner or self.prepared_generation is None:
                        raise BrokerError("请先确认使用共享 Claude 账号，再开始额度授权", 409)
                    generation = self.prepared_generation
                    self._require_generation(generation)
                    term = self.terminals.items.get(self.workspace_id)
                    if term and not term.finished:
                        return term, term.attach()
                    if self.capture and not self.capture.done():
                        raise BrokerError("正在确认上一轮登录，请等待状态更新", 409)
                    term = None
                    try:
                        command = await self.broker.terminal_command(self.workspace_id)
                        self.require_admin(owner)
                        self._require_generation(generation)
                        term = UsageAuthTerminal(command)
                        await term.start()
                        self.require_admin(owner)
                        self._require_generation(generation)
                    except BaseException:
                        if term:
                            await term.close()
                        await self.broker.stop(self.workspace_id)
                        raise
                    self.terminals.items[self.workspace_id] = term
                    self.capture_term = term
                    self.capture = asyncio.create_task(self._capture_login(term, owner, generation))
                    return term, term.attach()

    async def _capture_login(self, term, owner, generation):
        try:
            deadline = asyncio.get_running_loop().time() + 1800
            while not term.watch.done():
                self.require_admin(owner)
                self._require_generation(generation)
                if asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError()
                await asyncio.wait({term.watch}, timeout=1)
            await term.watch
            self.require_admin(owner)
            self._require_generation(generation)
            if not term.proc or term.proc.returncode != 0:
                raise BrokerError("额度查询登录未完成，请重新开始授权", 409)
            self.importing = True
            async with self.operation_lock:
                state = await self.broker.auth_status()
                self.require_admin(owner)
                self._require_generation(generation)
                if not state["authenticated"] or not state["profileScope"]:
                    raise BrokerError("登录未取得额度查询权限，请使用共享 Claude 订阅账号重新登录", 409)
                self.pending_generation, self.pending_owner = generation, owner
                self.account_email = state.get("accountEmail")
                self.error = None
                term.replay.clear()
                term.replay_size = 0
        except asyncio.CancelledError:
            raise
        except (BrokerError, asyncio.TimeoutError):
            self.error = "额度授权已失效、超时或共享账号已变更，请重新开始授权"
            async with self.terminals.lock:
                await self.stop_locked()
        except Exception:
            self.error = "未能确认额度查询登录，请检查服务后重新授权"
            async with self.terminals.lock:
                await self.stop_locked()
        finally:
            term.private_output = ""
            self.prepared_generation = None
            self.importing = False

    async def confirm(self, owner):
        async with self.terminals.lock:
            self.require_admin(owner)
            async with self.operation_lock:
                generation = self.pending_generation
                if generation is None or self.pending_owner != owner:
                    raise BrokerError("请先在当前管理员会话中完成额度查询登录", 409)
                self._require_generation(generation)
                state = await self.broker.auth_status()
                self.require_admin(owner)
                self._require_generation(generation)
                if not state["authenticated"] or not state["profileScope"]:
                    raise BrokerError("额度查询登录已失效，请重新登录", 409)
                self._persist_binding(generation, state.get("accountEmail"))
                self.runtime["official"].invalidate(code="usage_authorization_required")
                self.pending_generation = self.pending_owner = None
                self.account_email = None
                self.error = None
                await self._stop_operation_locked()
        return await self.status()

    async def fetch_usage(self):
        if self.closed:
            raise UsageUnavailable(code="usage_authorization_required")
        generation = self.generation()
        if ((self.prepared_generation is not None and self.prepared_generation != generation)
                or (self.pending_generation is not None and self.pending_generation != generation)):
            raise UsageUnavailable(code="usage_authorization_required")
        # A long browser login never holds this lock, but its state blocks reads.
        if self.operation_lock.locked() or self.prepared_generation is not None or self.pending_generation is not None:
            raise UsageUnavailable(code="usage_authorization_busy")
        async with self.operation_lock:
            generation, revision = self.generation(), self.binding_revision
            if self.binding_generation is None or self.binding_generation != generation:
                raise UsageUnavailable(code="usage_authorization_required")
            try:
                await self.broker.start(self.workspace_id)
                if generation != self.generation() or revision != self.binding_revision:
                    raise UsageUnavailable(code="usage_authorization_required")
                state = await self.broker.auth_status()
                if not state["authenticated"] or not state["profileScope"]:
                    raise UsageUnavailable(code="usage_authorization_required")
                if generation != self.generation() or revision != self.binding_revision:
                    raise UsageUnavailable(code="usage_authorization_required")
                report = await fetch_usage_command(self.broker.usage_command())
                if generation != self.generation() or revision != self.binding_revision:
                    raise UsageUnavailable(code="usage_authorization_required")
                return report
            finally:
                # Retain credentials, but never leave a dedicated reader alive.
                await self.broker.stop(self.workspace_id)

    async def _stop_operation_locked(self):
        self.owner = self.browser_owner = None
        self.prepared_generation = self.pending_generation = self.pending_owner = None
        self.account_email = None
        capture, self.capture = self.capture, None
        self.capture_term = None
        if capture and capture is not asyncio.current_task():
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
        await self.browsers.stop(self.workspace_id)
        await self.terminals.stop_locked(self.workspace_id)

    async def stop_locked(self):
        # Caller holds the lifecycle lock; no operation takes it while holding
        # operation_lock. Queries can finish without a lock-order cycle.
        async with self.operation_lock:
            await self._stop_operation_locked()

    async def revoke_binding(self):
        """Fail closed before shared-token replacement; caller holds lifecycle lock."""
        async with self.operation_lock:
            self._persist_binding(None)
            self.runtime["official"].invalidate(code="usage_authorization_required")
            await self._stop_operation_locked()

    async def logout(self, owner):
        async with self.terminals.lock:
            if owner in (self.owner, self.browser_owner, self.pending_owner):
                await self.stop_locked()

    async def reconcile(self):
        # Startup reconciliation already holds the application lifecycle lock.
        async with self.operation_lock:
            await self.broker.reconcile()

    def close_store(self):
        self.closed = True
        if self.db is not None:
            self.db.close()
            self.db = None

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            async with self.terminals.lock:
                try:
                    await self.stop_locked()
                finally:
                    await self.browsers.close()
        finally:
            self.close_store()


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["AUTHORIZE_USAGE_FOR_SHARED_ACCOUNT"]


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["CONFIRM_SAME_CLAUDE_ACCOUNT"]


def install_extra_routes(app, runtime, admin_sessions, admin_authenticated):
    def manager():
        if runtime.get("admin_usage_auth") is None:
            runtime["admin_usage_auth"] = AdminUsageAuth(runtime, admin_sessions)
        return runtime["admin_usage_auth"]

    @app.post("/api/admin/usage-auth/prepare")
    async def prepare(body: PrepareBody, session=Depends(admin_authenticated)):
        return await manager().prepare(session.token)

    @app.post("/api/admin/usage-auth/confirm")
    async def confirm(body: ConfirmBody, session=Depends(admin_authenticated)):
        return await manager().confirm(session.token)
