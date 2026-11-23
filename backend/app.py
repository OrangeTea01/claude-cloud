import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, StrictBool, field_validator, model_validator
from starlette.responses import Response

from .auth import Sessions, hash_password, valid_password_hash, verify_password
from .credentials import CredentialStore, InstallationModeError
from .docker_backend import BrokerError, Config, DockerBroker
from .terminal import Terminals
from .chat import Chats
from .login_browser import LoginBrowsers
from .account_browser import AccountBrowser, install_routes as install_account_browser_routes
from .admin_claude import install_routes as install_admin_claude_routes
from .admin_usage_auth import AdminUsageAuth, install_extra_routes as install_usage_auth_routes
from .file_helper import MAX_ENCODED_SIZE, MAX_REQUEST_SIZE, attachment_parts, upload_name
from .credentials import normalize_username
from .workspace_access import WorkspaceAccess, install_routes as install_workspace_routes
from .quotas import QuotaStore
from .official_shares import OfficialShares
from .official_usage import OfficialUsage
from .usage_control import UsageControl
from .usage_routes import install_routes as install_usage_routes
from .archive_download import ArchiveResponse, prepare_connected

COOKIE = "claude_cloud_session"
ADMIN_COOKIE = "claude_cloud_admin_session"
ADMIN_COOKIE_PATH = "/api/admin"
logger = logging.getLogger("claude-cloud")


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class LoginBody(PasswordBody):
    username: str | None = Field(default=None, min_length=3, max_length=32)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        return normalize_username(value) if value is not None else None


class UserPasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    newPassword: str = Field(min_length=12, max_length=1024)
    confirmPassword: str = Field(min_length=12, max_length=1024)


class SitePasswordBody(UserPasswordBody):
    currentPassword: str = Field(min_length=1, max_length=1024)


class ModelPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fableAllowed: StrictBool


class NameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    password: str = Field(default="", max_length=1024)
    mode: Literal["code", "chat"] = "code"

    @field_validator("password")
    @classmethod
    def optional_password(cls, value):
        if value and len(value) < 8:
            raise ValueError("工作区密码至少 8 个字符")
        return value


class PathBody(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class FileBody(PathBody):
    content: str = Field(max_length=MAX_ENCODED_SIZE)
    encoding: str = "utf-8"


class UploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=MAX_ENCODED_SIZE)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        return upload_name(value)


class AttachmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def safe_attachment(self):
        if attachment_parts(self.path)[-1] != self.name:
            raise ValueError("附件名称与上传路径不一致")
        return self


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=16000)
    attachments: list[AttachmentBody] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def nonblank_message(self):
        if not self.text.strip() and not self.attachments:
            raise ValueError("请输入消息或添加附件")
        return self


class ChatModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=32)


class PermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: StrictBool


class LoginBrowserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=16384)


