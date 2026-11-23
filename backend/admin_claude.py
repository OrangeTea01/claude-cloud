"""Administrator-only Claude authorization transport and shared credential rotation."""
import asyncio
import hmac
import json
import re

from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from .docker_backend import BrokerError
from .login_browser import LoginBrowsers
from .terminal import Terminal, Terminals


class LoginBrowserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=16384)


def extract_setup_token(output):
    """Accept one complete, bounded token from successful setup-token output."""
    if not isinstance(output, str) or len(output) > 262144:
        return None
    clean = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    found = list(re.finditer(r"(?<![A-Za-z0-9_-])(sk-ant-oat01-[A-Za-z0-9_-]{64,4096})(?![A-Za-z0-9_-])", clean))
    # A hard-wrapped token must never be accepted as a valid-looking prefix.
    # The login PTY is wide; refuse ambiguous continuations and request a retry.
    if any(re.match(r"[ \t]*\r?\n[ \t]*(?!sk-ant-oat01-)[A-Za-z0-9_-]{20,}(?:[ \t]*\r?\n|$)", clean[item.end():]) for item in found):
        return None
    matches = {item.group(1) for item in found}
    return matches.pop() if len(matches) == 1 else None


class AuthTerminal(Terminal):
    """Hide bearer output before it reaches websocket queues or replay buffers."""
    def __init__(self, command):
        super().__init__(command)
        self.private_output = ""
        self.output_overflow = False
        self.pending_prefix = ""
        self.ansi_state = "normal"
        self.secret_seen = False

    def resize(self, cols, rows):
        # Avoid Ink inserting line breaks inside the setup token. The browser
        # terminal still wraps this display locally to fit a phone or laptop.
        super().resize(400, rows)

    def filter_output(self, output):
        if len(self.private_output) + len(output) <= 262144:
            self.private_output += output
        else:
            self.output_overflow = True
            self.private_output = ""
        if self.secret_seen:
            return ""
        clean = []
        for char in output:
            if self.ansi_state == "normal":
                if char == "\x1b":
                    self.ansi_state = "escape"
                else:
                    clean.append(char)
            elif self.ansi_state == "escape":
                self.ansi_state = "csi" if char == "[" else "osc" if char == "]" else "normal"
            elif self.ansi_state == "csi":
                if "@" <= char <= "~":
                    self.ansi_state = "normal"
            elif self.ansi_state == "osc":
                if char == "\x07":
                    self.ansi_state = "normal"
                elif char == "\x1b":
                    self.ansi_state = "osc_escape"
            elif self.ansi_state == "osc_escape":
                self.ansi_state = "normal" if char == "\\" else "osc"
        text = self.pending_prefix + "".join(clean)
        self.pending_prefix = ""
        prefix = "sk-ant-oat01-"
        if prefix in text:
            self.secret_seen = True
            return text.split(prefix, 1)[0] + "[授权凭据已隐藏，正在保存]\r\n"
        for length in range(min(len(prefix) - 1, len(text)), 0, -1):
            if text.endswith(prefix[:length]):
                self.pending_prefix = text[-length:]
                return text[:-length]
        return text


