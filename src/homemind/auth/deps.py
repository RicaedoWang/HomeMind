"""FastAPI 身份依赖：把 token 变成业务层能用的 AuthUser。

用法：任意路由加一个参数 `user: AuthUser = Depends(current_user)` 即受保护。
测试里用 `app.dependency_overrides[current_user] = lambda: <AuthUser>` 旁路，
这样"测试必须确定性、不联网、不烧钱"这条原则不会被认证破坏。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .tokens import AuthUser, TokenError, decode_token

bearer_scheme = HTTPBearer(auto_error=False, description="Authorization: Bearer <token>")

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="需要登录",
    headers={"WWW-Authenticate": "Bearer"},
)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthUser:
    """解析 Bearer token。缺 token / 被篡改 / 过期，对外都只说"需要登录"。"""
    if credentials is None or not credentials.credentials:
        raise _UNAUTHORIZED
    try:
        return decode_token(credentials.credentials)
    except TokenError:
        # 不区分"签名错"还是"过期"：少给攻击者一点信息没坏处
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录态无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
