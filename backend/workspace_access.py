"""Account ownership and optional workspace locks, independent of Docker paths."""
import asyncio

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import Sessions, hash_password, verify_password
from .credentials import normalize_username
from .docker_backend import BrokerError


class AccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str = Field(min_length=12, max_length=1024)
    confirmPassword: str = Field(min_length=12, max_length=1024)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        return normalize_username(value)


class AccountPasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    newPassword: str = Field(min_length=12, max_length=1024)
    confirmPassword: str = Field(min_length=12, max_length=1024)


class UnlockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class WorkspacePasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currentPassword: str = Field(default="", max_length=1024)
    newPassword: str = Field(default="", max_length=1024)
    confirmPassword: str = Field(default="", max_length=1024)


class DeleteAllBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


class DeleteUsersBody(DeleteAllBody):
    userIds: list[str] = Field(min_length=1, max_length=100)

    @field_validator("userIds")
    @classmethod
    def unique_ids(cls, values):
        if len(set(values)) != len(values) or any(
                len(value) != 32 or any(char not in "0123456789abcdef" for char in value)
                for value in values):
            raise ValueError("请选择不重复的用户账号")
        return values


class WorkspaceAccess:
    def __init__(self, runtime, sessions):
        self.runtime, self.sessions = runtime, sessions
        self.attempts = Sessions()
        self.hash_slots = asyncio.Semaphore(2)

    def user(self, session):
        if not session or self.sessions.get(session.token) is not session:
            raise HTTPException(401, "请重新登录")
        user = self.runtime["credentials"].user(session.user_id)
        if user is None or user.get("deleting"):
            raise HTTPException(401, "请重新登录")
        return user

    def check(self, workspace_id, session, unlocked=True):
        user = self.user(session)
        if self.runtime.get("deleting_workspaces"):
            raise HTTPException(503, "管理员正在清理工作区，请稍后刷新")
        record = self.runtime["broker"].workspace_access(workspace_id)
        if record["ownerId"] != user["id"]:
            # Unknown and another user's IDs have the same externally visible error.
            raise HTTPException(404, "工作区不存在")
        if (unlocked and record["passwordHash"]
                and session.unlocked_workspaces.get(workspace_id) != record["passwordRevision"]):
            raise HTTPException(423, "工作区已锁定，请输入工作区密码")
        return record

    def view(self, item, session):
        record = self.check(item["id"], session, unlocked=False)
        result = {**item, "passwordProtected": bool(record["passwordHash"]),
                  "locked": bool(record["passwordHash"] and
                                 session.unlocked_workspaces.get(item["id"]) != record["passwordRevision"])}
        if result["locked"]:
            result.pop("chatActive", None)
            result.pop("terminalActive", None)
        return result

    def rate_limit(self, session, workspace_id):
        if not self.attempts.allow_login(f"{session.user_id}:{workspace_id}"):
            raise HTTPException(429, "工作区密码尝试过多，请 5 分钟后重试")