class AdminClaude:
    def __init__(self, runtime, admin_sessions):
        self.runtime = runtime
        self.broker = runtime["broker"].admin_auth()
        self.workspace_id = self.broker.workspace_id
        self.terminals = Terminals(self.broker, terminal_factory=AuthTerminal)
        self.terminals.lock = runtime["terminals"].lock
        self.browsers = LoginBrowsers(self.broker)
        self.owner = None
        self.browser_owner = None
        self.connection = None
        self.capture = None
        self.capture_term = None
        self.error = None
        self.importing = False
        self.admin_sessions = admin_sessions
        self.connect_lock = asyncio.Lock()

    def require_admin(self, owner):
        if not self.admin_sessions.get(owner):
            raise BrokerError("管理员登录已过期，请重新登录", 401)

    async def status(self):
        status = await self.broker.auth_status()
        finalizing = bool(self.capture and not self.capture.done() and self.capture_term and self.capture_term.finished)
        return {"authenticated": bool(status["authenticated"]),
                "terminalActive": self.terminals.active(self.workspace_id),
                "importing": self.importing or finalizing, "error": self.error}

    async def attach(self, owner):
        async with self.connect_lock:
            self.require_admin(owner)
            if (self.capture and not self.capture.done()
                    and (self.importing or (self.capture_term and self.capture_term.finished))):
                raise BrokerError("正在保存上一轮授权，请等待状态更新后再连接", 409)
            if self.owner is not None and self.owner != owner:
                async with self.terminals.lock:
                    self.require_admin(owner)
                    await self.stop_locked()
            self.terminals.require_policy = lambda: self.require_admin(owner)
            term, queue = await self.terminals.attach(self.workspace_id)
            if not self.admin_sessions.get(owner):
                async with self.terminals.lock:
                    await self.stop_locked()
                raise BrokerError("管理员登录已过期，请重新登录", 401)
            self.owner = owner
            if self.capture is None or self.capture.done():
                self.error = None
                self.capture_term = term
                self.capture = asyncio.create_task(self._capture_token(term, owner))
            return term, queue

    async def _capture_token(self, term, owner):
        try:
            deadline = asyncio.get_running_loop().time() + 1800
            while not term.watch.done():
                self.require_admin(owner)
                if asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError()
                await asyncio.wait({term.watch}, timeout=1)
            await term.watch
            self.require_admin(owner)
            if not term.proc or term.proc.returncode != 0:
                return
            token = None if term.output_overflow else extract_setup_token(term.private_output)
            term.private_output = ""
            if not token:
                self.error = "未能自动读取登录凭据，请重新连接管理员登录终端完成授权"
                return
            self.importing = True
            async with self.terminals.lock:
                self.require_admin(owner)
                # Every running workspace must stop before replacing the shared
                # account. A failure retains the previous credential unchanged.
                workspaces = await self.runtime["broker"].list()
                affected = {item["id"] for item in workspaces if item["running"]}
                affected.update(self.runtime["chats"].items)
                for workspace in affected:
                    await self.runtime["chats"].close_locked(workspace)
                    await self.runtime["terminals"].stop_locked(workspace)
                self.require_admin(owner)
                # Revoke the private usage association durably BEFORE changing
                # the inference grant. A crash between separate stores must not
                # pair a former account's usage with a newly saved token.
                if self.runtime.get("admin_usage_auth") is not None:
                    await self.runtime["admin_usage_auth"].revoke_binding()
                    self.require_admin(owner)
                commit = asyncio.create_task(self.runtime["broker"].save_shared_auth(token))
                try:
                    await asyncio.shield(commit)
                except asyncio.CancelledError:
                    await commit
                    if self.runtime.get("official"):
                        self.runtime["official"].clear()
                    if self.runtime.get("quotas"):
                        self.runtime["quotas"].clear_windows()
                    if self.runtime.get("shares"):
                        self.runtime["shares"].account_changed()
                    raise
                if self.runtime.get("official"):
                    self.runtime["official"].clear()
                if self.runtime.get("quotas"):
                    self.runtime["quotas"].clear_windows()
                if self.runtime.get("shares"):
                    self.runtime["shares"].account_changed()
                self.error = None
                term.replay.clear()
                term.replay_size = 0
        except asyncio.TimeoutError:
            self.error = "管理员登录终端已超时，请重新连接"
            async with self.terminals.lock:
                await self.stop_locked()
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            if exc.status == 401:
                async with self.terminals.lock:
                    if self.owner == owner:
                        await self.stop_locked()
            else:
                self.error = "未能确认授权保存，请检查 Docker 和服务器存储后刷新状态或重新授权"
        except Exception:
            self.error = "未能确认授权保存，请检查 Docker 和服务器存储后刷新状态或重新授权"
        finally:
            term.private_output = ""
            self.importing = False

    async def stop_locked(self):
        self.owner = None
        self.browser_owner = None
        capture, self.capture = self.capture, None
        self.capture_term = None
        if capture and capture is not asyncio.current_task():
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
        await self.browsers.stop(self.workspace_id)
        await self.terminals.stop_locked(self.workspace_id)

    async def logout(self, owner):
        async with self.terminals.lock:
            if owner in (self.owner, self.browser_owner):
                await self.stop_locked()

    async def close(self):
        async with self.terminals.lock:
            try:
                await self.stop_locked()
            finally:
                await self.browsers.close()


