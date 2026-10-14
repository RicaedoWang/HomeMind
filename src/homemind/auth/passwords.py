"""密码哈希：PBKDF2-SHA256 + 随机盐（标准库实现，零新依赖）。

为什么不用 bcrypt / argon2：
那两个更抗 GPU 暴力破解，但要装外部包。本项目是单机演示，
PBKDF2 200k 轮已远超"演示"所需强度；真上生产换 argon2 只是一行的事，
代码里已注明，别在面试时把 PBKDF2 吹成 bcrypt。

存储格式：`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`
自带迭代次数，将来调高强度时旧密码仍可校验，不用强制所有人改密码。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """生成可存储的密码串（含盐，同一密码两次结果不同）。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。任何格式异常都判失败，绝不抛异常给调用方。"""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        # 定长比对：避免通过响应时间差暴力试探
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False
