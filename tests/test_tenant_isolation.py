"""租户（家庭）隔离回归测试 —— 多租户方案 S2 的验收标准。

方案文档里的三条硬指标，这里一条对一个用例：

    test_qingdao_cannot_see_shenzhen_devices    → 小明看不到深圳的设备
    test_cross_family_control_403_no_side_effect → 拿别家 device_id 控制 → 403 且状态不变
    test_two_families_keep_own_temperature      → 两家各存一条"空调温度"，互不覆盖

另外四条是防止以后有人把隔离改漏的护栏：
工具层、任务、场景、短期记忆会话各一条。

为什么这些用例值得单独一个文件：
租户隔离是**安全属性**，不是功能。功能漏了用户会抱怨，租户漏了是数据泄露，
而且平时跑起来"看起来一切正常"——只有跨家庭的用例能把它测出来。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.core.tenancy import FAMILY_ARG  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.memory.short_term import ShortTermMemory  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

SZ = {"family_id": "family_sz", "member_id": "member_001", "role": "L4",
      "name": "我", "city": "深圳"}
QD = {"family_id": "family_qd", "member_id": "member_101", "role": "L4",
      "name": "小明", "city": "青岛"}


@pytest.fixture(autouse=True)
def _clean():
    reset_devices()
    reset_schedules()
    reset_tasks()
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()


class _TenantClient:
    """带固定身份的测试客户端。

    为什么不能直接 `app.dependency_overrides[...] = ...` 然后建两个 TestClient：
    dependency_overrides 是**全局单例**，后建的会覆盖先建的，
    于是"深圳的 client"发请求时身份已经变成青岛了——测试自己先踩了这个坑。
    所以每次发请求前都把身份切回自己那一份。
    """

    def __init__(self, profile: dict[str, str]) -> None:
        self._profile = profile
        self._raw = TestClient(app)

    def _use_identity(self) -> None:
        app.dependency_overrides[current_user] = lambda: AuthUser(**self._profile)

    def get(self, *args, **kwargs):
        self._use_identity()
        return self._raw.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        self._use_identity()
        return self._raw.post(*args, **kwargs)

    def close(self) -> None:
        self._raw.close()


@pytest.fixture()
def identity(tmp_path):
    """切身份的客户端工厂。

    一次测试里要同时用两个家庭的身份发请求，所以做成工厂而不是固定 fixture。
    认证本身在 tests/test_auth.py 测，这里一律旁路认证，只测隔离。
    """
    created: list[_TenantClient] = []

    def _make(profile: dict[str, str]) -> _TenantClient:
        set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
        client = _TenantClient(profile)
        created.append(client)
        return client

    yield _make
    app.dependency_overrides.clear()
    for client in created:
        client.close()


def _device_ids(state: dict) -> set[str]:
    return {d["id"] for d in state["devices"]}


# ---------------------------------------------------------------- 硬指标 1


def test_qingdao_cannot_see_shenzhen_devices(identity):
    """硬指标 1：小明（青岛）看不到深圳家的设备，反之亦然。"""
    sz = _device_ids(identity(SZ).get("/api/console/state").json())
    qd = _device_ids(identity(QD).get("/api/console/state").json())

    assert "living_room_ac" in sz, "深圳家应该有空调"
    assert "living_room_ac" not in qd, "小明不该看到深圳的空调"
    assert "living_room_heater" in qd, "青岛家应该有地暖"
    assert "living_room_heater" not in sz, "深圳家不该看到青岛的地暖"


# ---------------------------------------------------------------- 硬指标 2


def test_cross_family_control_403_no_side_effect(identity):
    """硬指标 2：拿别家的 device_id 来控制 → 403，且那台设备状态一点没变。"""
    sz_client = identity(SZ)
    qd_client = identity(QD)

    def ac_power() -> bool:
        devices = sz_client.get("/api/console/state").json()["devices"]
        return next(d["power"] for d in devices if d["id"] == "living_room_ac")

    before = ac_power()
    res = qd_client.post("/api/console/devices/living_room_ac/control",
                         json={"action": "turn_on"})
    assert res.status_code == 403, "跨家庭控制必须 403"
    assert ac_power() == before, "被拒的请求绝不能产生副作用"


def test_device_tool_only_touches_own_family():
    """工具层隔离：handler 只认服务端注入的 __family_id。

    这条不经过 API，直接打工具注册中心——证明隔离是在**设备层**生效的，
    不是靠接口层 if 判断挡住的（接口层改漏了还有这一层兜着）。
    """
    registry = default_registry()

    async def run() -> tuple[dict, dict]:
        ok = await registry.call(
            "device_control",
            {"action": "turn_on", "device_name": "空调", FAMILY_ARG: "family_sz"},
        )
        denied = await registry.call(
            "device_control",
            {"action": "turn_on", "device_name": "空调", FAMILY_ARG: "family_qd"},
        )
        return ok.data, denied.data

    ok_data, denied_data = asyncio.run(run())
    assert ok_data["success"] is True, "深圳家开空调应该成功"
    assert denied_data["success"] is False, "青岛家没有空调，不该操作成功"


# ---------------------------------------------------------------- 硬指标 3


def test_two_families_keep_own_temperature(tmp_path):
    """硬指标 3：两家各存一条"空调温度"，key 相同但不互相覆盖。"""
    service = MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "mem.db")))

    service.write("member_001", "我习惯空调开26度", family_id="family_sz")
    service.write("member_101", "我习惯空调开20度", family_id="family_qd")

    sz_records = service.long_term.list_by_member("member_001", family_id="family_sz")
    qd_records = service.long_term.list_by_member("member_101", family_id="family_qd")

    sz_values = [r.memory_value for r in sz_records]
    qd_values = [r.memory_value for r in qd_records]

    assert any("26" in v for v in sz_values), f"深圳家应记住 26 度，实际 {sz_values}"
    assert any("20" in v for v in qd_values), f"青岛家应记住 20 度，实际 {qd_values}"
    assert all("20" not in v for v in sz_values), "深圳家的记忆被青岛家污染了"
    assert all("26" not in v for v in qd_values), "青岛家的记忆被深圳家污染了"


def test_recall_does_not_leak_across_families(tmp_path):
    """召回也按家庭隔离：拿错 family_id 去查，一条都查不到。"""
    service = MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "mem.db")))
    service.write("member_001", "我习惯空调开26度", family_id="family_sz")

    right = service.recall("空调温度", member_id="member_001", family_id="family_sz")
    wrong = service.recall("空调温度", member_id="member_001", family_id="family_qd")

    assert right, "本家庭应该能召回"
    assert not wrong, "跨家庭召回必须为空"


# ---------------------------------------------------------------- 防漏护栏


def test_task_of_other_family_not_found(identity):
    """别家的任务 id 当作不存在——不能读，更不能完成/取消。"""
    qd_client = identity(QD)

    res = qd_client.post("/api/console/tasks/t-plant/complete", json={})
    assert res.status_code == 404, "深圳家的浇花任务不该被小明操作"


def test_scene_list_differs_per_family(identity):
    """场景清单跟着家庭走：海边那个家有"通风"，深圳家没有。"""
    sz_scenes = {s["id"] for s in identity(SZ).get("/api/console/state").json()["scenes"]}
    qd_scenes = {s["id"] for s in identity(QD).get("/api/console/state").json()["scenes"]}

    assert "movie" in sz_scenes
    assert "movie" not in qd_scenes, "青岛家没有观影场景"
    assert "vent" in qd_scenes, "青岛家应该有通风场景"


def test_short_term_session_isolated_by_family():
    """会话键带家庭：两家的对话各自独立，换家不串台。"""
    memory = ShortTermMemory()

    async def run() -> tuple[list, list]:
        await memory.append("console:family_sz:member_001", "user", "深圳家的事")
        await memory.append("console:family_qd:member_101", "user", "青岛家的事")
        return (await memory.get("console:family_sz:member_001"),
                await memory.get("console:family_qd:member_101"))

    sz_msgs, qd_msgs = asyncio.run(run())
    assert len(sz_msgs) == 1 and "深圳" in sz_msgs[0]["content"]
    assert len(qd_msgs) == 1 and "青岛" in qd_msgs[0]["content"]
