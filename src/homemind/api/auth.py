"""认证接口：登录 / 当前身份 / 登出。

对外只暴露三个动作，但每条都有明确的失败语义：
    POST /api/auth/login   账号密码 → token（失败一律 401，不区分账号是否存在）
    GET  /api/auth/me      当前身份（token 无效即 401）
    POST /api/auth/logout  登出（服务端留一次审计，token 本身无状态，由前端丢弃）
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth.deps import current_user
from ..auth.store import AccountStore, verify_login
from ..auth.tokens import AuthUser, issue_token
from ..core.trace import get_logger

logger = get_logger("api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_store: AccountStore | None = None


def get_account_store() -> AccountStore:
    """账号库单例（测试可用 set_account_store 换成临时库）。"""
    global _store
    if _store is None:
        _store = AccountStore()
    return _store


def set_account_store(store: AccountStore | None) -> None:
    global _store
    _store = store


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


@router.post("/login")
async def login(req: LoginRequest) -> dict[str, Any]:
    """登录。失败时永远返回同一句话——不告诉对方"这个用户名不存在"。"""
    store = get_account_store()
    account = verify_login(store, req.username, req.password)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    user = AuthUser(
        family_id=account.family_id,
        member_id=account.member_id,
        role=account.role,
        name=account.name,
        city=account.city,
    )
    token, expires_in = issue_token(user)

    # 登录本身就是一条真实事件：刚进家门时消息中心不该是空的。
    # 早期的种子消息是编的，现在第一条是真的——"谁在几点进了哪个家"。
    from .events import TYPE_SYSTEM, record_event

    record_event(
        user.family_id, TYPE_SYSTEM,
        title=f"{user.name} 登录了控制台",
        body=f"身份：{user.role} · 城市：{user.city} · 家庭：{user.family_id}",
        actor=user.name,
    )
    return {
        "token": token,
        "tokenType": "Bearer",
        "expiresIn": expires_in,
        "user": account.to_public_dict(),
    }


@router.get("/me")
async def me(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """当前登录身份（前端刷新后用它恢复登录态）。"""
    return {"user": user.to_dict()}


class RegisterRequest(BaseModel):
    """注册请求。mode=create 建新家，mode=join 凭邀请码加入。"""

    username: str = Field(..., description="用户名（3-20 位字母数字下划线）")
    password: str = Field(..., description="密码（至少 6 位）")
    name: str = Field(..., description="昵称，家里其他人看到的名字")
    mode: str = Field(default="create", description="create / join")
    family_name: str = Field(default="", description="新建家庭时的家庭名")
    city: str = Field(default="", description="城市（仅创建家庭时用）")
    invite_code: str = Field(default="", description="加入家庭时的邀请码")


# 注册失败码 → HTTP 状态。409 专给"重名"：这是唯一一个"换个别的方式重试就能成"
# 的错误，前端据此把光标停在用户名输入框上，而不是弹一句通用报错。
_REGISTER_STATUS = {
    "username_taken": status.HTTP_409_CONFLICT,
}


@router.post("/register")
async def register(req: RegisterRequest) -> dict[str, Any]:
    """注册并直接登录。

    注册成功就发 token，不让用户"注册完再登录一次"——那一步纯属折磨。
    """
    from ..auth.store import RegistrationError
    from ..tools.device_server import ensure_family

    store = get_account_store()
    try:
        account = store.register(
            username=req.username, password=req.password, name=req.name,
            mode=req.mode, family_name=req.family_name, city=req.city,
            invite_code=req.invite_code,
        )
    except RegistrationError as exc:
        raise HTTPException(
            status_code=_REGISTER_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=exc.message,
        ) from exc

    # 新家庭必须有设备，否则用户登进去看见一个空控制台，
    # 还以为是自己点错了。幂等，重复调用无害。
    if req.mode == "create":
        ensure_family(account.family_id)

    user = AuthUser(
        family_id=account.family_id,
        member_id=account.member_id,
        role=account.role,
        name=account.name,
        city=account.city,
    )
    token, expires_in = issue_token(user)

    from .events import TYPE_SYSTEM, record_event

    family_name = account.to_public_dict()["family"].get("name", account.family_id)
    record_event(
        user.family_id, TYPE_SYSTEM,
        title=(f"{user.name} 创建了「{family_name}」" if req.mode == "create"
               else f"{user.name} 加入了「{family_name}」"),
        body=(f"身份：{user.role} · 城市：{user.city or '未填写'}" if req.mode == "create"
              else f"身份：{user.role} · 由邀请码加入"),
        actor=user.name,
    )
    return {
        "token": token,
        "tokenType": "Bearer",
        "expiresIn": expires_in,
        "user": account.to_public_dict(),
    }


@router.get("/family/invite")
async def family_invite(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """查看本家邀请码，用于拉家人进来。

    只有户主（L4）能看。邀请码等于"进我家"的钥匙，普通成员随手一瞥就能把
    邻居拉进来，那这套权限分级就是装饰品。
    """
    from ..auth.family_store import get_family

    if user.role != "L4":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有户主可以查看邀请码",
        )
    family = get_family(user.family_id)
    if family is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="家庭不存在")
    return {
        "inviteCode": family.invite_code,
        "familyName": family.name,
        "familyId": family.family_id,
    }


@router.post("/logout")
async def logout(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """登出：清掉该成员的控制台会话记忆，再留一次审计。

    JWT 无状态，真正的失效靠前端丢弃 token；但短期记忆里还留着
    这个人的对话，换账号前必须清掉，否则下个登录者可能被问出上一家的事。
    """
    from ..memory.short_term import get_short_term_memory

    await get_short_term_memory().clear(f"console:{user.family_id}:{user.member_id}")
    logger.info("logout: %s", user.member_id,
                extra={"extra_fields": {"event": "auth.logout",
                                        "family_id": user.family_id,
                                        "member_id": user.member_id}})
    return {"ok": True}


@router.get("/accounts")
async def list_accounts() -> dict[str, Any]:
    """演示账号清单（登录页一键填充用）。生产环境关闭。"""
    if os.getenv("HM_ENV", "development") == "production":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return {"accounts": get_account_store().list_accounts()}
