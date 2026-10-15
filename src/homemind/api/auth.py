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