def install_routes(app, runtime, sessions, admin_sessions, authenticated, admin_authenticated,
                   access, enrich, config, credential_lock, admin_update_lock, admin_hash_slots):
    @app.post("/api/workspaces/{workspace_id}/unlock")
    async def unlock(workspace_id: str, body: UnlockBody, session=Depends(authenticated)):
        record = access.check(workspace_id, session, unlocked=False)
        if record["passwordHash"]:
            access.rate_limit(session, workspace_id)
            async with access.hash_slots:
                valid = await asyncio.to_thread(verify_password, body.password, record["passwordHash"])
            current = access.check(workspace_id, session, unlocked=False)
            if not valid or current["passwordRevision"] != record["passwordRevision"]:
                raise HTTPException(403, "工作区密码不正确或已更新")
            session.unlocked_workspaces[workspace_id] = current["passwordRevision"]
        return access.view(enrich(await runtime["broker"].workspace(workspace_id)), session)

    @app.post("/api/workspaces/{workspace_id}/lock")
    async def lock(workspace_id: str, session=Depends(authenticated)):
        access.check(workspace_id, session, unlocked=False)
        session.unlocked_workspaces.pop(workspace_id, None)
        return access.view(enrich(await runtime["broker"].workspace(workspace_id)), session)

    @app.put("/api/workspaces/{workspace_id}/password")
    async def password(workspace_id: str, body: WorkspacePasswordBody, session=Depends(authenticated)):
        record = access.check(workspace_id, session, unlocked=False)
        if body.newPassword != body.confirmPassword:
            raise HTTPException(400, "两次输入的工作区密码不一致")
        if body.newPassword and len(body.newPassword) < 8:
            raise HTTPException(400, "工作区密码至少需要 8 个字符")
        access.rate_limit(session, workspace_id)
        async with access.hash_slots:
            if record["passwordHash"] and not await asyncio.to_thread(verify_password, body.currentPassword, record["passwordHash"]):
                raise HTTPException(403, "当前工作区密码不正确")
            encoded = await asyncio.to_thread(hash_password, body.newPassword) if body.newPassword else None
        async with runtime["terminals"].lock:
            current = access.check(workspace_id, session, unlocked=False)
            if current["passwordRevision"] != record["passwordRevision"]:
                raise HTTPException(409, "工作区密码已更新，请刷新后重试")
            revision = runtime["broker"].set_workspace_password(workspace_id, encoded)
            sessions.lock_workspace(workspace_id)
            if encoded:
                session.unlocked_workspaces[workspace_id] = revision
        return access.view(enrich(await runtime["broker"].workspace(workspace_id)), session)

    def still_admin(session):
        if admin_sessions.get(session.token) is not session:
            raise HTTPException(401, "管理员登录已过期，请重新登录")

    async def encode_user_password(password, session):
        async with admin_hash_slots:
            if await asyncio.to_thread(verify_password, password, config.management_password_hash):
                raise HTTPException(400, "用户登录密码不能与管理员密码相同")
            encoded = await asyncio.to_thread(hash_password, password)
        still_admin(session)
        return encoded

    @app.get("/api/admin/users", dependencies=[Depends(admin_authenticated)])
    async def users():
        return {"users": runtime["credentials"].users()}

    @app.post("/api/admin/users", status_code=201)
    async def create_user(body: AccountBody, session=Depends(admin_authenticated)):
        if body.password != body.confirmPassword:
            raise HTTPException(400, "两次输入的密码不一致")
        async with admin_update_lock:
            still_admin(session)
            encoded = await encode_user_password(body.password, session)
            async with credential_lock:
                still_admin(session)
                try:
                    user = runtime["credentials"].create_user(body.username, encoded)
                except ValueError:
                    raise HTTPException(409, "用户名已存在或不符合要求") from None
                except Exception:
                    raise HTTPException(503, "账号保存失败，请检查服务器存储后重试") from None
        return {"user": user}

    @app.put("/api/admin/users/{user_id}/password")
    async def reset_user(user_id: str, body: AccountPasswordBody, session=Depends(admin_authenticated)):
        if body.newPassword != body.confirmPassword:
            raise HTTPException(400, "两次输入的密码不一致")
        async with admin_update_lock:
            still_admin(session)
            if runtime["credentials"].user(user_id) is None:
                raise HTTPException(404, "用户不存在")
            encoded = await encode_user_password(body.newPassword, session)
            async with credential_lock:
                still_admin(session)
                current = runtime["credentials"].user(user_id)
                if current is None:
                    raise HTTPException(404, "用户不存在")
                if current.get("deleting"):
                    raise HTTPException(409, "该用户正在删除，请先完成清理")
                try:
                    runtime["credentials"].replace_user_password(user_id, encoded)
                except Exception:
                    raise HTTPException(503, "密码保存失败，请检查服务器存储后重试") from None
                sessions.revoke_user(user_id)
        return {"ok": True, "sessionsRevoked": True}

    @app.get("/api/admin/workspaces", dependencies=[Depends(admin_authenticated)])
    async def all_workspaces():
        usernames = {item["id"]: item["username"] for item in runtime["credentials"].users()}
        return {"workspaces": [{**item, "username": usernames.get(item.get("ownerId"), "未知用户")}
                               for item in await runtime["broker"].list()]}

    def require_idle_cleanup():
        task = runtime.get("bulk_delete_task")
        if runtime.get("deleting_workspaces") or (task is not None and not task.done()):
            raise HTTPException(409, "已有清理任务正在运行，请等待完成")

    async def remove_workspace(item):
        workspace_id = item["id"]
        stopped = True
        # A failed browser shutdown must not leave the Claude task untouched.
        for stop in (runtime["login_browsers"].stop, runtime["chats"].close_locked,
                     runtime["terminals"].stop_locked):
            try:
                await stop(workspace_id)
            except Exception:
                stopped = False
        if not stopped:
            raise BrokerError("工作区未能完整停止，请重试清理", 503)
        # Keep the registered workspace until both history and Docker data are
        # gone, so a partial volume removal can be safely retried after restart.
        await runtime["chats"].forget(workspace_id)
        await runtime["broker"].delete(workspace_id)
        sessions.lock_workspace(workspace_id)

    async def cleanup_user(user):
        result = {"deleted": False, "userId": user["id"], "username": user["username"],
                  "deletedWorkspaces": 0, "failed": [], "remaining": None}
        try:
            items = await runtime["broker"].list(owner_id=user["id"])
            for item in items:
                try:
                    await remove_workspace(item)
                    result["deletedWorkspaces"] += 1
                except Exception:
                    result["failed"].append({"id": item["id"], "name": item["name"],
                                             "error": "删除未完成，请检查 Docker 与存储后重试"})
            result["remaining"] = len(await runtime["broker"].list(owner_id=user["id"]))
            if not result["failed"] and result["remaining"] == 0:
                async with credential_lock:
                    if runtime.get("shares"):
                        runtime["shares"].delete_user(user["id"])
                    runtime["quotas"].delete_user(user["id"])
                    runtime["credentials"].delete_user(user["id"])
                    result["deleted"] = True
        except Exception:
            result["failed"].append({"id": user["id"], "name": "账号与数据清理",
                                     "error": "清理未完成，账号已禁用，请检查服务器后重试"})
        return result

    async def cleanup_users(users):
        # The same lock as workspace create/start closes the admission race:
        # any already-started create completes before we enumerate its owner.
        async with runtime["terminals"].lock:
            results = [await cleanup_user(user) for user in users]
        deleted = sum(item["deleted"] for item in results)
        return {"results": results, "deletedUsers": deleted, "failedUsers": len(results) - deleted}

    async def begin_cleanup(user_ids, session):
        async with credential_lock:
            still_admin(session)
            require_idle_cleanup()
            if any(runtime["credentials"].user(user_id) is None for user_id in user_ids):
                raise HTTPException(404, "用户不存在，请刷新账号列表")
            try:
                users = runtime["credentials"].begin_user_deletions(user_ids)
            except Exception:
                raise HTTPException(503, "无法保存清理状态，未开始删除，请检查服务器存储后重试") from None
            for user in users:
                sessions.revoke_user(user["id"])
            # Persist the denial first; a disconnect cannot re-enable the user
            # or cancel committed cleanup. Shutdown waits for this same task.
            task = asyncio.create_task(cleanup_users(users))
            runtime["bulk_delete_task"] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @app.post("/api/admin/users/delete-batch")
    async def delete_users(body: DeleteUsersBody, session=Depends(admin_authenticated)):
        if body.confirmation != "DELETE_USERS_AND_DATA":
            raise HTTPException(400, "请先确认删除所选用户及全部数据")
        return await begin_cleanup(body.userIds, session)

    @app.post("/api/admin/users/{user_id}/delete")
    async def delete_user(user_id: str, body: DeleteAllBody, session=Depends(admin_authenticated)):
        if body.confirmation != "DELETE_USER_AND_DATA":
            raise HTTPException(400, "请先确认删除该用户及全部数据")
        result = await begin_cleanup([user_id], session)
        return result["results"][0]

    async def clear_all():
        deleted, failed = 0, []
        try:
            async with runtime["terminals"].lock:
                items = await runtime["broker"].list()
                for item in items:
                    workspace_id = item["id"]
                    try:
                        await remove_workspace(item)
                        deleted += 1
                    except Exception:
                        failed.append({"id": workspace_id, "name": item["name"],
                                       "error": "删除未完成，请检查 Docker 与存储后重试"})
                return {"deleted": deleted, "failed": failed, "remaining": len(await runtime["broker"].list())}
        finally:
            runtime["deleting_workspaces"] = False

    @app.post("/api/admin/workspaces/delete-all")
    async def delete_all(body: DeleteAllBody, session=Depends(admin_authenticated)):
        if body.confirmation != "DELETE_ALL_WORKSPACES":
            raise HTTPException(400, "请先确认删除全部工作区")
        require_idle_cleanup()
        still_admin(session)
        runtime["deleting_workspaces"] = True
        task = asyncio.create_task(clear_all())
        runtime["bulk_delete_task"] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
