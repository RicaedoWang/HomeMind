"""设备与房间的持久化。

为什么设备要落库：

V1.2 之前设备是 `device_server._DEVICES` 这个模块级字典，用户在控制台上能看不能改。
V1.3 少爷要"自己添加电器"，并且明确选了**内置设备也能删**——
一旦内置设备可删，代码里的常量就不再是真相：用户删掉洗衣机，进程一重启，
`_DEVICES` 又从常量里长回来，删了个寂寞。

所以从这一版起：**数据库是设备的唯一真相**，代码里的内置清单降级为
"首次播种时用的模板"。`_seed_log` 表记下哪些家庭已经播过种，
这样"用户把设备全删光"和"这是一个新家庭"能被区分开，前者不会复活设备。

房间单独一张表：用户要求房间能增删改。设备的 `room` 字段仍然是字符串
（不是外键），因为内置场景、意图解析、控制台分组全都按房间名匹配——
改成外键要动的地方太多，而字符串在"改名时同步更新"这一条上已经够用
（见 `rename_room`：它会在同一个事务里把设备房间一起改掉）。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..core.models import DeviceState
from ..core.trace import get_logger
from ..auth.family_store import DEFAULT_DB_PATH

logger = get_logger("tools.device_store")

# 设备类型白名单。放这里而不是 API 层，是为了让"存进库的数据"天然只有这几种，
# 不管入口是 HTTP 接口还是别的什么。
# extra 是兜底类型：用户加"咖啡机"时选它，界面按通用电器画图标。
DEVICE_TYPES: dict[str, str] = {
    "light": "灯",
    "ac": "空调",
    "curtain": "窗帘",
    "lock": "门锁",
    "heater": "地暖",
    "fan": "风扇/新风",
    "washer": "洗衣机",
    "dryer": "晾衣机",
    "robot": "扫地机器人",
    "humidifier": "加湿器",
    "valve": "水阀/浇花器",
    "hood": "油烟机",
    "tv": "电视",
    "speaker": "音箱",
    "extra": "其他电器",
}

# 高危类型：新建或改名时给个提示，执行时仍由安全护栏按类型强制确认。
HIGH_RISK_TYPES = frozenset({"lock", "gas_valve"})


def _connect(db_path: str) -> sqlite3.Connection:
    """统一连接入口（委托给 core.db，集中配置 WAL 与外键）。

    这里是最热的写路径——每次设备开关、每次事件记录都要开连接。
    WAL 下单条提交从 3.6ms 降到 0.02ms，收益最大的就是这处。
    """
    from ..core.db import connect

    return connect(db_path)


def new_device_id() -> str:
    return f"dev_{uuid.uuid4().hex[:8]}"


class DeviceStore:
    """设备表 + 房间表（同一个内容库 `data/homemind_content.db`）。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device (
                    family_id   TEXT NOT NULL,
                    device_id   TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    room        TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    power       INTEGER NOT NULL DEFAULT 0,
                    value       REAL,
                    min_value   REAL,
                    max_value   REAL,
                    unit        TEXT NOT NULL DEFAULT '',
                    builtin     INTEGER NOT NULL DEFAULT 0,
                    sort_order  INTEGER NOT NULL DEFAULT 0,
                    created_at  REAL NOT NULL,
                    PRIMARY KEY (family_id, device_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS room (
                    family_id  TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    builtin    INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (family_id, name)
                )
            """)
            # 播种标记：区分"这个家庭还没浇过水"和"用户把设备全删光了"。
            # 没有这张表，前者和后者在数据上长得一模一样，
            # 于是删光的家庭每次重启都复活一套设备——用户会以为删除功能坏了。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_seed_log (
                    family_id TEXT PRIMARY KEY,
                    seeded_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_family ON device(family_id)"
            )

    # ------------------------------------------------------------ 播种

    def seeded(self, family_id: str) -> bool:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM device_seed_log WHERE family_id=?", (family_id,)
            ).fetchone()
        return row is not None

    def seed_family(
        self,
        family_id: str,
        devices: Iterable[DeviceState],
        rooms: Iterable[str] = (),
    ) -> bool:
        """给一个家庭播种内置设备与房间（幂等）。返回是否真的播了。

        幂等靠 `device_seed_log`，不靠"设备表是不是空的"：
        用户删光设备后表也是空的，靠表内容判断会让删除功能看起来失灵。
        """
        if self.seeded(family_id):
            return False

        now = time.time()
        device_list = list(devices)
        # 房间从设备反推：设备所在的房间必须先存在，否则控制台按房间分组时
        # 会出现"有设备但没有房间"的孤儿分组。
        room_names = list(dict.fromkeys([*(rooms or []), *(d.room for d in device_list)]))

        with _connect(self.db_path) as conn:
            for order, room in enumerate(room_names):
                if not room:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO room
                       (family_id, name, builtin, sort_order, created_at)
                       VALUES (?,?,1,?,?)""",
                    (family_id, room, order, now),
                )
            for order, dev in enumerate(device_list):
                conn.execute(
                    """INSERT OR IGNORE INTO device
                       (family_id, device_id, name, room, device_type, power, value,
                        min_value, max_value, unit, builtin, sort_order, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (family_id, dev.device_id, dev.name, dev.room, dev.device_type,
                     1 if dev.power else 0, dev.value, dev.min_value, dev.max_value,
                     dev.unit, order, now),
                )
            conn.execute(
                "INSERT OR REPLACE INTO device_seed_log (family_id, seeded_at) VALUES (?,?)",
                (family_id, now),
            )
        logger.info("device seed: %s (%d 台)", family_id, len(device_list),
                    extra={"extra_fields": {"event": "device.seed",
                                            "family_id": family_id}})
        return True

    # ------------------------------------------------------------ 读

    def devices_of(self, family_id: str) -> dict[str, DeviceState]:
        """本家庭的全部设备，按播种/创建顺序返回。

        未知家庭返回空桶，**不自动建**——读操作顺手建数据会让评测不可复现
        （查一次状态就多一个家庭），初始化必须是显式动作。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM device WHERE family_id=? ORDER BY sort_order, created_at",
                (family_id,),
            ).fetchall()
        return {row["device_id"]: self._row_to_device(row) for row in rows}

    def get(self, family_id: str, device_id: str) -> DeviceState | None:
        """只在本家庭范围内找。别家的 device_id 一律当不存在。"""
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM device WHERE family_id=? AND device_id=?",
                (family_id, device_id),
            ).fetchone()
        return self._row_to_device(row) if row else None

    def all_family_ids(self) -> set[str]:
        """库里出现过的所有家庭（设备表 ∪ 房间表 ∪ 播种标记）。

        三个都取是因为它们可能不同步：一个刚播种还没加设备的家庭只在
        seed_log 里，一个设备全删光的家庭也只剩 seed_log。
        `reset_devices()` 靠它清理测试期间动态注册的家庭。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT family_id FROM device
                   UNION SELECT family_id FROM room
                   UNION SELECT family_id FROM device_seed_log"""
            ).fetchall()
        return {r["family_id"] for r in rows}

    # ------------------------------------------------------------ 写：设备

    def add(
        self,
        family_id: str,
        name: str,
        room: str,
        device_type: str = "extra",
        min_value: float | None = None,
        max_value: float | None = None,
        unit: str = "",
        initial_value: float | None = None,
    ) -> DeviceState:
        """新增一台设备。房间不存在时自动补一条房间记录。

        自动补房间是刻意的：用户添加电器时只会从下拉里选现有房间，
        但如果直接调接口传了个新房间，拒绝反而多余——顺手建了更好用。
        """
        device_id = new_device_id()
        now = time.time()
        value = initial_value
        if value is not None and min_value is not None and max_value is not None:
            value = max(min_value, min(max_value, value))
        with _connect(self.db_path) as conn:
            order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM device WHERE family_id=?",
                (family_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO device
                   (family_id, device_id, name, room, device_type, power, value,
                    min_value, max_value, unit, builtin, sort_order, created_at)
                   VALUES (?,?,?,?,?,0,?,?,?,?,0,?,?)""",
                (family_id, device_id, name.strip(), room, device_type, value,
                 min_value, max_value, unit, order, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO room
                   (family_id, name, builtin, sort_order, created_at)
                   VALUES (?,?,0,?,?)""",
                (family_id, room, self._next_room_order(conn, family_id), now),
            )
        logger.info("device added: %s (%s)", device_id, name,
                    extra={"extra_fields": {"event": "device.add",
                                            "family_id": family_id,
                                            "device": device_id}})
        result = self.get(family_id, device_id)
        assert result is not None  # 刚插进去必然读得到
        return result

    def update(
        self,
        family_id: str,
        device_id: str,
        name: str | None = None,
        room: str | None = None,
        min_value: float | None = None,
        max_value: float | None = None,
        unit: str | None = None,
        clear_range: bool = False,
    ) -> DeviceState | None:
        """改设备属性。找不到返回 None（调用方据此回 404）。

        `clear_range` 是"把可调范围改回不可调"的显式开关。
        没有它就没法区分"不传 = 不改"和"传 null = 改成不可调"——
        前者是省略，后者是用户的明确意图。
        """
        current = self.get(family_id, device_id)
        if current is None:
            return None

        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name.strip()
        if room is not None:
            fields["room"] = room
        if clear_range:
            fields["min_value"] = None
            fields["max_value"] = None
        else:
            if min_value is not None:
                fields["min_value"] = min_value
            if max_value is not None:
                fields["max_value"] = max_value
        if unit is not None:
            fields["unit"] = unit

        with _connect(self.db_path) as conn:
            if fields:
                assignments = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE device SET {assignments} WHERE family_id=? AND device_id=?",
                    (*fields.values(), family_id, device_id),
                )
            if room is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO room
                       (family_id, name, builtin, sort_order, created_at)
                       VALUES (?,?,0,?,?)""",
                    (family_id, room, self._next_room_order(conn, family_id), time.time()),
                )
            # 改完范围后把当前值夹一遍，避免留下"值在范围外"的脏数据
            updated = self._read_one(conn, family_id, device_id)
            if (updated and updated.value is not None
                    and updated.min_value is not None and updated.max_value is not None):
                clamped = updated.clamp(updated.value)
                if clamped != updated.value:
                    conn.execute(
                        "UPDATE device SET value=? WHERE family_id=? AND device_id=?",
                        (clamped, family_id, device_id),
                    )
        return self.get(family_id, device_id)

    def delete(self, family_id: str, device_id: str) -> bool:
        """删设备。内置设备也能删——这是少爷明确要的（V1.3）。

        删了不会复活：`device_seed_log` 拦住播种逻辑，重启照旧是删掉的状态。
        """
        current = self.get(family_id, device_id)
        if current is None:
            return False
        with _connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM device WHERE family_id=? AND device_id=?",
                (family_id, device_id),
            )
        logger.info("device deleted: %s", device_id,
                    extra={"extra_fields": {"event": "device.delete",
                                            "family_id": family_id,
                                            "device": device_id}})
        return True

    def save_state(self, family_id: str, device: DeviceState) -> None:
        """把运行态（开关/当前值）写回库。

        每次 turn_on / set_value 都要调它。以前状态只在内存里，
        "关灯"重启后又亮着——用户会说"这玩意儿失忆了"。
        """
        with _connect(self.db_path) as conn:
            conn.execute(
                """UPDATE device SET power=?, value=?
                   WHERE family_id=? AND device_id=?""",
                (1 if device.power else 0, device.value, family_id, device.device_id),
            )

    # ------------------------------------------------------------ 房间

    def rooms_of(self, family_id: str) -> list[str]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM room WHERE family_id=? ORDER BY sort_order, created_at",
                (family_id,),
            ).fetchall()
        return [r["name"] for r in rows]

    def add_room(self, family_id: str, name: str) -> str:
        name = name.strip()
        with _connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO room
                   (family_id, name, builtin, sort_order, created_at)
                   VALUES (?,?,0,?,?)""",
                (family_id, name, self._next_room_order(conn, family_id), time.time()),
            )
        return name

    def rename_room(self, family_id: str, old: str, new: str) -> bool:
        """房间改名，并把该房间下所有设备的 room 一起改。

        这两步必须在同一个事务里：只改 room 表不改设备，
        设备就会挂在"不存在的房间"下，控制台分组时整个消失。
        """
        new = new.strip()
        if not new or new == old:
            return False
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM room WHERE family_id=? AND name=?", (family_id, old)
            ).fetchone()
            if row is None:
                return False
            if conn.execute(
                "SELECT 1 FROM room WHERE family_id=? AND name=?", (family_id, new)
            ).fetchone():
                # 目标名字已存在：不做合并，直接拒绝。合并房间是另一件事，
                # 悄悄把两房间的设备并到一起，用户会发现设备"换了位置"。
                return False
            conn.execute(
                "UPDATE room SET name=? WHERE family_id=? AND name=?",
                (new, family_id, old),
            )
            conn.execute(
                "UPDATE device SET room=? WHERE family_id=? AND room=?",
                (new, family_id, old),
            )
        return True

    def devices_in_room(self, family_id: str, room: str) -> list[DeviceState]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM device WHERE family_id=? AND room=? ORDER BY sort_order",
                (family_id, room),
            ).fetchall()
        return [self._row_to_device(r) for r in rows]

    def delete_room(self, family_id: str, name: str) -> tuple[bool, str]:
        """删房间。房间下还有设备就拒绝——这是少爷选的策略：不丢数据。

        返回 (是否删掉, 拒绝原因)。原因要能直接展示给用户，
        否则用户只会看到"操作失败"四个字，不知道下一步该干什么。
        """
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM room WHERE family_id=? AND name=?", (family_id, name)
            ).fetchone()
            if row is None:
                return False, "房间不存在"
            used = conn.execute(
                "SELECT COUNT(*) FROM device WHERE family_id=? AND room=?",
                (family_id, name),
            ).fetchone()[0]
            if used:
                return False, f"「{name}」里还有 {used} 台设备，先把它们移到别的房间再删"
            conn.execute(
                "DELETE FROM room WHERE family_id=? AND name=?", (family_id, name)
            )
        return True, ""

    # ------------------------------------------------------------ 重置（测试）

    def reset_family(self, family_id: str) -> None:
        """把某家庭恢复成出厂：删掉全部设备与房间、清播种标记。

        测试与评测靠它保证可复现；真实使用中不暴露这个入口。
        """
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM device WHERE family_id=?", (family_id,))
            conn.execute("DELETE FROM room WHERE family_id=?", (family_id,))
            conn.execute("DELETE FROM device_seed_log WHERE family_id=?", (family_id,))

    def reset_all(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM device")
            conn.execute("DELETE FROM room")
            conn.execute("DELETE FROM device_seed_log")

    def clear_family(self, family_id: str) -> None:
        """只删数据、保留播种标记：模拟"用户把设备删光了"。"""
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM device WHERE family_id=?", (family_id,))
            conn.execute("DELETE FROM room WHERE family_id=?", (family_id,))

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _next_room_order(conn: sqlite3.Connection, family_id: str) -> int:
        return conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM room WHERE family_id=?",
            (family_id,),
        ).fetchone()[0]

    @staticmethod
    def _read_one(conn: sqlite3.Connection, family_id: str, device_id: str) -> DeviceState | None:
        row = conn.execute(
            "SELECT * FROM device WHERE family_id=? AND device_id=?",
            (family_id, device_id),
        ).fetchone()
        return DeviceStore._row_to_device(row) if row else None

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> DeviceState:
        return DeviceState(
            device_id=row["device_id"],
            name=row["name"],
            room=row["room"],
            device_type=row["device_type"],
            power=bool(row["power"]),
            value=row["value"],
            min_value=row["min_value"],
            max_value=row["max_value"],
            unit=row["unit"],
            builtin=bool(row["builtin"]),
        )


# ---------------------------------------------------------------- 单例与测试接缝

_device_store: DeviceStore | None = None


def get_device_store() -> DeviceStore:
    global _device_store
    if _device_store is None:
        _device_store = DeviceStore()
    return _device_store


def set_device_store(store: DeviceStore | None) -> None:
    """测试接缝：换成临时库（与 auth.set_account_store 同套路）。

    设备现在会落库，测试若走默认路径就是往用户的真实内容库里写测试设备——
    和 family/scene 一模一样的风险，所以留同一个接缝。
    """
    global _device_store
    _device_store = store
