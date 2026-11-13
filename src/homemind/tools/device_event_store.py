"""设备事件历史（故障 / 维修 / 保养 / 更换）——「私有数据」的落点。

## 为什么单独一张表

`memory_struct` 存的是**偏好**（`preference:空调温度` = 26 度），
它回答的是"这个人喜欢什么"。

但面试官真正会追问的那句话是"**你上次洗衣机什么时候修的**"——这是
**事件**，不是偏好：有时间、有故障现象、有原因、有处理动作、有花费。
偏好表装不下它，硬塞进去只会把两种完全不同的数据搅成一锅。

所以单开一张 `device_event`，与设备表同库（都属于"用户可写的内容数据"）。

## 它解决什么问题

通用大模型知道"洗衣机报 P4 是进水异常"，但它不知道：

- 你家这台上个月刚因为同样的 P4 清洗过滤网；
- 你家水压本来就偏低，上次折腾半天发现不是滤网的问题；
- 上次维修花了多少钱、谁修的。

这些数据只在你自己系统里。**这才是"为什么非你不可"的答案**，
而不是"我能查说明书"——说明书网上遍地都是。

## 设计约定

- `family_id` 进所有查询：两个家庭的同型号洗衣机是两台不同的机器。
- `device_id` 存 id 而不是名称：设备改名后历史不能断，名称只作冗余展示。
- `happened_at` 与 `created_at` 分开：补录历史时发生时间不等于录入时间。
- 时间一律存 Unix 时间戳（REAL），与项目其它表保持一致。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..auth.family_store import DEFAULT_DB_PATH
from ..core.trace import get_logger
from ..memory.semantic import jaccard_similarity

logger = get_logger("tools.device_event_store")

# 事件类型白名单。放这里而不是 API 层，理由同 device_store 的 DEVICE_TYPES：
# 让「进库的数据」天然只有这几种，不管入口是接口还是脚本。
EVENT_TYPES: dict[str, str] = {
    "fault": "故障",
    "repair": "维修",
    "maintain": "保养",
    "replace": "更换",
}


def _connect(db_path: str) -> sqlite3.Connection:
    """统一连接入口（委托 core.db，WAL / busy_timeout 一起生效）。"""
    from ..core.db import connect

    return connect(db_path)


class DeviceEventStore:
    """设备事件的持久化与检索。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_event (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id    TEXT NOT NULL,
                    device_id    TEXT NOT NULL,
                    device_name  TEXT NOT NULL DEFAULT '',
                    event_type   TEXT NOT NULL,
                    symptom      TEXT NOT NULL DEFAULT '',
                    cause        TEXT NOT NULL DEFAULT '',
                    action       TEXT NOT NULL DEFAULT '',
                    cost         REAL,
                    happened_at  REAL NOT NULL,
                    created_at   REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_device "
                "ON device_event(family_id, device_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_time "
                "ON device_event(family_id, happened_at)"
            )

    # ------------------------------------------------------------ 写入

    def record(
        self,
        family_id: str,
        device_id: str,
        event_type: str = "fault",
        symptom: str = "",
        cause: str = "",
        action: str = "",
        cost: float | None = None,
        device_name: str = "",
        happened_at: float | None = None,
    ) -> int:
        """记一条事件，返回自增 id。

        `event_type` 不在白名单里时退回 `fault` 而不是抛异常——
        这类数据是"多记一条没坏处、漏记一条就讲不出故事"，
        因为一个参数写错就把用户的维修历史丢掉，得不偿失。
        """
        if event_type not in EVENT_TYPES:
            logger.warning("unknown event_type %r, fallback to fault", event_type)
            event_type = "fault"

        now = time.time()
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO device_event
                   (family_id, device_id, device_name, event_type, symptom,
                    cause, action, cost, happened_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    family_id, device_id, device_name or "", event_type,
                    symptom, cause, action,
                    cost, happened_at if happened_at is not None else now, now,
                ),
            )
            event_id = cur.lastrowid
        logger.info("device event recorded: %s/%s %s", family_id, device_id, event_type,
                    extra={"extra_fields": {"event": "device_event.record",
                                            "family_id": family_id}})
        return int(event_id or 0)

    # ------------------------------------------------------------ 查询

    def list_for_device(
        self, family_id: str, device_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """某台设备的历史事件，按发生时间倒序。"""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM device_event
                   WHERE family_id=? AND device_id=?
                   ORDER BY happened_at DESC LIMIT ?""",
                (family_id, device_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_all(self, family_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """本家庭全部事件，按发生时间倒序。"""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM device_event
                   WHERE family_id=? ORDER BY happened_at DESC LIMIT ?""",
                (family_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_for_query(
        self, family_id: str, query: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        """给知识问答用：从一句话里找出相关的设备历史。

        打分顺序（越靠前越可信）：

        1. **设备名直接出现在问句里**——"洗衣机报 P4 怎么办"命中
           `device_name="洗衣机"` 的事件。这一路最准，给 1.0 分。
        2. 否则用 Jaccard 算「故障现象 + 处理动作」与问句的字面重合度，
           兜住"滤网清洗""进水异常"这类没提设备名的说法。

        只扫最近 200 条：事件是随时间累积的，问一次故障几乎不可能
        关联到三年前的记录，全表扫描没有收益。
        """
        rows = self.list_all(family_id, limit=200)
        if not rows:
            return []

        scored: list[dict[str, Any]] = []
        for row in rows:
            name = row.get("device_name") or ""
            if name and name in query:
                score = 1.0
            else:
                text = f"{row.get('symptom', '')} {row.get('cause', '')} {row.get('action', '')}"
                score = jaccard_similarity(query, text)
            if score > 0:
                scored.append({**row, "score": round(score, 4)})

        scored.sort(key=lambda x: (x["score"], x["happened_at"]), reverse=True)
        return scored[:limit]

    def count(self, family_id: str) -> int:
        with _connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM device_event WHERE family_id=?", (family_id,)
            ).fetchone()[0]

    # ------------------------------------------------------------ 重置（测试）

    def reset_family(self, family_id: str) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM device_event WHERE family_id=?", (family_id,))

    def reset_all(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM device_event")

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "event_type": row["event_type"],
            "event_label": EVENT_TYPES.get(row["event_type"], row["event_type"]),
            "symptom": row["symptom"],
            "cause": row["cause"],
            "action": row["action"],
            "cost": row["cost"],
            "happened_at": row["happened_at"],
        }


# ---------------------------------------------------------------- 单例与测试接缝

_event_store: DeviceEventStore | None = None


def get_device_event_store() -> DeviceEventStore:
    global _event_store
    if _event_store is None:
        _event_store = DeviceEventStore()
    return _event_store


def set_device_event_store(store: DeviceEventStore | None) -> None:
    """测试接缝：换成临时库（与 set_device_store / set_knowledge_store 同套路）。

    这是**用户可写**的存储，走默认路径就是往真实内容库里写测试事件，
    所以必须留这个接缝，并同步加到 tests/conftest.py 的隔离 fixture 里。
    """
    global _event_store
    _event_store = store
