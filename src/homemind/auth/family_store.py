"""家庭（租户）与自建场景的持久化。

为什么单独一个库 `data/homemind_content.db`：
家庭信息与自建场景是**用户创建的内容**，会随使用不断变化（注册一个新家、
新建一个场景），和账号认证、记忆偏好是三件不同的事。物理隔开的好处是：
谁要改结构就改自己的表，不用碰别人的数据，迁移失败也不会牵连真实记忆。

和 `auth/store.py` 的关系：
- 账号表里只存 `family_id` 这个字符串，不存家庭实体
- 家庭实体（名字 / 城市 / 邀请码）在这里
两边靠 `family_id` 关联。删了这个库账号也不会失效，只是家庭信息查不到，
调用方按"未知家庭"处理（而不是假装它叫某个默认名字）。

为什么内置家庭也要落库：
注册功能要求"家庭"是一个**能新增的实体**，不是代码里写死的字典。
内置的两个演示家庭只是种子数据，第一次启动写进去，之后和用户注册的家庭
走同一张表、同一套读写路径——这样"新建家庭"和"内置家庭"不会出现两套逻辑。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.trace import get_logger

logger = get_logger("auth.family")

DEFAULT_DB_PATH = "data/homemind_content.db"

# 邀请码字符表：去掉 0/O/1/I/L 这些肉眼分不清的。
# 邀请码是要念给家人听、手打进手机的，少一个歧义字符就少一次"怎么输都不对"。
_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_INVITE_LENGTH = 6

# 动作白名单。放在这里而不是 API 层，是为了让"存进去的数据"天然只有这几种，
# 不管调用方是 HTTP 接口还是别的什么入口。
VALID_ACTIONS = ("turn_on", "turn_off", "set_value")


# ---------------------------------------------------------------- 内置数据

# family_id 是租户主键，一旦生成永不改变（账号表里存的就是它）。
# invite_code 内置家庭用固定值，方便面试演示时直接念；用户新建的家庭随机生成。
BUILTIN_FAMILIES: list[dict[str, Any]] = [
    {"family_id": "family_sz", "name": "深圳的家", "city": "深圳",
     "home_id": "home_sz", "invite_code": "SZ8888"},
    {"family_id": "family_qd", "name": "青岛的家", "city": "青岛",
     "home_id": "home_qd", "invite_code": "QD8888"},
]

# 内置场景：只读（不能改名、不能删），但可以执行。
# 动作统一成 dict，不再用三元组——`for device_id, action, value in scene["actions"]`
# 这种解包，字段一多就崩，而且自建场景从 JSON 读回来天生就是 dict。
BUILTIN_SCENES: dict[str, list[dict[str, Any]]] = {
    "family_sz": [
        {"id": "home", "name": "回家", "icon": "home",
         "actions": [
             {"device_id": "living_room_light", "action": "turn_on", "value": 60},
             {"device_id": "living_room_curtain", "action": "turn_on", "value": 100},
         ]},
        {"id": "away", "name": "离家", "icon": "exit",
         "actions": [
             {"device_id": "living_room_light", "action": "turn_off", "value": None},
             {"device_id": "living_room_ac", "action": "turn_off", "value": None},
             {"device_id": "living_room_curtain", "action": "turn_off", "value": None},
         ]},
        {"id": "movie", "name": "观影", "icon": "movie",
         "actions": [
             {"device_id": "living_room_light", "action": "set_value", "value": 20},
             {"device_id": "living_room_curtain", "action": "turn_off", "value": None},
         ]},
        {"id": "sleep", "name": "睡眠", "icon": "moon",
         "actions": [
             {"device_id": "living_room_light", "action": "turn_off", "value": None},
             {"device_id": "bedroom_light", "action": "set_value", "value": 8},
         ]},
        {"id": "wake", "name": "起床", "icon": "sun",
         "actions": [
             {"device_id": "bedroom_light", "action": "turn_on", "value": 80},
             {"device_id": "living_room_curtain", "action": "turn_on", "value": 60},
         ]},
    ],
    "family_qd": [
        {"id": "home", "name": "回家", "icon": "home",
         "actions": [
             {"device_id": "living_room_light", "action": "turn_on", "value": 60},
             {"device_id": "living_room_heater", "action": "turn_on", "value": None},
         ]},
        {"id": "away", "name": "离家", "icon": "exit",
         "actions": [
             {"device_id": "living_room_light", "action": "turn_off", "value": None},
             {"device_id": "living_room_heater", "action": "turn_off", "value": None},
         ]},
        {"id": "sleep", "name": "睡眠", "icon": "moon",
         "actions": [
             {"device_id": "living_room_light", "action": "turn_off", "value": None},
             {"device_id": "living_room_heater", "action": "set_value", "value": 18},
         ]},
        {"id": "vent", "name": "通风", "icon": "wind",
         "actions": [
             {"device_id": "living_room_fan", "action": "turn_on", "value": None},
             {"device_id": "kitchen_hood", "action": "turn_off", "value": None},
         ]},
    ],
}

# 新注册的家庭：设备模板照搬深圳那套（见 device_server.ensure_family），
# 所以默认场景也得是"能用深圳设备跑通"的那一套，否则场景里全是空转。
DEFAULT_SCENE_TEMPLATE: list[dict[str, Any]] = BUILTIN_SCENES["family_sz"]


# ---------------------------------------------------------------- 数据结构

@dataclass
class Family:
    """一个家庭 = 一个租户。"""

    family_id: str
    name: str
    city: str
    home_id: str
    invite_code: str
    builtin: bool = False

    def to_dict(self, include_invite: bool = False) -> dict[str, Any]:
        """对外结构。邀请码默认不返回——它相当于"进我家"的钥匙，
        只在户主主动查看时才给（见 /api/auth/family/invite）。"""
        data: dict[str, Any] = {
            "family_id": self.family_id,
            "name": self.name,
            "city": self.city,
            "home_id": self.home_id,
            "builtin": self.builtin,
        }
        if include_invite:
            data["invite_code"] = self.invite_code
        return data


@dataclass
class Scene:
    """一个场景：名字 + 一串设备动作。"""

    scene_id: str
    family_id: str
    name: str
    icon: str
    actions: list[dict[str, Any]]
    builtin: bool = False

    @property
    def editable(self) -> bool:
        """内置场景只读。让用户删掉"回家"然后控制台空一片，不如不让删。"""
        return not self.builtin

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.scene_id,
            "name": self.name,
            "icon": self.icon,
            "actions": [dict(a) for a in self.actions],
            "builtin": self.builtin,
            "editable": self.editable,
        }


# ---------------------------------------------------------------- 工具函数

def _connect(db_path: str) -> sqlite3.Connection:
    """统一连接入口：必须设 row_factory，否则拿到的行是元组不是字典。

    `auth/store.py` 在这上面踩过一次（`row["password_hash"]` 报 TypeError），
    两个模块刻意用同样的写法，免得在同一类坑里摔第二跤。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def new_invite_code(existing: set[str]) -> str:
    """生成一个没被用过的邀请码。"""
    for _ in range(64):
        code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_LENGTH))
        if code not in existing:
            return code
    # 理论上到不了这里；真到了说明随机源出了问题，宁可报错也别返回重复码。
    raise RuntimeError("无法生成唯一邀请码")


