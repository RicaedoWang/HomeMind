"""JWT 签发与校验（HS256）。

密钥来源优先级：
1. `HM_AUTH_SECRET` 环境变量（生产唯一推荐）
2. `data/.auth_secret` 文件——开发环境首次启动时自动生成并持久化
   （`data/` 已在 .gitignore，不会误提交）

为什么开发要持久化而不是每次随机：随机的话一重启服务，
所有人登录态全废，演示到一半要重新登录，体验很差。
生产环境没有配置密钥则**直接报错**，绝不静默用一个弱默认值。
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import jwt

from ..core.trace import get_logger

logger = get_logger("auth.tokens")

ALGORITHM = "HS256"
_SECRET_FILE = Path("data/.auth_secret")


class TokenError(Exception):
    """token 无效 / 过期 / 被篡改。对外统一不区分原因，避免信息泄露。"""


@dataclass(frozen=True)
class AuthUser:
    """从 token 解出的身份。业务层只认这个对象。"""

    family_id: str
    member_id: str
    role: str          # L1 访客 ~ L4 管理员
    name: str
    city: str

    def to_dict(self) -> dict[str, str]:
        return {
            "family_id": self.family_id,
            "member_id": self.member_id,
            "role": self.role,
            "name": self.name,
            "city": self.city,
        }


def _resolve_secret() -> str:
    env_secret = os.getenv("HM_AUTH_SECRET")
    if env_secret:
        return env_secret

    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_text(encoding="utf-8").strip()

    if os.getenv("HM_ENV", "development") == "production":
        raise RuntimeError("生产环境必须显式配置 HM_AUTH_SECRET，不允许自动生成")

    secret = secrets.token_hex(32)
    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SECRET_FILE.write_text(secret, encoding="utf-8")
    logger.warning("no HM_AUTH_SECRET found, generated one at %s", _SECRET_FILE)
    return secret


def issue_token(user: AuthUser, ttl_hours: float | None = None) -> tuple[str, int]:
    """签发 token，返回 (token, 有效期秒数)。ttl 可为负——测试过期用。"""
    if ttl_hours is None:
        ttl_hours = float(os.getenv("HM_AUTH_TTL_HOURS", "8"))
    expires_in = int(ttl_hours * 3600)
    now = int(time.time())
    payload = {
        **user.to_dict(),
        "iat": now,
        "exp": now + expires_in,
    }
    token = jwt.encode(payload, _resolve_secret(), algorithm=ALGORITHM)
    return token, expires_in


def decode_token(token: str) -> AuthUser:
    """校验并解析 token。任何问题都抛 TokenError（对外只说"无效"）。"""
    try:
        payload = jwt.decode(token, _resolve_secret(), algorithms=[ALGORITHM])
        return AuthUser(
            family_id=payload["family_id"],
            member_id=payload["member_id"],
            role=payload["role"],
            name=payload["name"],
            city=payload["city"],
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    except KeyError as exc:
        raise TokenError(f"missing claim: {exc}") from exc