class BodyLimit:
    """Bound request size before JSON parsing, including chunked request bodies."""
    def __init__(self, app, sessions, origin):
        self.app = app
        self.sessions = sessions
        self.origin = origin
        self.file_requests = asyncio.Semaphore(2)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            return await self.app(scope, receive, send)
        file_request = ((scope["method"] == "POST" and re.fullmatch(r"/api/workspaces/[a-f0-9]{32}/attachments", scope["path"]))
                        or (scope["method"] == "PUT" and re.fullmatch(r"/api/workspaces/[a-f0-9]{32}/file", scope["path"])))
        if file_request:
            # Reject anonymous/cross-site uploads before buffering their larger
            # bodies. Route dependencies still validate again before mutation.
            request = Request(scope)
            session = self.sessions.get(request.cookies.get(COOKIE))
            if not session:
                return await JSONResponse({"detail": "请先登录"}, status_code=401)(scope, receive, send)
            if (request.headers.get("origin") != self.origin
                    or not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf.encode())):
                return await JSONResponse({"detail": "请求校验失败，请刷新页面"}, status_code=403)(scope, receive, send)
            async with self.file_requests:
                return await self.limited(scope, receive, send, MAX_REQUEST_SIZE)
        return await self.limited(scope, receive, send, 3 * 1024 * 1024)

    async def limited(self, scope, receive, send, limit):
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse({"detail": "请求过大，单文件上限 10 MiB"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


def create_app(config: Config | None = None, broker=None, translator=None, login_browsers=None, account_browser=None):
    config = config or Config.from_env()
    if config.app_mode not in ("multiuser", "solo"):
        raise RuntimeError("APP_MODE 必须为 solo 或 multiuser")
    solo = config.app_mode == "solo"
    if solo:
        # This value only enables existing administrative transports. Password
        # verification in solo mode always uses the durable site's credential.
        config.management_password_hash = config.password_hash
    sessions = Sessions()
    admin_sessions = sessions if solo else Sessions(ttl=1800)
    admin_cookie = COOKIE if solo else ADMIN_COOKIE
    admin_cookie_path = "/" if solo else ADMIN_COOKIE_PATH
    login_slots = asyncio.Semaphore(2)
    admin_login_slots = asyncio.Semaphore(2)
    credential_lock = asyncio.Lock()
    admin_update_lock = asyncio.Lock()
    model_policy_lock = asyncio.Lock()
    runtime = {"broker": broker, "terminals": None, "chats": None, "login_browsers": login_browsers, "reconciled": False,
               "credentials": None, "admin_claude": None, "admin_usage_auth": None, "account_browser": account_browser,
               "solo": solo}
    reconcile_lock = asyncio.Lock()
    browser_owners = {}
    access = WorkspaceAccess(runtime, sessions)

    async def reconcile():
        async with reconcile_lock:
            async with runtime["terminals"].lock:
                if runtime["reconciled"]:
                    return
                if runtime["account_browser"] is not None:
                    await runtime["account_browser"].reconcile_locked()
                old_workspaces = await runtime["broker"].list()
                await runtime["login_browsers"].reconcile(old_workspaces)
                await runtime["broker"].reconcile_admin_auth()
                if runtime["admin_usage_auth"] is not None:
                    await runtime["admin_usage_auth"].reconcile()
                for workspace in old_workspaces:
                    if workspace["running"]:
                        await runtime["broker"].stop(workspace["id"])
                await runtime["broker"].apply_model_policy(runtime["credentials"].fable_allowed)
                runtime["chats"].policy_ready = True
                runtime["chats"].policy_changed()
                runtime["reconciled"] = True

    @asynccontextmanager
    async def lifespan(app):
        origin = urlsplit(config.public_origin)
        if origin.scheme not in ("http", "https") or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username:
            raise RuntimeError("PUBLIC_ORIGIN 必须是完整来源，如 https://claude.example.com，不含路径")
        if origin.scheme == "http" and origin.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise RuntimeError("公网部署必须使用 HTTPS；HTTP 仅允许 localhost SSH 隧道")
        if origin.scheme == "https" and not config.cookie_secure:
            raise RuntimeError("HTTPS 部署必须设置 COOKIE_SECURE=true")
        if not valid_password_hash(config.password_hash):
            raise RuntimeError("请先运行 scripts/configure.py 初始化网站配置")
        if config.management_password_hash and not valid_password_hash(config.management_password_hash):
            raise RuntimeError("管理员密码配置无效，请检查 MANAGEMENT_PASSWORD_HASH")
        try:
            runtime["credentials"] = CredentialStore(config.data_dir, config.password_hash, app_mode=config.app_mode)
        except InstallationModeError as exc:
            raise RuntimeError(str(exc)) from None
        except Exception:
            raise RuntimeError("无法读取用户密码存储，请检查数据卷权限及磁盘空间") from None
        try:
            if not runtime["broker"]:
                runtime["broker"] = DockerBroker(config)
            if runtime["credentials"].user(runtime["credentials"].legacy_user_id) is not None:
                runtime["broker"].assign_legacy_owner(runtime["credentials"].legacy_user_id)
            runtime["broker"].fable_allowed = runtime["credentials"].fable_allowed
            runtime["terminals"] = Terminals(runtime["broker"])
            runtime["quotas"] = QuotaStore(config.data_dir)
            runtime["shares"] = OfficialShares(runtime["quotas"])
            runtime["official"] = OfficialUsage(config.data_dir)
            runtime["usage"] = UsageControl(runtime["broker"], runtime["credentials"], runtime["quotas"],
                                            runtime["official"], config.data_dir, shares=runtime["shares"], solo=solo)
            runtime["chats"] = Chats(runtime["broker"], runtime["terminals"], translator, config.data_dir,
                                     policy=runtime["credentials"], usage=runtime["usage"])
            runtime["usage"].chats = runtime["chats"]
            # Production queries use a private profile-authorized CLI. User
            # workers retain only the inference grant. Memory test brokers do
            # not create Docker resources or perform external authorization.
            if isinstance(runtime["broker"], DockerBroker):
                runtime["admin_usage_auth"] = AdminUsageAuth(runtime, admin_sessions)
                runtime["usage"].official_fetcher = runtime["admin_usage_auth"].fetch_usage
            runtime["chats"].policy_ready = False
            if runtime["login_browsers"] is None:
                runtime["login_browsers"] = LoginBrowsers(runtime["broker"])
            if runtime["account_browser"] is None and isinstance(runtime["broker"], DockerBroker):
                runtime["account_browser"] = AccountBrowser(runtime, admin_sessions)
            # A web service restart cannot reconnect an old local PTY. Stop only owned
            # containers, retaining all files/login state, to avoid hidden Claude runs.
            try:
                await reconcile()
            except BrokerError:
                logger.warning("Docker unavailable during startup; workspace endpoints will report its status")
            if runtime["account_browser"] is not None:
                runtime["account_browser"].start_watcher()
            runtime["usage"].start()
        except BaseException:
            if runtime.get("account_browser"):
                await runtime["account_browser"].close()
            if runtime.get("usage"):
                await runtime["usage"].close()
            if runtime.get("admin_usage_auth"):
                runtime["admin_usage_auth"].close_store()
            for key in ("quotas", "official"):
                if runtime.get(key):
                    runtime[key].close()
            runtime["credentials"].close()
            raise
        try:
            yield
        finally:
            try:
                if runtime["usage"].task:
                    runtime["usage"].task.cancel()
                    await asyncio.gather(runtime["usage"].task, return_exceptions=True)
                if runtime.get("bulk_delete_task"):
                    try:
                        await runtime["bulk_delete_task"]
                    except Exception:
                        logger.warning("Workspace cleanup did not fully complete; continuing service shutdown")
                if runtime["account_browser"] is not None:
                    await runtime["account_browser"].close()
                if runtime["admin_claude"] is not None:
                    await runtime["admin_claude"].close()
                if runtime["admin_usage_auth"] is not None:
                    await runtime["admin_usage_auth"].close()
                await runtime["login_browsers"].close()
                await runtime["chats"].shutdown()
                await runtime["terminals"].shutdown()
                runtime["broker"].close()
            finally:
                await runtime["usage"].close()
                runtime["quotas"].close()
                runtime["official"].close()
                runtime["credentials"].close()
                sessions.revoke_all()
                admin_sessions.revoke_all()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    app.state.admin_sessions = admin_sessions
    app.state.runtime = runtime
    app.add_middleware(BodyLimit, sessions=sessions, origin=config.public_origin)

    @app.middleware("http")
    async def headers(request, call_next):
        if solo and (re.fullmatch(r"/api/admin/(?:users(?:/.*)?|quotas(?:/.*)?|user-password)", request.url.path)
                     or re.fullmatch(r"/api/workspaces/[a-f0-9]{32}/(?:lock|unlock|password)", request.url.path)):
            response = JSONResponse({"detail": "独享版不提供此操作"}, status_code=404)
        else:
            response = await call_next(request)
        # Do not return private contents if a password reset/lock happened while
        # a Docker read or other awaited operation was in flight.
        workspace_id = request.path_params.get("workspace_id")
        if workspace_id and request.url.path.startswith("/api/workspaces/") and response.status_code < 400:
            session = sessions.get(request.cookies.get(COOKIE))
            try:
                if request.method != "DELETE" or request.url.path.endswith("/file"):
                    access.check(workspace_id, session, unlocked=not request.url.path.endswith(("/unlock", "/lock", "/password")))
                else:
                    access.user(session)
                if request.method in ("POST", "PUT", "PATCH"):
                    runtime["usage"].touch(workspace_id)
            except (HTTPException, BrokerError) as exc:
                response = JSONResponse({"detail": exc.detail if isinstance(exc, HTTPException) else str(exc)},
                                        status_code=exc.status_code if isinstance(exc, HTTPException) else exc.status)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(BrokerError)
    async def broker_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Do not reflect passwords/file content in Pydantic error payloads.
        return JSONResponse({"detail": "请求格式或字段长度不正确"}, status_code=422)

    def check_origin(origin):
        if origin != config.public_origin:
            raise HTTPException(403, "来源校验失败，请使用配置的网页地址")

    async def authenticated(request: Request):
        session = sessions.get(request.cookies.get(COOKIE))
        if not session:
            raise HTTPException(401, "请先登录控制台")
        access.user(session)
        if request.method not in ("GET", "HEAD"):
            check_origin(request.headers.get("origin"))
            if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()):
                raise HTTPException(403, "会话校验失败，请刷新后重试")
        workspace_id = request.path_params.get("workspace_id")
        if workspace_id and request.url.path.startswith("/api/workspaces/"):
            # Retired login transports always return 403 and expose no data.
            if not request.url.path.endswith(("/terminal/stop", "/login-browser", "/login-browser/stop")):
                access.check(workspace_id, session, unlocked=not request.url.path.endswith(("/unlock", "/lock", "/password")))
        return session

    def admin_enabled():
        if not config.management_password_hash:
            raise HTTPException(503, "管理员入口尚未启用，请先在服务器运行 scripts/configure_admin.py")

    async def admin_authenticated(request: Request):
        admin_enabled()
        session = admin_sessions.get(request.cookies.get(admin_cookie))
        if not session:
            raise HTTPException(401, "请先登录管理员页面")
        if solo:
            access.user(session)
        if request.method not in ("GET", "HEAD"):
            check_origin(request.headers.get("origin"))
            if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()):
                raise HTTPException(403, "管理员会话校验失败，请刷新后重试")
        return session

    @app.get("/api/health")
    async def health():
        return {"ok": True, **({"installationId": config.installation_id} if config.installation_id else {})}

    @app.get("/api/session")
    async def session_info(request: Request):
        session = sessions.get(request.cookies.get(COOKIE))
        user = runtime["credentials"].user(session.user_id) if session else None
        return login_view(session, user) if user and not user.get("deleting") else {"authenticated": False, **({"mode": "solo"} if solo else {})}

    def login_view(session, user):
        identity = {"id": user["id"]} if solo else {"id": user["id"], "username": user["username"]}
        return {"authenticated": True, "csrfToken": session.csrf, "user": identity,
                **({"mode": "solo", "admin": True} if solo else {})}

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        check_origin(request.headers.get("origin"))
        if (solo and body.username is not None) or (not solo and body.username is None):
            raise HTTPException(422, "独享版只需网站密码" if solo else "请填写用户名")
        if not sessions.allow_login(f"{request.client.host if request.client else 'unknown'}:{'solo' if solo else body.username}"):
            raise HTTPException(429, "尝试次数过多，请 5 分钟后重试")
        async with login_slots:
            async with credential_lock:
                user = (runtime["credentials"].user(runtime["credentials"].solo_user_id) if solo
                        else runtime["credentials"].user_by_username(body.username))
                encoded = user["passwordHash"] if user else config.password_hash
                revision = user["revision"] if user else None
            valid = await asyncio.to_thread(verify_password, body.password, encoded)
        async with credential_lock:
            # A reset may finish while scrypt runs in a thread. An old password
            # must never mint a fresh session after all sessions were revoked.
            current = runtime["credentials"].user(user["id"]) if user else None
            if not valid or current is None or current.get("deleting") or current["revision"] != revision:
                raise HTTPException(401, "用户名或密码不正确，或密码已更新")
            sessions.remove(request.cookies.get(COOKIE))
            session = sessions.create(user_id=user["id"])
        response = JSONResponse(login_view(session, user))
        response.set_cookie(COOKIE, session.token, httponly=True, secure=config.cookie_secure, samesite="strict", max_age=sessions.ttl, path="/")
        return response

    @app.post("/api/logout", dependencies=[Depends(authenticated)])
    async def logout(request: Request):
        token = request.cookies.get(COOKIE)
        sessions.remove(token)
        if solo:
            await cleanup_administrator(token)
        async with runtime["terminals"].lock:
            for workspace_id, owner in list(browser_owners.items()):
                if owner == token:
                    await runtime["login_browsers"].stop(workspace_id)
                    browser_owners.pop(workspace_id, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=config.cookie_secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/admin/session")
    async def admin_session_info(request: Request):
        enabled = bool(config.management_password_hash)
        session = admin_sessions.get(request.cookies.get(admin_cookie)) if enabled else None
        result = {"enabled": enabled, "authenticated": bool(session), **({"mode": "solo"} if solo else {})}
        if session:
            result["csrfToken"] = session.csrf
        return result

    @app.post("/api/admin/login")
    async def admin_login(body: PasswordBody, request: Request):
        if solo:
            return await login(LoginBody(password=body.password), request)
        check_origin(request.headers.get("origin"))
        admin_enabled()
        if not admin_sessions.allow_login(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "管理员登录尝试过多，请 5 分钟后重试")
        async with admin_login_slots:
            valid = await asyncio.to_thread(verify_password, body.password, config.management_password_hash)
        if not valid:
            raise HTTPException(401, "管理员密码不正确")
        admin_sessions.remove(request.cookies.get(admin_cookie))
        session = admin_sessions.create()
        response = JSONResponse({"authenticated": True, "csrfToken": session.csrf})
        response.set_cookie(admin_cookie, session.token, httponly=True, secure=config.cookie_secure,
                            samesite="strict", max_age=admin_sessions.ttl, path=admin_cookie_path)
        return response

    @app.post("/api/admin/logout", dependencies=[Depends(admin_authenticated)])
    async def admin_logout(request: Request):
        if solo:
            return await logout(request)
        token = request.cookies.get(admin_cookie)
        admin_sessions.remove(token)
        await cleanup_administrator(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(admin_cookie, path=admin_cookie_path, secure=config.cookie_secure,
                               httponly=True, samesite="strict")
        return response

    async def cleanup_administrator(token):
        if runtime["account_browser"] is not None:
            await runtime["account_browser"].logout(token)
        if runtime["admin_claude"] is not None:
            await runtime["admin_claude"].logout(token)
        if runtime["admin_usage_auth"] is not None:
            await runtime["admin_usage_auth"].logout(token)

    @app.post("/api/settings/password")
    async def site_password(body: SitePasswordBody, session=Depends(authenticated)):
        if not solo:
            raise HTTPException(404, "接口不存在")
        if body.newPassword != body.confirmPassword:
            raise HTTPException(400, "两次输入的新密码不一致")
        if not sessions.allow_login("password-change:" + session.token):
            raise HTTPException(429, "尝试次数过多，请稍后重试")
        async with admin_update_lock:
            async with credential_lock:
                user = access.user(session)
                saved, revision = user["passwordHash"], user["revision"]
            async with login_slots:
                valid = await asyncio.to_thread(verify_password, body.currentPassword, saved)
                if not valid:
                    raise HTTPException(400, "当前网站密码不正确")
                encoded = await asyncio.to_thread(hash_password, body.newPassword)
            async with credential_lock:
                current = access.user(session)
                if current["revision"] != revision:
                    raise HTTPException(409, "密码已更新，请重新登录")
                try:
                    runtime["credentials"].replace_user_password(user["id"], encoded)
                except Exception:
                    raise HTTPException(503, "密码保存失败，原密码仍有效") from None
                tokens = list(sessions.sessions)
                sessions.revoke_all()
            # Revoke every display immediately; cleanup remains bound to its old
            # lease, never another newly logged-in browser. A Docker outage does
            # not undo the durable password change or make old sessions valid.
            for token in tokens:
                try:
                    await cleanup_administrator(token)
                except Exception:
                    logger.warning("Expired private browser cleanup will retry")
            response = JSONResponse({"ok": True, "sessionsRevoked": True})
            response.delete_cookie(COOKIE, path="/", secure=config.cookie_secure, httponly=True, samesite="strict")
            return response

    @app.put("/api/admin/user-password")
    async def change_user_password(body: UserPasswordBody, session=Depends(admin_authenticated)):
        if body.newPassword != body.confirmPassword:
            raise HTTPException(400, "两次输入的新密码不一致")
        async with admin_update_lock:
            if not admin_sessions.get(session.token):
                raise HTTPException(401, "管理员登录已过期，请重新登录")
            async with admin_login_slots:
                matches_admin = await asyncio.to_thread(verify_password, body.newPassword, config.management_password_hash)
                if not admin_sessions.get(session.token):
                    raise HTTPException(401, "管理员登录已过期，请重新登录")
                if matches_admin:
                    raise HTTPException(400, "用户登录密码不能与管理员密码相同")
                encoded = await asyncio.to_thread(hash_password, body.newPassword)
            async with credential_lock:
                if not admin_sessions.get(session.token):
                    raise HTTPException(401, "管理员登录已过期，请重新登录")
                owner = runtime["credentials"].user(runtime["credentials"].legacy_user_id)
                if owner is None:
                    raise HTTPException(404, "没有可重设的旧版默认账号，请先创建用户或在账号列表中选择用户")
                if owner.get("deleting"):
                    raise HTTPException(409, "该用户正在删除，请先完成清理")
                try:
                    runtime["credentials"].replace(encoded)
                except Exception:
                    raise HTTPException(503, "密码保存失败，请检查服务器存储后重试；原密码仍有效") from None
                sessions.revoke_user(runtime["credentials"].legacy_user_id)
        return {"ok": True, "sessionsRevoked": True}

    @app.get("/api/status", dependencies=[Depends(authenticated)])
    async def status():
        result = await runtime["broker"].status()
        auth = await runtime["broker"].shared_auth_status()
        return {**result, "claudeConfigured": bool(auth["authenticated"]),
                **({"claudeAuthenticated": bool(auth["authenticated"]), "mode": "solo"} if solo else {})}

    @app.get("/api/admin/model-policy", dependencies=[Depends(admin_authenticated)])
    async def model_policy():
        return {**runtime["credentials"].model_policy(), "enforcementReady": runtime["chats"].policy_ready}

    @app.put("/api/admin/model-policy")
    async def update_model_policy(body: ModelPolicyBody, session=Depends(admin_authenticated)):
        async with model_policy_lock:
            async with runtime["terminals"].lock:
                if not admin_sessions.get(session.token):
                    raise HTTPException(401, "管理员登录已过期，请重新登录")
                store, chats, broker = runtime["credentials"], runtime["chats"], runtime["broker"]
                allowed = body.fableAllowed
                if store.fable_allowed == allowed and chats.policy_ready:
                    return {**store.model_policy(), "enforcementReady": True}
                if not allowed:
                    # Persist the denial before stopping any task. A crash or
                    # partial Docker failure must never re-enable Fable on restart.
                    try:
                        store.replace_model_policy(False)
                    except Exception:
                        raise HTTPException(503, "模型权限保存失败，原设置未更改；请检查服务器存储") from None
                    broker.fable_allowed = False
                    chats.policy_ready = False
                    chats.policy_changed()
                    async def enforce_denial():
                        failed = False
                        try:
                            affected = await chats.revoke_fable_locked()
                            for workspace in affected:
                                await runtime["login_browsers"].stop(workspace)
                                browser_owners.pop(workspace, None)
                        except BrokerError:
                            failed = True
                        try:
                            await broker.apply_model_policy(False)
                        except BrokerError:
                            failed = True
                        return failed

                    # Once denial is committed, a disconnected/cancelled HTTP
                    # request cannot abandon running Fable tasks halfway through.
                    enforcement = asyncio.create_task(enforce_denial())
                    try:
                        failed = await asyncio.shield(enforcement)
                    except asyncio.CancelledError:
                        await enforcement
                        raise
                    if failed:
                        raise HTTPException(503, "Fable 已禁用，但部分会话或模型权限同步失败；请检查 Docker 后再次保存禁用设置") from None
                    chats.policy_ready = True
                    chats.policy_changed()
                    return {**store.model_policy(), "enforcementReady": True}

                # Grant only after Docker policy is applied. No user launch or
                # model switch can pass the shared lock during the transition.
                chats.policy_ready = False
                chats.policy_changed()
                try:
                    await broker.apply_model_policy(True)
                    if not admin_sessions.get(session.token):
                        raise HTTPException(401, "管理员登录已过期，请重新登录")
                    store.replace_model_policy(True)
                except BaseException as exc:
                    rollback_ok = False
                    try:
                        await broker.apply_model_policy(False)
                        rollback_ok = True
                    except BrokerError:
                        pass
                    broker.fable_allowed = False
                    chats.policy_ready = rollback_ok and not store.fable_allowed
                    chats.policy_changed()
                    if not chats.policy_ready:
                        # An uncertain helper write must not leave a raw CLI
                        # able to use a newly granted model behind a stale denial.
                        try:
                            await chats.revoke_fable_locked()
                        except BrokerError:
                            pass
                    if isinstance(exc, (HTTPException, asyncio.CancelledError)):
                        raise
                    raise HTTPException(503, "Fable 权限未能启用，请检查服务器存储及 Docker 后重试") from None
                chats.policy_ready = True
                chats.policy_changed()
                return {**store.model_policy(), "enforcementReady": True}

    @app.get("/api/translation/status", dependencies=[Depends(authenticated)])
    async def translation_status():
        raise HTTPException(410, "翻译功能已移除，请刷新页面后直接发送消息")

    def enrich(item):
        item["terminalActive"] = runtime["terminals"].active(item["id"])
        item["chatActive"] = runtime["chats"].active(item["id"])
        return item

    @app.get("/api/workspaces")
    async def workspaces(session=Depends(authenticated)):
        user = access.user(session)
        items = await runtime["broker"].list(owner_id=user["id"])
        return {"workspaces": [access.view(enrich(item), session) for item in items]}

    @app.post("/api/workspaces", status_code=201)
    async def create(body: NameBody, session=Depends(authenticated)):
        if solo and body.password:
            raise HTTPException(422, "独享版的工作区无需另设密码")
        async with access.hash_slots:
            encoded = await asyncio.to_thread(hash_password, body.password) if body.password else None
        await reconcile()
        async with runtime["terminals"].lock:
            user = access.user(session)
            if runtime.get("deleting_workspaces"):
                raise HTTPException(503, "管理员正在清理工作区，请稍后刷新")
            runtime["chats"].require_model(None)
            item = await runtime["broker"].create(body.name, owner_id=user["id"], password_hash=encoded, mode=body.mode)
            runtime["usage"].touch(item["id"])
            if encoded:
                session.unlocked_workspaces[item["id"]] = runtime["broker"].workspace_access(item["id"])["passwordRevision"]
            return access.view(enrich(item), session)

    @app.post("/api/workspaces/{workspace_id}/start")
    async def start(workspace_id: str, session=Depends(authenticated)):
        await reconcile()
        async with runtime["terminals"].lock:
            access.check(workspace_id, session)
            runtime["chats"].require_model(None)
            item = await runtime["broker"].start(workspace_id)
            runtime["usage"].touch(workspace_id)
            return access.view(enrich(item), session)

    @app.post("/api/workspaces/{workspace_id}/stop")
    async def stop(workspace_id: str, session=Depends(authenticated)):
        async with runtime["terminals"].lock:
            access.check(workspace_id, session)
            await runtime["login_browsers"].stop(workspace_id)
            browser_owners.pop(workspace_id, None)
            await runtime["chats"].close_locked(workspace_id)
            await runtime["terminals"].stop_locked(workspace_id)
        return access.view(enrich(await runtime["broker"].workspace(workspace_id)), session)

    @app.delete("/api/workspaces/{workspace_id}")
    async def delete(workspace_id: str, session=Depends(authenticated)):
        async with runtime["terminals"].lock:
            access.check(workspace_id, session)
            await runtime["login_browsers"].stop(workspace_id)
            browser_owners.pop(workspace_id, None)
            await runtime["chats"].close_locked(workspace_id)
            await runtime["terminals"].stop_locked(workspace_id)
            await runtime["chats"].forget(workspace_id)
            await runtime["broker"].delete(workspace_id)
            sessions.lock_workspace(workspace_id)
        return {"ok": True}

    @app.get("/api/workspaces/{workspace_id}/files")
    async def list_files(workspace_id: str, path: str = "", session=Depends(authenticated)):
        return await runtime["broker"].files(workspace_id, "list", path, authorize=lambda: access.check(workspace_id, session))

    @app.get("/api/workspaces/{workspace_id}/file")
    async def read_file(workspace_id: str, path: str, session=Depends(authenticated)):
        return await runtime["broker"].files(workspace_id, "read", path, authorize=lambda: access.check(workspace_id, session))

    @app.get("/api/workspaces/{workspace_id}/archive")
    async def download_workspace(workspace_id: str, request: Request, session=Depends(authenticated)):
        authorize = lambda: access.check(workspace_id, session)
        artifact = await prepare_connected(request, lambda: runtime["broker"].archive(workspace_id, authorize=authorize))
        try:
            authorize()
            name = runtime["broker"].get(workspace_id)["name"]
            return ArchiveResponse(artifact, name, workspace_id, authorize)
        except BaseException:
            artifact.close()
            raise

    @app.put("/api/workspaces/{workspace_id}/file")
    async def write_file(workspace_id: str, body: FileBody, session=Depends(authenticated)):
        return await runtime["broker"].files(workspace_id, "write", body.path, content=body.content, encoding=body.encoding,
                                             authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/attachments", status_code=201)
    async def upload_attachment(workspace_id: str, body: UploadBody, session=Depends(authenticated)):
        return await runtime["broker"].files(workspace_id, "upload", "", name=body.name, content=body.content,
                                             authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/directory")
    async def mkdir(workspace_id: str, body: PathBody, session=Depends(authenticated)):
        return await runtime["broker"].files(workspace_id, "mkdir", body.path, authorize=lambda: access.check(workspace_id, session))

    @app.delete("/api/workspaces/{workspace_id}/file")
    async def delete_file(workspace_id: str, path: str, session=Depends(authenticated)):
        return await runtime["broker"].files(workspace_id, "delete", path, authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/terminal/stop", dependencies=[Depends(authenticated)])
    async def stop_terminal(workspace_id: str):
        raise HTTPException(403, "Claude 登录仅由管理员管理")

    @app.get("/api/workspaces/{workspace_id}/chat", dependencies=[Depends(authenticated)])
    async def chat_state(workspace_id: str):
        return runtime["chats"].get(workspace_id).snapshot()

    @app.put("/api/workspaces/{workspace_id}/chat/model")
    async def chat_model(workspace_id: str, body: ChatModelBody, session=Depends(authenticated)):
        return await runtime["chats"].set_model(workspace_id, body.model, authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/chat/message", status_code=202)
    async def chat_message(workspace_id: str, body: ChatBody, session=Depends(authenticated)):
        await reconcile()
        return await runtime["chats"].send(workspace_id, body.text,
                                           [item.model_dump() for item in body.attachments],
                                           authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/chat/preview", dependencies=[Depends(authenticated)])
    async def chat_preview(workspace_id: str):
        raise HTTPException(410, "翻译功能已移除，请刷新页面后直接发送消息")

    @app.post("/api/workspaces/{workspace_id}/chat/back-translate", dependencies=[Depends(authenticated)])
    async def chat_back_translate(workspace_id: str):
        raise HTTPException(410, "翻译功能已移除，请刷新页面后直接发送消息")

    @app.post("/api/workspaces/{workspace_id}/chat/permissions/{request_id}")
    async def chat_permission(workspace_id: str, request_id: str, body: PermissionBody, session=Depends(authenticated)):
        return await runtime["chats"].permission(workspace_id, request_id, body.allow,
                                                authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/chat/stop")
    async def chat_stop(workspace_id: str, session=Depends(authenticated)):
        return await runtime["chats"].stop(workspace_id, authorize=lambda: access.check(workspace_id, session))

    @app.post("/api/workspaces/{workspace_id}/chat/messages/{message_id}/translate", status_code=202, dependencies=[Depends(authenticated)])
    async def chat_retry(workspace_id: str, message_id: str):
        raise HTTPException(410, "翻译功能已移除，请查看已有回复原文")

    # Removed user-facing login transports stay explicitly forbidden, including
    # old bookmarked websocket URLs from previous versions.
    @app.api_route("/api/workspaces/{workspace_id}/login-browser", methods=["GET", "POST"], dependencies=[Depends(authenticated)])
    @app.post("/api/workspaces/{workspace_id}/login-browser/stop", dependencies=[Depends(authenticated)])
    async def forbidden_login(workspace_id: str):
        raise HTTPException(403, "Claude 登录仅由管理员管理")

    @app.websocket("/api/workspaces/{workspace_id}/terminal")
    @app.websocket("/api/workspaces/{workspace_id}/login-browser/display")
    async def forbidden_login_socket(ws: WebSocket, workspace_id: str):
        await ws.close(code=4403)

    install_account_browser_routes(app, config, runtime, admin_sessions, admin_authenticated, admin_cookie)
    install_admin_claude_routes(app, config, runtime, admin_sessions, admin_authenticated, admin_cookie, reconcile)
    install_admin_claude_routes(app, config, runtime, admin_sessions, admin_authenticated, admin_cookie, reconcile,
                               prefix="/api/admin/usage-auth", manager_key="admin_usage_auth", factory=AdminUsageAuth)
    install_usage_auth_routes(app, runtime, admin_sessions, admin_authenticated)
    install_workspace_routes(app, runtime, sessions, admin_sessions, authenticated, admin_authenticated,
                             access, enrich, config, credential_lock, admin_update_lock, admin_login_slots)
    install_usage_routes(app, runtime, authenticated, admin_authenticated, admin_sessions, access)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def unknown(path: str):
        raise HTTPException(404, "接口不存在")

    static_dir = Path(os.getenv("STATIC_DIR", str(Path(__file__).resolve().parent.parent / "frontend" / "dist")))
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")
    else:
        @app.get("/")
        async def missing_frontend():
            return Response("Frontend is not built. Run npm ci && npm run build in frontend/.", status_code=503)
    return app


app = create_app()