# ---------------------------------------------------------------- 家庭存储

class FamilyStore:
    """家庭表（SQLite），内置两个演示家庭 + 用户注册的家庭。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._seed()

    def _init_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS family (
                    family_id   TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    city        TEXT NOT NULL DEFAULT '',
                    home_id     TEXT NOT NULL,
                    invite_code TEXT NOT NULL UNIQUE,
                    builtin     INTEGER NOT NULL DEFAULT 0,
                    created_at  REAL NOT NULL
                )
            """)

    def _seed(self) -> None:
        """写入内置家庭。已存在则跳过——不覆盖用户后来改过的名字。"""
        now = time.time()
        with _connect(self.db_path) as conn:
            for item in BUILTIN_FAMILIES:
                exists = conn.execute(
                    "SELECT 1 FROM family WHERE family_id=?", (item["family_id"],)
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO family
                       (family_id, name, city, home_id, invite_code, builtin, created_at)
                       VALUES (?,?,?,?,?,1,?)""",
                    (item["family_id"], item["name"], item["city"],
                     item["home_id"], item["invite_code"], now),
                )

    def get(self, family_id: str) -> Family | None:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM family WHERE family_id=?", (family_id,)
            ).fetchone()
        return self._row_to_family(row) if row else None

    def get_by_invite(self, invite_code: str) -> Family | None:
        """按邀请码找家庭。邀请码大小写不敏感——用户手打时很少全大写。"""
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM family WHERE invite_code=?", (invite_code.strip().upper(),)
            ).fetchone()
        return self._row_to_family(row) if row else None

    def list_all(self) -> list[Family]:
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM family ORDER BY builtin DESC, created_at").fetchall()
        return [self._row_to_family(r) for r in rows]

    def create(self, name: str, city: str = "") -> Family:
        """新建家庭，注册者会自动成为户主（L4），这一步只负责建实体。"""
        with _connect(self.db_path) as conn:
            used = {r["invite_code"] for r in conn.execute("SELECT invite_code FROM family")}
        family_id = f"family_{uuid.uuid4().hex[:8]}"
        family = Family(
            family_id=family_id,
            name=name.strip() or "我的家",
            city=city.strip(),
            home_id=f"home_{uuid.uuid4().hex[:8]}",
            invite_code=new_invite_code(used),
            builtin=False,
        )
        with _connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO family
                   (family_id, name, city, home_id, invite_code, builtin, created_at)
                   VALUES (?,?,?,?,?,0,?)""",
                (family.family_id, family.name, family.city, family.home_id,
                 family.invite_code, time.time()),
            )
        logger.info("family created: %s (%s)", family.family_id, family.name,
                    extra={"extra_fields": {"event": "family.create",
                                            "family_id": family.family_id}})
        return family

    @staticmethod
    def _row_to_family(row: sqlite3.Row) -> Family:
        return Family(
            family_id=row["family_id"],
            name=row["name"],
            city=row["city"],
            home_id=row["home_id"],
            invite_code=row["invite_code"],
            builtin=bool(row["builtin"]),
        )


