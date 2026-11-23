"""Authenticated quota management; account summaries never expose credentials."""
import asyncio

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .docker_backend import BrokerError
from .quotas import USER_ID

MAX_QUOTA = 10**12


class QuotaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fiveHourLimit: StrictInt | None = Field(ge=0, le=MAX_QUOTA)
    weeklyLimit: StrictInt | None = Field(ge=0, le=MAX_QUOTA)


class DistributionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fiveHourTotal: StrictInt | None = Field(ge=0, le=MAX_QUOTA)
    weeklyTotal: StrictInt | None = Field(ge=0, le=MAX_QUOTA)
    confirmation: str


class AutomaticBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    userIds: list[StrictStr] = Field(max_length=1000)
    confirmation: str


def install_routes(app, runtime, authenticated, admin_authenticated, admin_sessions, access):
    def account(session):
        user = access.user(session)
        control = runtime["usage"]
        return {"quota": None if runtime.get("solo") else control.quota(user_id=user["id"]), "official": control.official.snapshot(),
                **({"mode": "solo"} if runtime.get("solo") else {}),
                "activeTasks": control.tasks(user["id"]), "idleMinutes": 30}

    def admin_view():
        control = runtime["usage"]
        return {"users": [{**item, "quota": control.quota(user_id=item["id"])}
                          for item in runtime["credentials"].users()], "official": control.official.snapshot(),
                "settings": runtime["shares"].config()}

    def require_manual():
        if runtime["shares"].config()["mode"] == "official_share":
            raise HTTPException(409, "已启用自动配额；请先关闭自动分配，再修改 Token 配额")

    def still_admin(session):
        if admin_sessions.get(session.token) is not session:
            raise HTTPException(401, "管理员登录已过期")

    async def enforce_limits():
        # Caller holds the shared lifecycle lock; continue even if HTTP leaves.
        chats = runtime["chats"]
        for session in list(chats.items.values()):
            if runtime["usage"].should_pause(session):
                await chats.pause_quota_locked(session)

    async def shield_enforcement():
        task = asyncio.create_task(enforce_limits())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @app.get("/api/account/usage")
    async def usage(session=Depends(authenticated)):
        return account(session)

    @app.post("/api/account/usage/refresh")
    async def refresh_usage(session=Depends(authenticated)):
        await runtime["usage"].refresh()
        return account(session)

    @app.post("/api/workspaces/{workspace_id}/activity")
    async def activity(workspace_id: str, session=Depends(authenticated)):
        access.check(workspace_id, session)
        runtime["usage"].touch(workspace_id)
        return {"ok": True, "idleMinutes": 30}

    @app.post("/api/workspaces/{workspace_id}/chat/resume")
    async def resume(workspace_id: str, session=Depends(authenticated)):
        return await runtime["chats"].resume_quota(workspace_id, authorize=lambda: access.check(workspace_id, session))

    @app.get("/api/admin/quotas", dependencies=[Depends(admin_authenticated)])
    async def quotas():
        return admin_view()

    @app.post("/api/admin/quotas/refresh")
    async def refresh_admin_quotas(session=Depends(admin_authenticated)):
        await runtime["usage"].refresh()
        still_admin(session)
        return admin_view()

    @app.put("/api/admin/users/{user_id}/quota")
    async def set_quota(user_id: str, body: QuotaBody, session=Depends(admin_authenticated)):
        async with runtime["terminals"].lock:
            still_admin(session)
            require_manual()
            user = runtime["credentials"].user(user_id)
            if not user or user.get("deleting"):
                raise HTTPException(404, "用户不存在")
            runtime["usage"].quotas.set_limits(user_id, body.fiveHourLimit, body.weeklyLimit)
            await shield_enforcement()
            return admin_view()

    @app.post("/api/admin/quotas/distribute")
    async def distribute(body: DistributionBody, session=Depends(admin_authenticated)):
        if body.confirmation != "DISTRIBUTE_QUOTA":
            raise HTTPException(400, "请先确认平均分配")
        async with runtime["terminals"].lock:
            still_admin(session)
            require_manual()
            ids = [item["id"] for item in runtime["credentials"].users() if not item.get("deleting")]
            if not ids:
                raise HTTPException(409, "还没有可分配的用户")
            runtime["usage"].quotas.distribute(ids, body.fiveHourTotal, body.weeklyTotal)
            await shield_enforcement()
            return admin_view()

    @app.put("/api/admin/quotas/automatic")
    async def automatic(body: AutomaticBody, session=Depends(admin_authenticated)):
        if body.confirmation != "CHANGE_AUTOMATIC_QUOTA":
            raise HTTPException(400, "请先确认自动配额设置")
        if len(set(body.userIds)) != len(body.userIds) or any(not USER_ID.fullmatch(uid) for uid in body.userIds):
            raise HTTPException(400, "请选择有效且不重复的用户")
        if body.enabled and not body.userIds:
            raise HTTPException(400, "请至少选择一位参与分配的用户")
        async with runtime["terminals"].lock:
            still_admin(session)
            if any(runtime["usage"].task_running(item) for item in runtime["chats"].items.values()):
                raise HTTPException(409, "请先停止所有正在运行或等待授权的 Claude 任务，再修改自动配额")
            if runtime["chats"].quota_stop_pending:
                raise HTTPException(409, "正在确认任务已停止，请稍后重试")
            for user_id in body.userIds:
                user = runtime["credentials"].user(user_id)
                if not user or user.get("deleting"):
                    raise HTTPException(409, "用户列表已变化，请刷新后重新选择")
            try:
                runtime["shares"].configure(body.userIds if body.enabled else None, enabled=body.enabled,
                                            official_snapshot=runtime["official"].snapshot())
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return admin_view()
