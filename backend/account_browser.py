"""Private, ephemeral registration/billing browsers for administrator sessions.

Only the fixed official entry pages are accepted by HTTP. The admin operates the
browser manually; no credentials, payment fields or page contents are captured.
"""
import asyncio
import hmac
import json
import logging
import time
from typing import Literal
import uuid

from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from .docker_backend import BrokerError, DockerBroker, RESOURCE_LABEL
from .login_browser import LoginBrowsers

logger = logging.getLogger("claude-cloud.account-browser")
RESOURCE_ID = "account-browser"
PURPOSE_URLS = {
    "register": "https://claude.ai/login",
    "billing": "https://claude.ai/settings/billing",
    "usage": "https://claude.ai/settings/usage",
}


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["register", "billing", "usage"]
    browserId: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    browserId: str = Field(pattern=r"^[a-f0-9]{32}$")


class AccountBrowserBroker(DockerBroker):
    """Network-only facade. It never creates a worker or any persistent volume."""
    def __init__(self, parent):
        self.parent = parent
        self.config, self.owner, self.command = parent.config, parent.owner, parent.command
        self.lock = asyncio.Lock()

    def get(self, workspace_id):
        if workspace_id != RESOURCE_ID:
            raise BrokerError("账号浏览器不存在", 404)
        return {"id": RESOURCE_ID, "name": "注册与订阅浏览器"}

    def labels(self, workspace_id):
        self.get(workspace_id)
        return [*super().labels(workspace_id), "--label", f"{RESOURCE_LABEL}=account-browser"]

    async def inspect_owned(self, kind, name, workspace_id):
        self.get(workspace_id)
        info = await super().inspect_owned(kind, name, workspace_id)
        if info:
            labels = (info.get("Config", {}).get("Labels") if kind == "container" else info.get("Labels")) or {}
            if labels.get(RESOURCE_LABEL) != "account-browser":
                raise BrokerError("账号浏览器资源归属不匹配，已拒绝操作", 409)
        return info

    async def workspace(self, workspace_id):
        item = self.get(workspace_id)
        network = await self.inspect_owned("network", self.names(workspace_id)["network"], workspace_id)
        return {**item, "running": bool(network)}

    async def prepare(self):
        async with self.parent.lock:
            await self.parent.check_engine()
            await self._ensure_workspace_egress(RESOURCE_ID, create_network=True)

    async def cleanup_network(self):
        async with self.parent.lock:
            name = self.names(RESOURCE_ID)["network"]
            network = await self.inspect_owned("network", name, RESOURCE_ID)
            if network:
                # Never disconnect any unknown attached container.
                await self.command("network", "disconnect", "--force", name, self.config.egress_container, check=False)
                await self.command("network", "rm", name)


class PurposeBrowsers(LoginBrowsers):
    def validate_url(self, url):
        if url not in PURPOSE_URLS.values():
            raise BrokerError("请选择注册、订阅或额外用量入口")
        return url


