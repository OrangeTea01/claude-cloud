"""Ephemeral, sandboxed server browsers for a registered workspace's OAuth login."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from .docker_backend import BrokerError

ROLE_LABEL = "io.claude-cloud.role"
EXPIRY_LABEL = "io.claude-cloud.expires-at"
ROLE = "login-browser"
MAX_URL = 16384
OFFICIAL_AUTH_PATHS = {
    "claude.com": "/cai/oauth/authorize",
    "claude.ai": "/oauth/authorize",
    "platform.claude.com": "/oauth/authorize",
    "console.anthropic.com": "/oauth/authorize",
}


def validate_login_url(url: str) -> str:
    """Only initial official OAuth URLs; never an arbitrary browser navigation API."""
    error = "请使用当前 Claude 终端显示的 HTTPS 官方授权链接"
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL:
        raise BrokerError("请粘贴授权链接；链接长度不能超过 16384 个字符")
    if any(ord(c) <= 32 or ord(c) >= 127 for c in url) or "\\" in url:
        raise BrokerError("授权链接中有空白或无效字符，请刷新网页后重新粘贴终端链接")
    if re.search(r"%(?![0-9a-fA-F]{2})", url):
        raise BrokerError("授权链接的百分号编码不完整，请重新复制终端中的链接")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or OFFICIAL_AUTH_PATHS.get(parsed.hostname) != parsed.path):
            raise ValueError()
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
        required = ("client_id", "state", "code_challenge", "response_type")
        missing = [key for key in required if key not in query or not any(query[key])]
        if missing:
            # Only fixed field names appear in errors, never OAuth parameter values.
            raise BrokerError("授权链接缺少参数：" + "、".join(missing) + "。请重新复制当前终端链接，保留参数间的 &")
        repeated = [key for key in required if len(query[key]) != 1]
        if repeated:
            raise BrokerError("授权链接包含重复参数：" + "、".join(repeated) + "。请只粘贴一条当前终端链接")
        if query["response_type"] != ["code"]:
            raise BrokerError("授权链接的 response_type 参数应为 code，请重新复制当前终端链接")
        if "code_challenge_method" in query and query["code_challenge_method"] != ["S256"]:
            raise BrokerError("授权链接的 code_challenge_method 参数应为 S256")
        if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for values in query.values() for value in values):
            raise ValueError()
        if "redirect_uri" in query:
            if len(query["redirect_uri"]) != 1:
                raise ValueError()
            redirect = urlsplit(query["redirect_uri"][0])
            if redirect.username is not None or redirect.password is not None or redirect.fragment or redirect.query:
                raise ValueError()
            public_callback = (redirect.scheme == "https" and redirect.port in (None, 443)
                               and redirect.hostname in ("platform.claude.com", "console.anthropic.com")
                               and redirect.path == "/oauth/code/callback")
            local_callback = (redirect.scheme == "http" and redirect.hostname in ("localhost", "127.0.0.1")
                              and redirect.port is not None and redirect.path == "/callback")
            if not public_callback and not local_callback:
                raise BrokerError("授权链接的回调地址不受支持，请使用当前 Claude 终端生成的链接")
    except (ValueError, UnicodeError):
        raise BrokerError(error) from None
    return url


class LoginBrowsers:
    def __init__(self, broker, image=None, seccomp_path=None, lifetime=1800):
        self.broker = broker
        self.image = image or os.getenv("LOGIN_BROWSER_IMAGE", "claude-cloud-login-browser:local")
        self.seccomp_path = str(seccomp_path or Path(__file__).resolve().parents[1] / "deploy/login_browser/seccomp.json")
        self.lifetime = max(1, min(int(lifetime), 1800))
        self.lock = asyncio.Lock()
        self.leases = {}
        self.channels = {}

    def validate_url(self, url):
        return validate_login_url(url)

    def _name(self, workspace_id):
        return self.broker.names(workspace_id)["container"] + "-login"

    async def _inspect(self, workspace_id):
        info = await self.broker.inspect_owned("container", self._name(workspace_id), workspace_id)
        if info and info.get("Config", {}).get("Labels", {}).get(ROLE_LABEL) != ROLE:
            raise BrokerError("登录浏览器资源归属不匹配，已拒绝操作", 409)
        return info

    @staticmethod
    def _expires(info):
        try:
            return float(info["Config"]["Labels"][EXPIRY_LABEL])
        except (KeyError, ValueError, TypeError):
            return 0

    async def _status_locked(self, workspace_id):
        info = await self._inspect(workspace_id)
        if info and (not info.get("State", {}).get("Running") or self._expires(info) <= time.time()):
            await self._stop_locked(workspace_id)
            info = None
        if not info:
            return {"active": False, "expiresAt": None}
        code, _, _ = await self.broker.command("exec", self._name(workspace_id), "python3", "-I",
                                               "/opt/login-browser/control.py", "ready", check=False, timeout=5)
        if code:
            await self._stop_locked(workspace_id)
            return {"active": False, "expiresAt": None}
        return {"active": True, "expiresAt": datetime.fromtimestamp(self._expires(info), timezone.utc).isoformat()}

    async def status(self, workspace_id):
        async with self.lock:
            return await self._status_locked(workspace_id)

    async def start(self, workspace_id, url):
        url = self.validate_url(url)
        async with self.lock:
            names = self.broker.names(workspace_id)
            workspace = await self.broker.workspace(workspace_id)
            if not workspace["running"]:
                raise BrokerError("请先启动工作区并连接 Claude 登录终端", 409)
            network = await self.broker.inspect_owned("network", names["network"], workspace_id)
            if (not network or not network.get("Internal")
                    or network.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"):
                raise BrokerError("登录浏览器需要工作区的隔离网络", 409)
            # The single-user server has room for one additional browser at a time.
            for key in list(self.leases):
                if key != workspace_id and (await self._status_locked(key))["active"]:
                    raise BrokerError("请先关闭另一个工作区的登录浏览器", 409)
            await self._stop_locked(workspace_id)
            if not Path(self.seccomp_path).is_file():
                raise BrokerError("登录浏览器沙箱配置缺失，请更新网页镜像", 503)
            await self.broker.command("image", "inspect", self.image, timeout=10)
            expires = time.time() + self.lifetime
            try:
                await self.broker.command(
                    "container", "create", "--name", self._name(workspace_id), *self.broker.labels(workspace_id),
                    "--label", f"{ROLE_LABEL}={ROLE}", "--label", f"{EXPIRY_LABEL}={expires}",
                    "--init", "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges=true", "--security-opt", "seccomp=" + self.seccomp_path,
                    "--pids-limit", "256", "--memory", "768m", "--memory-swap", "768m", "--cpus", "1",
                    "--shm-size", "128m", "--ulimit", "nofile=2048:2048", "--restart", "no",
                    "--network", names["network"], "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                    "--env", "HOME=/tmp/browser-home", "--env", "DISPLAY=:99", "--env", "LANG=C.UTF-8",
                    "--env", f"LOGIN_BROWSER_LIFETIME={self.lifetime}", self.image,
                )
                await self.broker.command("container", "start", self._name(workspace_id))
                deadline = asyncio.get_running_loop().time() + 25
                while True:
                    info = await self._inspect(workspace_id)
                    if not info or not info.get("State", {}).get("Running"):
                        raise BrokerError("服务器登录浏览器未能启动；请检查 Chromium 沙箱和服务器资源", 503)
                    code, _, _ = await self.broker.command("exec", self._name(workspace_id), "python3", "-I",
                                                           "/opt/login-browser/control.py", "ready", check=False, timeout=5)
                    if code == 0:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise BrokerError("服务器登录浏览器启动超时，请检查服务器资源", 503)
                    await asyncio.sleep(.25)
                # The URL is deliberately absent from Docker's args, env and logs.
                await self.broker.command("exec", "-i", self._name(workspace_id), "python3", "-I",
                                          "/opt/login-browser/control.py", "open",
                                          input_data=json.dumps({"url": url}).encode(), timeout=10)
                self.leases[workspace_id] = asyncio.create_task(self._expire(workspace_id, expires))
                return {"active": True, "expiresAt": datetime.fromtimestamp(expires, timezone.utc).isoformat()}
            except BaseException:
                await self._stop_locked(workspace_id)
                raise

    async def navigate(self, workspace_id, url):
        """Navigate a live browser without extending its lifetime or sharing it."""
        url = self.validate_url(url)
        async with self.lock:
            if not (await self._status_locked(workspace_id))["active"]:
                raise BrokerError("浏览器已关闭或到期，请重新打开", 409)
            await self.broker.command("exec", "-i", self._name(workspace_id), "python3", "-I",
                                      "/opt/login-browser/control.py", "open",
                                      input_data=json.dumps({"url": url}).encode(), timeout=10)
            return {"active": True, "expiresAt": datetime.fromtimestamp(self._expires(await self._inspect(workspace_id)), timezone.utc).isoformat()}

    async def _expire(self, workspace_id, expires):
        try:
            await asyncio.sleep(max(0, expires - time.time()))
            await self.stop(workspace_id)
        except asyncio.CancelledError:
            pass
        except BrokerError:
            # The container runner independently enforces the same maximum lifetime.
            pass

    async def _stop_locked(self, workspace_id):
        # Verify ownership before touching a channel or removing any container.
        info = await self._inspect(workspace_id)
        task = self.leases.pop(workspace_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        proc = self.channels.pop(workspace_id, None)
        if proc:
            await self._close_proc(proc)
        if info:
            await self.broker.command("container", "rm", "--force", self._name(workspace_id))

    async def stop(self, workspace_id):
        async with self.lock:
            await self._stop_locked(workspace_id)
            return {"ok": True}

    async def open_channel(self, workspace_id):
        async with self.lock:
            if not (await self._status_locked(workspace_id))["active"]:
                raise BrokerError("请先打开服务器登录浏览器", 409)
            current = self.channels.get(workspace_id)
            if current and current.returncode is None:
                raise BrokerError("登录浏览器已在另一个页面打开", 409)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", "-i", "--user", "1000:1000", self._name(workspace_id),
                    "python3", "-I", "/opt/login-browser/relay.py",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                raise BrokerError("无法连接服务器登录浏览器", 503) from None
            self.channels[workspace_id] = proc
            return proc

    @staticmethod
    async def _close_proc(proc):
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def release_channel(self, workspace_id, proc):
        async with self.lock:
            if self.channels.get(workspace_id) is proc:
                self.channels.pop(workspace_id, None)
            await self._close_proc(proc)

    async def reconcile(self, workspaces=None):
        if workspaces is None:
            workspaces = await self.broker.list()
        for workspace in workspaces:
            await self.stop(workspace["id"])

    async def close(self):
        try:
            for workspace_id in set(self.leases) | set(self.channels):
                try:
                    await self.stop(workspace_id)
                except BrokerError:
                    pass
        finally:
            # Docker may be down. Do not leave expiry tasks accessing a broker
            # whose database is about to close. The container also has its own TTL.
            tasks = [task for task in self.leases.values() if task is not asyncio.current_task()]
            self.leases.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            channels = list(self.channels.values())
            self.channels.clear()
            for proc in channels:
                await self._close_proc(proc)
