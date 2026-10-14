"""账号存储与登录校验。

为什么账号单独一个库 `data/homemind_accounts.db`，不塞进记忆库：
记忆库里是你真实的偏好数据，认证属于"可能反复改结构"的模块，
混在一张库里迟早要迁移，迁移就有弄脏真实数据的风险。物理隔开 = 风险归零。

四个演示账号覆盖两个家庭（深圳家 / 青岛家），S3 会给两个家灌不同的设备与任务。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.trace import get_logger
from .passwords import hash_password, verify_password

logger = get_logger("auth.store")

DEFAULT_DB_PATH = "data/homemind_accounts.db"

# 家庭（租户）。city 是演示用字段，不做真实 GeoIP 定位。
FAMILIES: dict[str, dict[str, Any]] = {
    "family_sz": {"family_id": "family_sz", "name": "深圳的家", "city": "深圳", "home_id": "home_sz"},
    "family_qd": {"family_id": "family_qd", "name": "青岛的家", "city": "青岛", "home_id": "home_qd"},
}

# 演示密码：四个账号统一，方便当场演示（登录页会提示）
DEMO_PASSWORD = "demo1234"

# (username, family_id, member_id, role, name)
SEED_ACCOUNTS: list[tuple[str, str, str, str, str]] = [
    ("wo", "family_sz", "member_001", "L4", "我"),
    ("mama", "family_sz", "member_002", "L3", "妈妈"),
    ("baba", "family_sz", "member_003", "L3", "爸爸"),
    ("xiaoming", "family_qd", "member_101", "L4", "小明"),
]

# 登录失败限流：同一用户名 60 秒内最多 5 次，超了锁 60 秒
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 60


@dataclass
class Account:
    """一个登录账号。"""

    user_id: str
    family_id: str
    member_id: str
    username: str
    role: str
    name: str
    city: str

    def to_public_dict(self) -> dict[str, Any]:
        """对外可见的信息——永远不含密码哈希。"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "member_id": self.member_id,
            "name": self.name,
            "role": self.role,
            "city": self.city,
            "family": FAMILIES.get(self.family_id, {"family_id": self.family_id}),
        }


class AccountStore:
    """账号表（SQLite）。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._attempts: dict[str, list[float]] = {}
        self._init_schema()

    def _connect(self) -> Any:
        """统一连接入口：必须设 row_factory，否则取到的是元组不是字典。

        踩过的坑：`get_password_hash` 单独 connect 时漏了这一行，
        结果 `row["password_hash"]` 报 TypeError——所有连接都从这里出，杜绝复发。
        """
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_account (
                    user_id       TEXT PRIMARY KEY,
                    family_id     TEXT NOT NULL,
                    member_id     TEXT NOT NULL,
                    username      TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'L3',
                    name          TEXT NOT NULL DEFAULT '',
                    city          TEXT NOT NULL DEFAULT '',
                    created_at    REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_family ON user_account(family_id)")
        self._seed()

    def _seed(self) -> None:
        """写入演示账号。已存在则跳过——不覆盖用户后来改过的密码。"""
        now = time.time()
        with self._connect() as conn:
            for username, family_id, member_id, role, name in SEED_ACCOUNTS:
                exists = conn.execute(
                    "SELECT 1 FROM user_account WHERE username=?", (username,)
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO user_account
                       (user_id, family_id, member_id, username, password_hash,
                        role, name, city, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f"user_{username}", family_id, member_id, username,
                     hash_password(DEMO_PASSWORD), role, name,
                     FAMILIES[family_id]["city"], now),
                )

    def get_by_username(self, username: str) -> Account | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_account WHERE username=?", (username,)
            ).fetchone()
        return self._row_to_account(row) if row else None

    def get_password_hash(self, username: str) -> str | None:
        """取密码哈希。用户不存在返回 None——调用方不得据此区分错误原因。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM user_account WHERE username=?", (username,)
            ).fetchone()
        return row["password_hash"] if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        """列出全部账号（不含哈希），给前端登录页做一键填充。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_account ORDER BY family_id, role DESC"
            ).fetchall()
        return [self._row_to_account(r).to_public_dict() for r in rows]

    def is_rate_limited(self, username: str) -> bool:
        """登录失败限流判定。"""
        now = time.time()
        recent = [t for t in self._attempts.get(username, []) if now - t < _WINDOW_SECONDS]
        self._attempts[username] = recent
        return len(recent) >= _MAX_ATTEMPTS

    def record_failure(self, username: str) -> None:
        self._attempts.setdefault(username, []).append(time.time())

    def clear_failures(self, username: str) -> None:
        self._attempts.pop(username, None)

    @staticmethod
    def _row_to_account(row: Any) -> Account:
        return Account(
            user_id=row["user_id"],
            family_id=row["family_id"],
            member_id=row["member_id"],
            username=row["username"],
            role=row["role"],
            name=row["name"],
            city=row["city"],
        )


def verify_login(store: AccountStore, username: str, password: str) -> Account | None:
    """登录校验。失败原因一律不明确返回（防账号枚举）。"""
    if store.is_rate_limited(username):
        logger.warning("login rate limited: %s", username,
                       extra={"extra_fields": {"event": "auth.rate_limited"}})
        return None

    stored = store.get_password_hash(username)
    # 用户不存在时也跑一次哈希校验：让"用户不存在"和"密码错误"耗时接近，
    # 避免用响应时间差嗅探哪些用户名是真实存在的。
    ok = verify_password(password, stored or hash_password("__invalid__"))
    account = store.get_by_username(username) if ok and stored else None
    if account is None:
        store.record_failure(username)
        logger.warning("login failed: %s", username,
                       extra={"extra_fields": {"event": "auth.login_failed"}})
        return None

    store.clear_failures(username)
    logger.info("login ok: %s (%s)", username, account.family_id,
                extra={"extra_fields": {"event": "auth.login_ok",
                                        "family_id": account.family_id,
                                        "member_id": account.member_id}})
    return account
