"""账号存储与登录校验。

为什么账号单独一个库 `data/homemind_accounts.db`，不塞进记忆库：
记忆库里是你真实的偏好数据，认证属于"可能反复改结构"的模块，
混在一张库里迟早要迁移，迁移就有弄脏真实数据的风险。物理隔开 = 风险归零。

四个演示账号覆盖两个家庭（深圳家 / 青岛家），S3 会给两个家灌不同的设备与任务。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.trace import get_logger
from .family_store import get_family, get_family_store
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


class RegistrationError(Exception):
    """注册失败。`code` 给调用方映射 HTTP 状态码，`message` 直接显示给用户。

    注册和登录的失败语义刻意不同：登录一律"用户名或密码错误"（防账号枚举），
    注册必须说清哪一步不对——"用户名已被占用"本来就是用户需要知道的信息，
    而且注册接口天然是公开的，藏也藏不住。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
        """对外可见的信息——永远不含密码哈希。

        家庭信息从 family_store 现查，不再读 `FAMILIES` 这张写死的表：
        用户注册的新家也必须能正常显示名字，否则新用户登录后只看到一个
        `family_6e532934` 这样的乱码，谁知道这是谁家。
        """
        family = get_family(self.family_id)
        return {
            "user_id": self.user_id,
            "username": self.username,
            "member_id": self.member_id,
            "name": self.name,
            "role": self.role,
            "city": self.city,
            "family": (family.to_dict() if family
                       else {"family_id": self.family_id, "name": self.family_id}),
        }


class AccountStore:
    """账号表（SQLite）。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._attempts: dict[str, list[float]] = {}
        self._init_schema()

    def _connect(self) -> Any:
        """统一连接入口（委托给 core.db，集中配置 WAL 与外键）。

        踩过的坑：`get_password_hash` 单独 connect 时漏了 row_factory，
        结果 `row["password_hash"]` 报 TypeError——所有连接都从 `core.db.connect`
        出，配置只有一处，杜绝复发。
        """
        from ..core.db import connect

        return connect(self.db_path)

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

    def accounts_of_family(self, family_id: str) -> list[Account]:
        """某个家庭的全体成员。家庭成员列表靠它，不再读写死的 FAMILY_MEMBERS。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_account WHERE family_id=? ORDER BY role DESC, created_at",
                (family_id,),
            ).fetchall()
        return [self._row_to_account(r) for r in rows]

    def register(
        self,
        username: str,
        password: str,
        name: str,
        mode: str = "create",
        family_name: str = "",
        city: str = "",
        invite_code: str = "",
    ) -> Account:
        """注册。两种模式：

        - `create`：建一个新家庭，注册者是户主（L4），设备按模板初始化
        - `join`：凭邀请码加入已有家庭，成为普通成员（L3）

        为什么注册必须能建家庭：智能家居产品的第一个用户永远是"给自己家装一套"，
        不是"加入别人装好的家"。只做邀请码，等于要求用户先有个亲戚。
        反过来只做建家庭，则多成员共享设备这条路径永远没人走通。

        校验顺序刻意是"先查用户名再建家庭"：反过来一旦用户名重复，
        库里会留下一个没有任何成员的空家庭——那是不可回收的垃圾数据。
        """
        uname = (username or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,20}", uname):
            raise RegistrationError("invalid_username",
                                    "用户名只能是 3-20 位字母、数字或下划线")
        if len(password or "") < 6:
            raise RegistrationError("weak_password", "密码至少 6 位")
        display = (name or "").strip()
        if not display or len(display) > 20:
            raise RegistrationError("invalid_name", "昵称不能为空，且不超过 20 个字")

        if self.get_by_username(uname) is not None:
            raise RegistrationError("username_taken", "这个用户名已经被用了")

        if mode == "join":
            family = get_family_store().get_by_invite(invite_code or "")
            if family is None:
                raise RegistrationError("invalid_invite", "邀请码无效，请向家人确认")
            family_id = family.family_id
            member_id = self._next_member_id(family_id)
            role = "L3"
            city_value = family.city
        elif mode == "create":
            family = get_family_store().create(family_name or f"{display}的家", city)
            family_id = family.family_id
            member_id = "member_001"
            role = "L4"
            city_value = family.city
        else:
            raise RegistrationError("invalid_mode", "注册方式只能是创建家庭或加入家庭")

        account = Account(
            user_id=f"user_{uuid.uuid4().hex[:10]}",
            family_id=family_id,
            member_id=member_id,
            username=uname,
            role=role,
            name=display,
            city=city_value,
        )
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO user_account
                       (user_id, family_id, member_id, username, password_hash,
                        role, name, city, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (account.user_id, account.family_id, account.member_id, uname,
                     hash_password(password), role, display, city_value, time.time()),
                )
        except Exception as exc:  # noqa: BLE001 - UNIQUE 冲突是预期内的
            # 上面已经查过一次重名，能走到这里基本只有并发抢注。
            # 不把 sqlite 的原始报错抛给用户（那里面带表名和 SQL）。
            logger.warning("register insert failed: %s", type(exc).__name__)
            raise RegistrationError("username_taken", "这个用户名已经被用了") from exc

        logger.info("registered: %s → %s (%s)", uname, family_id, role,
                    extra={"extra_fields": {"event": "auth.register",
                                            "family_id": family_id,
                                            "member_id": member_id,
                                            "mode": mode}})
        return account

    def _next_member_id(self, family_id: str) -> str:
        """家庭内下一个成员编号。取现有最大号 +1，不回收空号。

        不回收是有意的：member_002 退出后把号给新来的人，历史记录（
        memory 里存的 member_id、消息中心的事件 actor）就会指向另一个人。
        编号只增不减，脏但不会认错人。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT member_id FROM user_account WHERE family_id=?", (family_id,)
            ).fetchall()
        numbers = []
        for row in rows:
            match = re.fullmatch(r"member_(\d+)", row["member_id"] or "")
            if match:
                numbers.append(int(match.group(1)))
        return f"member_{(max(numbers) + 1 if numbers else 1):03d}"

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