class AccountBrowser:
    def __init__(self, runtime, sessions, *, facade=None, browsers=None, clock=time.time):
        self.runtime, self.sessions, self.clock = runtime, sessions, clock
        self.broker = facade or AccountBrowserBroker(runtime["broker"])
        self.browsers = browsers or PurposeBrowsers(self.broker)
        self.lock = runtime["terminals"].lock
        self.owner = self.browser_id = self.purpose = None
        self.expires = 0
        self.expires_at = None
        self.connection = None
        self.watcher = None
        self.reconciled = False

    def valid_owner(self):
        session = self.sessions.get(self.owner)
        return bool(session and self.admin_identity(session) and self.clock() < self.expires)

    def admin_identity(self, session):
        if self.runtime.get("solo"):
            return session.user_id == self.runtime["credentials"].solo_user_id
        return session.user_id is None

    def valid(self, session, browser_id):
        return (self.sessions.get(session.token) is session and self.admin_identity(session)
                and self.owner == session.token and self.browser_id == browser_id and self.valid_owner())

    def require(self, session):
        current = self.sessions.get(session.token)
        if current is not session or not self.admin_identity(session):
            raise BrokerError("管理员登录已过期，请重新登录管理员页面", 401)

    def metadata(self):
        return {"active": bool(self.owner), "browserId": self.browser_id, "purpose": self.purpose,
                "expiresAt": self.expires_at}

    async def stop_locked(self):
        # Keep the owner/busy marker until deletion succeeds; never offer a
        # profile left behind by a Docker failure to another session.
        await self.browsers.stop(RESOURCE_ID)
        await self.broker.cleanup_network()
        self.owner = self.browser_id = self.purpose = self.expires_at = None
        self.expires = 0
        self.connection = None

    async def reconcile_locked(self):
        if not self.reconciled:
            await self.stop_locked()
            self.reconciled = True

    async def reconcile(self):
        async with self.lock:
            await self.reconcile_locked()

    async def prune_locked(self):
        if self.owner and not self.valid_owner():
            await self.stop_locked()

    async def active_locked(self):
        await self.prune_locked()
        return bool(self.owner)

    async def status(self, session):
        async with self.lock:
            self.require(session)
            await self.reconcile_locked()
            await self.prune_locked()
            if self.owner and self.owner != session.token:
                raise BrokerError("服务器浏览器正在其他会话中使用，请稍后再试", 409)
            if self.owner:
                status = await self.browsers.status(RESOURCE_ID)
                if not status.get("active"):
                    await self.stop_locked()
            self.require(session)
            return self.metadata()

    async def open(self, session, purpose, browser_id=None):
        if purpose not in PURPOSE_URLS:
            raise BrokerError("请选择注册、订阅或额外用量入口")
        async with self.lock:
            self.require(session)
            await self.reconcile_locked()
            await self.prune_locked()
            if self.owner:
                if self.owner != session.token:
                    raise BrokerError("服务器浏览器正在其他会话中使用，请稍后再试", 409)
                if browser_id != self.browser_id:
                    raise BrokerError("浏览器会话已变化，请重新打开面板", 409)
                await self.browsers.navigate(RESOURCE_ID, PURPOSE_URLS[purpose])
                if not self.valid(session, browser_id):
                    await self.stop_locked()
                    raise BrokerError("登录或浏览器已过期，请重新打开", 401)
                self.purpose = purpose
                return self.metadata()
            if browser_id:
                raise BrokerError("浏览器已关闭或到期，请重新打开面板", 409)
            for key in ("admin_claude", "admin_usage_auth"):
                other = self.runtime.get(key)
                if other and (await other.browsers.status(other.workspace_id)).get("active"):
                    raise BrokerError("服务器浏览器正在使用，请稍后再试", 409)
            try:
                await self.broker.prepare()
                status = await self.browsers.start(RESOURCE_ID, PURPOSE_URLS[purpose])
                self.require(session)
                self.owner = session.token
                self.browser_id, self.purpose = uuid.uuid4().hex, purpose
                from datetime import datetime
                self.expires_at = status["expiresAt"]
                self.expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")).timestamp()
                return self.metadata()
            except BaseException:
                await self.stop_locked()
                raise

    async def stop(self, session, browser_id):
        async with self.lock:
            self.require(session)
            await self.prune_locked()
            if self.owner and (self.owner != session.token or self.browser_id != browser_id):
                raise BrokerError("不能关闭其他会话的浏览器", 409)
            if self.owner:
                await self.stop_locked()
            return {"ok": True}

    async def logout(self, token):
        async with self.lock:
            if token == self.owner:
                await self.stop_locked()

    def start_watcher(self):
        if not self.watcher:
            self.watcher = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            await asyncio.sleep(1)
            try:
                async with self.lock:
                    await self.prune_locked()
            except BrokerError:
                # The container also has an independent absolute TTL. Retry
                # deletion without exposing page/user data or reusing a profile.
                pass

    async def close(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
            self.watcher = None
        async with self.lock:
            try:
                await self.stop_locked()
            except BrokerError:
                logger.warning("Account browser cleanup deferred until Docker is available")
            finally:
                await self.browsers.close()


def install_routes(app, config, runtime, admin_sessions, admin_authenticated, admin_cookie):
    def manager():
        service = runtime.get("account_browser")
        if service is None:
            raise HTTPException(503, "服务器浏览器暂不可用，请检查 Docker 和浏览器镜像")
        return service

    @app.websocket("/api/account/claude-browser/display")
    async def retired_user_display(ws: WebSocket):
        await ws.close(code=4403)

    @app.get("/api/admin/claude-account-browser")
    async def status(session=Depends(admin_authenticated)):
        return await manager().status(session)

    @app.post("/api/admin/claude-account-browser")
    async def open_browser(body: OpenBody, session=Depends(admin_authenticated)):
        return await manager().open(session, body.purpose, body.browserId)

    @app.post("/api/admin/claude-account-browser/stop")
    async def stop(body: StopBody, session=Depends(admin_authenticated)):
        return await manager().stop(session, body.browserId)

    @app.websocket("/api/admin/claude-account-browser/display")
    async def display(ws: WebSocket):
        session = admin_sessions.get(ws.cookies.get(admin_cookie))
        service = runtime.get("account_browser")
        if (not config.management_password_hash or not session or not service or not service.admin_identity(session)
                or ws.headers.get("origin") != config.public_origin):
            await ws.close(code=4403)
            return
        await ws.accept()
        proc, connection, tasks, close_code = None, object(), [], 1000
        try:
            raw = await asyncio.wait_for(ws.receive_text(), 10)
            if len(raw) > 4096:
                close_code = 4403
                return
            first = json.loads(raw)
            if (not isinstance(first, dict) or first.get("type") != "auth"
                    or not isinstance(first.get("csrfToken"), str)
                    or not hmac.compare_digest(first["csrfToken"].encode(), session.csrf.encode())
                    or not isinstance(first.get("browserId"), str)):
                close_code = 4403
                return
            browser_id = first["browserId"]
            async with service.lock:
                if not service.valid(session, browser_id):
                    close_code = 4403
                    return
                if service.connection is not None:
                    close_code = 4409
                    return
                proc = await service.browsers.open_channel(RESOURCE_ID)
                if not service.valid(session, browser_id):
                    close_code = 4403
                    return
                service.connection = connection

            def valid():
                return service.valid(session, browser_id) and service.connection is connection

            async def output():
                while valid():
                    data = await proc.stdout.read(65536)
                    if not data or not valid():
                        return
                    await ws.send_bytes(data)

            async def inputs():
                while valid():
                    data = await ws.receive_bytes()
                    if not valid():
                        return
                    if len(data) > 1048576:
                        raise ValueError("oversized browser input")
                    proc.stdin.write(data)
                    await proc.stdin.drain()

            async def watch():
                while valid():
                    await asyncio.sleep(.5)

            tasks = [asyncio.create_task(output()), asyncio.create_task(inputs()), asyncio.create_task(watch())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if not valid():
                close_code = 4403
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except (BrokerError, asyncio.TimeoutError, ValueError, KeyError, OSError, RuntimeError):
            close_code = 1011
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if service.connection is connection:
                service.connection = None
            if proc is not None:
                await service.browsers.release_channel(RESOURCE_ID, proc)
            if not admin_sessions.get(session.token):
                await service.logout(session.token)
            try:
                await ws.close(code=close_code)
            except (RuntimeError, WebSocketDisconnect):
                pass