# ---------------------------------------------------------------- 场景存储

class SceneStore:
    """自建场景表 + 内置场景合并。

    内置场景在代码里（BUILTIN_SCENES），自建场景在库里，读的时候合并成一份。
    这么做的好处：内置场景跟着代码版本走（改代码就生效），自建场景跟着用户走
    （重启不丢），两者互不干扰，也不需要给内置场景写一堆"是不是被改过"的标记。
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS custom_scene (
                    scene_id   TEXT PRIMARY KEY,
                    family_id  TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    icon       TEXT NOT NULL DEFAULT 'sparkle',
                    actions    TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scene_family ON custom_scene(family_id)"
            )

    def builtin_scenes(self, family_id: str) -> list[Scene]:
        """内置场景。有专属清单的家庭用专属的，新家庭用通用模板。"""
        source = BUILTIN_SCENES.get(family_id, DEFAULT_SCENE_TEMPLATE)
        return [
            Scene(scene_id=s["id"], family_id=family_id, name=s["name"],
                  icon=s["icon"], actions=[dict(a) for a in s["actions"]], builtin=True)
            for s in source
        ]

    def custom_scenes(self, family_id: str) -> list[Scene]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM custom_scene WHERE family_id=? ORDER BY created_at",
                (family_id,),
            ).fetchall()
        return [self._row_to_scene(r) for r in rows]

    def scenes_of(self, family_id: str) -> list[Scene]:
        """本家庭全部场景：内置在前，自建在后。"""
        return self.builtin_scenes(family_id) + self.custom_scenes(family_id)

    def find(self, family_id: str, scene_id: str) -> Scene | None:
        """按 id 找场景。只在本家庭范围内找——别家的场景 id 一律当不存在。"""
        return next((s for s in self.scenes_of(family_id) if s.scene_id == scene_id), None)

    def create(self, family_id: str, name: str, icon: str,
               actions: list[dict[str, Any]]) -> Scene:
        scene = Scene(
            scene_id=f"scene_{uuid.uuid4().hex[:8]}",
            family_id=family_id,
            name=name.strip(),
            icon=icon or "sparkle",
            actions=[dict(a) for a in actions],
            builtin=False,
        )
        with _connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO custom_scene
                   (scene_id, family_id, name, icon, actions, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (scene.scene_id, scene.family_id, scene.name, scene.icon,
                 json.dumps(scene.actions, ensure_ascii=False), time.time()),
            )
        return scene

    def update(self, family_id: str, scene_id: str, name: str | None = None,
               icon: str | None = None,
               actions: list[dict[str, Any]] | None = None) -> Scene | None:
        """改自建场景。内置场景返回 None——调用方据此回 403。"""
        current = self.find(family_id, scene_id)
        if current is None or current.builtin:
            return None
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name.strip()
        if icon is not None:
            fields["icon"] = icon
        if actions is not None:
            fields["actions"] = json.dumps([dict(a) for a in actions], ensure_ascii=False)
        if fields:
            assignments = ", ".join(f"{k}=?" for k in fields)
            with _connect(self.db_path) as conn:
                conn.execute(
                    f"UPDATE custom_scene SET {assignments} WHERE scene_id=? AND family_id=?",
                    (*fields.values(), scene_id, family_id),
                )
        return self.find(family_id, scene_id)

    def delete(self, family_id: str, scene_id: str) -> bool:
        """删自建场景。内置场景删不掉（返回 False）。"""
        current = self.find(family_id, scene_id)
        if current is None or current.builtin:
            return False
        with _connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM custom_scene WHERE scene_id=? AND family_id=?",
                (scene_id, family_id),
            )
        return True

    def clear_family(self, family_id: str) -> None:
        """清空某家庭的自建场景（测试用）。"""
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM custom_scene WHERE family_id=?", (family_id,))

    @staticmethod
    def _row_to_scene(row: sqlite3.Row) -> Scene:
        try:
            actions = json.loads(row["actions"])
        except (ValueError, TypeError):
            # 库里被手改成坏 JSON 时不能让整个控制台 500——当成空场景，
            # 前端还能删掉它重建，日志里留一笔方便排查。
            logger.warning("bad scene actions json: %s", row["scene_id"])
            actions = []
        return Scene(
            scene_id=row["scene_id"],
            family_id=row["family_id"],
            name=row["name"],
            icon=row["icon"],
            actions=actions,
            builtin=False,
        )


# ---------------------------------------------------------------- 单例与测试接缝

_family_store: FamilyStore | None = None
_scene_store: SceneStore | None = None


def get_family_store() -> FamilyStore:
    global _family_store
    if _family_store is None:
        _family_store = FamilyStore()
    return _family_store


def set_family_store(store: FamilyStore | None) -> None:
    """测试接缝：换成临时库（与 auth.set_account_store 同套路）。"""
    global _family_store
    _family_store = store


def get_scene_store() -> SceneStore:
    global _scene_store
    if _scene_store is None:
        _scene_store = SceneStore()
    return _scene_store


def set_scene_store(store: SceneStore | None) -> None:
    global _scene_store
    _scene_store = store


def get_family(family_id: str) -> Family | None:
    """便捷查询：拿不到就返回 None，由调用方决定怎么降级。"""
    return get_family_store().get(family_id)
