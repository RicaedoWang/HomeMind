"""长期结构化记忆（对齐 FR-MEM-002、FR-MEM-006）。

存储后端设计：
- **只用 SQLite**：标准库自带、零配置、开箱即跑。
- **MySQL 未接入**：`sql/init.sql` 里有对齐的 DDL，但代码里**没有任何 MySQL 连接实现**
  （没有 pymysql、没有 mysql-connector、也没有 `HM_DB_BACKEND` 分支）。
  想接需要自己写一套 Store 实现，不是改配置。

为什么没接 MySQL，以及什么时候才该接：
本项目是单机单进程部署。实测 50 个家庭 / 750 台设备 / 4,500 条记忆 / 91 万条事件
的库，查询全部是亚毫秒级（取一个家庭全部设备 0.038ms，取最近 20 条事件 0.045ms）。

真正的切换信号是**多实例部署**，不是用户变多：
- SQLite 是文件锁，多个进程同时写会互相阻塞，跨机器则完全不可行；
- 需要主从、热备、慢查询分析这类在线运维能力时；
- 团队多人改 schema、需要迁移工具链时。

这三件事一件都没发生之前，换 MySQL 只是给自己加运维负担。

DDL 与 `sql/init.sql` 保持一致，是为了「真要换的时候语义不跑偏」，
**不代表现在就能换**。这句话以前写成了"换后端只改配置"——那是假的，已更正。

对应能力：
    upsert      写入/更新（同 key 冲突时版本 +1 并留痕，FR-MEM-006）
    delete      硬删除（用户主动遗忘，隐私合规 NFR-COMP-001）
    decay       低频记忆降权（自然遗忘）
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.tenancy import DEFAULT_FAMILY_ID
from ..core.trace import get_logger

logger = get_logger("memory.long_term")

# 记忆类型（对齐 SRS 数据字典）
TYPE_PREFERENCE = "preference"      # 用户偏好
TYPE_MEMBER = "member_profile"      # 家庭成员画像
TYPE_DEVICE_HABIT = "device_habit"  # 设备使用习惯
TYPE_SCENE = "scene"                # 场景行为

# 权限等级：数值越大权限越高
ROLE_LEVEL = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


def role_level(role: str) -> int:
    """角色转等级，未知角色按最低权限处理（安全第一）。"""
    return ROLE_LEVEL.get(role.upper(), 1)


@dataclass
class MemoryRecord:
    """一条长期记忆。"""

    memory_id: int
    family_id: str
    member_id: str
    memory_type: str
    memory_key: str
    memory_value: str
    version: int
    importance: float
    visible_role: str
    created_at: float
    updated_at: float
    last_access_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "family_id": self.family_id,
            "member_id": self.member_id,
            "memory_type": self.memory_type,
            "memory_key": self.memory_key,
            "memory_value": self.memory_value,
            "version": self.version,
            "importance": round(self.importance, 3),
            "visible_role": self.visible_role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_access_at": self.last_access_at,
        }


class LongTermMemory:
    """长期记忆存储（SQLite 实现）。"""

    def __init__(self, db_path: str | None = None) -> None:
        # 默认库统一到 data/homemind.db。
        # 以前这里默认是 homemind_memory.db，而控制台"我的习惯"页读的是 homemind.db，
        # 结果 AI 记住的东西在习惯页里看不到——两个库各写各的。
        # 一个项目只有一个真相库，CLI / Web / AI 全部指向它。
        self.db_path = db_path or "data/homemind.db"
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """统一连接入口（委托给 core.db，集中配置 WAL 与外键）。"""
        from ..core.db import connect

        return connect(self.db_path)

    def _init_schema(self) -> None:
        """建表。字段与 sql/init.sql 的 memory_struct 保持一致。"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_struct (
                    memory_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id     TEXT NOT NULL DEFAULT 'family_sz',
                    member_id     TEXT NOT NULL,
                    memory_type   TEXT NOT NULL,
                    memory_key    TEXT NOT NULL,
                    memory_value  TEXT NOT NULL,
                    version       INTEGER NOT NULL DEFAULT 1,
                    importance    REAL NOT NULL DEFAULT 0.5,
                    visible_role  TEXT NOT NULL DEFAULT 'L3',
                    created_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL,
                    last_access_at REAL NOT NULL,
                    UNIQUE(family_id, member_id, memory_key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_history (
                    history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id   INTEGER NOT NULL,
                    old_value   TEXT,
                    new_value   TEXT NOT NULL,
                    changed_at  REAL NOT NULL
                )
            """)
        # 老库（V0.9 之前建的）没有 family_id 列，CREATE TABLE IF NOT EXISTS
        # 会直接跳过建表，必须靠这一步补上。
        self._migrate_family_id()

    def _migrate_family_id(self) -> None:
        """给老库补上 family_id 列，并把唯一键升级成三元组。

        SQLite 不支持 ALTER TABLE 改唯一约束，只能"建新表 → 搬数据 → 删旧表 → 改名"。
        存量数据一律归入默认家庭（family_sz）——它们是单家庭时期写的，
        本来就不属于任何"第二个家庭"，归到默认家是最贴近事实的猜测。

        全程在一个事务里做：中途崩了旧表还在，不会丢数据。
        """
        with self._connect() as conn:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(memory_struct)")}
            if "family_id" in columns:
                return

            logger.warning("memory_struct 缺少 family_id，开始迁移到租户版表结构",
                           extra={"extra_fields": {"event": "memory.migrate"}})
            conn.execute("""
                CREATE TABLE memory_struct_v2 (
                    memory_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id     TEXT NOT NULL DEFAULT 'family_sz',
                    member_id     TEXT NOT NULL,
                    memory_type   TEXT NOT NULL,
                    memory_key    TEXT NOT NULL,
                    memory_value  TEXT NOT NULL,
                    version       INTEGER NOT NULL DEFAULT 1,
                    importance    REAL NOT NULL DEFAULT 0.5,
                    visible_role  TEXT NOT NULL DEFAULT 'L3',
                    created_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL,
                    last_access_at REAL NOT NULL,
                    UNIQUE(family_id, member_id, memory_key)
                )
            """)
            conn.execute("""
                INSERT INTO memory_struct_v2
                  (memory_id, family_id, member_id, memory_type, memory_key, memory_value,
                   version, importance, visible_role, created_at, updated_at, last_access_at)
                  SELECT memory_id, 'family_sz', member_id, memory_type, memory_key,
                         memory_value, version, importance, visible_role,
                         created_at, updated_at, last_access_at
                  FROM memory_struct
            """)
            conn.execute("DROP TABLE memory_struct")
            conn.execute("ALTER TABLE memory_struct_v2 RENAME TO memory_struct")
            logger.warning("memory_struct 迁移完成：唯一键已升级为 (family_id, member_id, memory_key)",
                           extra={"extra_fields": {"event": "memory.migrate_done"}})

    def upsert(
        self,
        member_id: str,
        memory_type: str,
        memory_key: str,
        memory_value: str,
        importance: float = 0.5,
        visible_role: str = "L3",
        family_id: str = DEFAULT_FAMILY_ID,
    ) -> MemoryRecord:
        """写入或更新记忆。

        同 (family_id, member_id, memory_key) 已存在时：版本 +1、旧值进历史表（FR-MEM-006）。

        注意 family_id 排在最后且有默认值，是为了不破坏既有的位置参数调用。
        但**默认值只是给单测/评测兜底**，生产路径必须显式传——
        漏传意味着这条记忆被写进了默认家庭，属于数据放错地方。
        """
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_struct WHERE family_id=? AND member_id=? AND memory_key=?",
                (family_id, member_id, memory_key),
            ).fetchone()

            if row is None:
                cursor = conn.execute(
                    """INSERT INTO memory_struct
                       (family_id, member_id, memory_type, memory_key, memory_value, version,
                        importance, visible_role, created_at, updated_at, last_access_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (family_id, member_id, memory_type, memory_key, memory_value, 1,
                     importance, visible_role, now, now, now),
                )
                memory_id = cursor.lastrowid or 0
                logger.info("memory created: %s = %s", memory_key, memory_value,
                            extra={"extra_fields": {"event": "memory.create",
                                                    "key": memory_key}})
            else:
                memory_id = row["memory_id"]
                # 冲突更新：旧值留痕
                conn.execute(
                    "INSERT INTO memory_history (memory_id, old_value, new_value, changed_at)"
                    " VALUES (?,?,?,?)",
                    (memory_id, row["memory_value"], memory_value, now),
                )
                conn.execute(
                    """UPDATE memory_struct
                       SET memory_value=?, version=version+1, importance=?,
                           visible_role=?, updated_at=?, last_access_at=?
                       WHERE memory_id=?""",
                    (memory_value, importance, visible_role, now, now, memory_id),
                )
                logger.info("memory updated: %s -> %s (v%s)", memory_key, memory_value,
                            row["version"] + 1,
                            extra={"extra_fields": {"event": "memory.update",
                                                    "key": memory_key}})

        return self.get_by_id(memory_id)  # type: ignore[return-value]

    def get_by_id(self, memory_id: int) -> MemoryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_struct WHERE memory_id=?", (memory_id,)
            ).fetchone()
        return self._to_record(row) if row else None

    def list_by_member(
        self,
        member_id: str,
        memory_type: str | None = None,
        viewer_role: str = "L4",
        family_id: str = DEFAULT_FAMILY_ID,
    ) -> list[MemoryRecord]:
        """列出某成员在本家庭内的记忆，按查看者权限过滤（FR-MEM-008）。

        权限规则：只能看到 visible_role 等级 <= 自己等级 的记忆。
        租户规则：只查本家庭——即使 member_id 撞号，别的家庭也看不到。
        """
        sql = "SELECT * FROM memory_struct WHERE family_id=? AND member_id=?"
        params: list[Any] = [family_id, member_id]
        if memory_type:
            sql += " AND memory_type=?"
            params.append(memory_type)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        level = role_level(viewer_role)
        records = [self._to_record(r) for r in rows]
        allowed = [r for r in records if role_level(r.visible_role) <= level]
        dropped = len(records) - len(allowed)
        if dropped:
            logger.info("memory filtered by permission: %s 条被拦截", dropped,
                        extra={"extra_fields": {"event": "memory.acl_filter",
                                                "dropped": dropped}})
        return allowed

    def list_all(self) -> list[MemoryRecord]:
        """列出库中全部记忆（管理/统计用，不做权限过滤）。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM memory_struct").fetchall()
        return [self._to_record(r) for r in rows]

    def all_members(self) -> list[str]:
        """列出库中出现过的全部成员 ID。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT member_id FROM memory_struct").fetchall()
        return [r["member_id"] for r in rows]

    def touch(self, memory_id: int) -> None:
        """更新最后访问时间（用于衰减计算）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memory_struct SET last_access_at=? WHERE memory_id=?",
                (time.time(), memory_id),
            )

    def delete_by_id(self, memory_id: int) -> bool:
        """按 ID 硬删除（合并重复记忆时用）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT memory_id FROM memory_struct WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM memory_struct WHERE memory_id=?", (memory_id,))
            conn.execute("DELETE FROM memory_history WHERE memory_id=?", (memory_id,))
            return True

    def delete(self, member_id: str, memory_key: str,
               family_id: str = DEFAULT_FAMILY_ID) -> bool:
        """硬删除（用户主动遗忘 / 撤回授权，NFR-COMP-001）。

        注意：这是真删除，不是软删除标记——用户说"忘掉"就该真忘掉。
        删除也带 family_id：不能让一个家庭的操作删掉另一个家庭的记忆。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT memory_id FROM memory_struct"
                " WHERE family_id=? AND member_id=? AND memory_key=?",
                (family_id, member_id, memory_key),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM memory_struct WHERE memory_id=?", (row["memory_id"],))
            conn.execute("DELETE FROM memory_history WHERE memory_id=?", (row["memory_id"],))
            logger.warning("memory hard deleted: %s", memory_key,
                           extra={"extra_fields": {"event": "memory.delete",
                                                   "key": memory_key}})
            return True

    def decay(self, decay_lambda: float = 0.01, floor: float = 0.05) -> int:
        """自然遗忘：按距上次访问的天数衰减重要度。

        importance *= exp(-λ × Δt)，重要度低于 floor 的记忆会被清理。
        返回被清理的条数。
        """
        now = time.time()
        affected = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM memory_struct").fetchall()
            for row in rows:
                days = (now - row["last_access_at"]) / 86400.0
                new_importance = row["importance"] * pow(2.718281828, -decay_lambda * days)
                if new_importance < floor:
                    conn.execute("DELETE FROM memory_struct WHERE memory_id=?",
                                 (row["memory_id"],))
                    affected += 1
                else:
                    conn.execute("UPDATE memory_struct SET importance=? WHERE memory_id=?",
                                 (new_importance, row["memory_id"]))
        if affected:
            logger.info("memory decay cleaned %s 条冷记忆", affected,
                        extra={"extra_fields": {"event": "memory.decay",
                                                "cleaned": affected}})
        return affected

    def history(self, memory_key: str) -> list[dict[str, Any]]:
        """查看某条记忆的变更历史。"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT h.* FROM memory_history h
                JOIN memory_struct m ON m.memory_id = h.memory_id
                WHERE m.memory_key=? ORDER BY h.changed_at
            """, (memory_key,)).fetchall()
        return [dict(r) for r in rows]

    def clear(self) -> None:
        """清空全部记忆（测试用）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_struct")
            conn.execute("DELETE FROM memory_history")

    @staticmethod
    def _to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            family_id=row["family_id"] if "family_id" in row.keys() else DEFAULT_FAMILY_ID,
            member_id=row["member_id"],
            memory_type=row["memory_type"],
            memory_key=row["memory_key"],
            memory_value=row["memory_value"],
            version=row["version"],
            importance=row["importance"],
            visible_role=row["visible_role"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_access_at=row["last_access_at"],
        )