def install_routes(app, config, runtime, admin_sessions, admin_authenticated, admin_cookie, reconcile,
                   *, prefix="/api/admin/claude", manager_key="admin_claude", factory=AdminClaude):
    def manager():
        if runtime.get(manager_key) is None:
            runtime[manager_key] = factory(runtime, admin_sessions)
        return runtime[manager_key]

    def current(session, service=None):
        return bool(admin_sessions.get(session.token) and (service is None or service.owner == session.token))

    async def authenticate_socket(ws):
        session = admin_sessions.get(ws.cookies.get(admin_cookie)) if config.management_password_hash else None
        if not session or ws.headers.get("origin") != config.public_origin:
            await ws.close(code=4403)
            return None
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), 10)
            valid = (current(session) and isinstance(first, dict) and first.get("type") == "auth"
                     and isinstance(first.get("csrfToken"), str)
                     and hmac.compare_digest(first["csrfToken"].encode(), session.csrf.encode()))
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
            valid = False
        if not valid:
            await ws.close(code=4403)
            return None
        return session

    @app.get(prefix + "/status", dependencies=[Depends(admin_authenticated)])
    async def status():
        return await manager().status()

    @app.post(prefix + "/terminal/stop")
    async def stop_terminal(session=Depends(admin_authenticated)):
        service = manager()
        async with service.terminals.lock:
            if not current(session):
                raise HTTPException(401, "管理员登录已过期，请重新登录")
            await service.stop_locked()
        return {"ok": True}

    @app.get(prefix + "/login-browser")
    async def browser_status(session=Depends(admin_authenticated)):
        service = manager()
        if service.browser_owner not in (None, session.token):
            raise HTTPException(409, "登录浏览器正在另一个管理员会话中使用")
        return await service.browsers.status(service.workspace_id)

    @app.post(prefix + "/login-browser")
    async def browser_start(body: LoginBrowserBody, session=Depends(admin_authenticated)):
        service = manager()
        async with service.terminals.lock:
            if not current(session, service):
                raise HTTPException(401, "请重新连接当前管理员登录终端")
            if not service.terminals.active(service.workspace_id):
                raise HTTPException(409, "请先连接管理员登录终端，取得官方授权链接")
            account_browser = runtime.get("account_browser")
            if account_browser is not None and await account_browser.active_locked():
                raise HTTPException(409, "注册与订阅浏览器正在使用，请稍后再打开授权浏览器")
            # All authorization services share this lock. A second private
            # browser must not bypass the server's single-browser budget.
            for key in ("admin_claude", "admin_usage_auth"):
                other = runtime.get(key)
                if other is not None and other is not service:
                    if (await other.browsers.status(other.workspace_id)).get("active"):
                        raise HTTPException(409, "请先关闭另一项 Claude 授权的登录浏览器")
            result = await service.browsers.start(service.workspace_id, body.url)
            if not current(session, service):
                await service.browsers.stop(service.workspace_id)
                raise HTTPException(401, "管理员登录已过期，请重新登录")
            service.browser_owner = session.token
            return result

    @app.post(prefix + "/login-browser/stop")
    async def browser_stop(session=Depends(admin_authenticated)):
        service = manager()
        async with service.terminals.lock:
            if not current(session):
                raise HTTPException(401, "管理员登录已过期，请重新登录")
            if service.browser_owner not in (None, session.token):
                raise HTTPException(409, "登录浏览器正在另一个管理员会话中使用")
            await service.browsers.stop(service.workspace_id)
            service.browser_owner = None
        return {"ok": True}

    @app.websocket(prefix + "/terminal")
    async def terminal(ws: WebSocket):
        session = await authenticate_socket(ws)
        if not session:
            return
        service, term, queue, tasks = None, None, None, []
        try:
            await reconcile()
            if not current(session):
                await ws.close(code=4403)
                return
            service = manager()
            term, queue = await service.attach(session.token)
            if not current(session, service):
                await service.logout(session.token)
                return

            async def output():
                while current(session, service):
                    try:
                        item = await asyncio.wait_for(queue.get(), 1)
                    except asyncio.TimeoutError:
                        continue
                    if not current(session, service) or item["type"] == "disconnect":
                        return
                    await ws.send_json(item)
                    if item["type"] == "exit":
                        return

            async def inputs():
                while current(session, service):
                    raw = await ws.receive_text()
                    if not current(session, service):
                        return
                    if len(raw) > 100000:
                        raise BrokerError("终端消息过长")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise BrokerError("无效的终端消息")
                    if message.get("type") == "input" and isinstance(message.get("data"), str):
                        await term.input(message["data"])
                    elif message.get("type") == "resize":
                        cols, rows = message.get("cols"), message.get("rows")
                        if type(cols) is not int or type(rows) is not int:
                            raise BrokerError("无效的终端尺寸")
                        term.resize(cols, rows)

            tasks = [asyncio.create_task(output()), asyncio.create_task(inputs())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except (BrokerError, asyncio.TimeoutError, ValueError):
            try:
                await ws.send_json({"type": "error", "message": "管理员登录终端连接失败，请检查服务状态后重试"})
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if term and queue is not None:
                term.detach(queue)
            if service and not current(session):
                await service.logout(session.token)
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    @app.websocket(prefix + "/login-browser/display")
    async def display(ws: WebSocket):
        session = await authenticate_socket(ws)
        if not session:
            return
        service, proc, tasks = manager(), None, []
        connection, close_code = object(), 1000
        try:
            async with service.terminals.lock:
                if not current(session) or service.browser_owner != session.token:
                    close_code = 4403
                    return
                if service.connection is not None:
                    close_code = 4409
                    return
                proc = await service.browsers.open_channel(service.workspace_id)
                service.connection = connection

            def valid():
                return current(session) and service.browser_owner == session.token

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
                    await asyncio.sleep(1)

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
                await service.browsers.release_channel(service.workspace_id, proc)
            if not current(session):
                await service.logout(session.token)
            try:
                await ws.close(code=close_code)
            except (RuntimeError, WebSocketDisconnect):
                pass
