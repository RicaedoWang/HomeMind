"""自定义电器与房间管理的回归（V1.3）。

回归自 2026-10-20 少爷的反馈："缺少自定义添加电器的功能"。

这一版做了三件容易被做错的事，每条都有用例守着：

1. **设备从内存搬进数据库**。用户要求"内置设备也能删"，一旦内置可删，
   代码里的常量就不再是真相——删掉洗衣机，进程重启又从常量里长回来。
   所以 `_DEVICES` 那个模块级字典退役，`DeviceStore` 接管。
   用例 `test_deleted_builtin_device_stays_deleted` 守的就是这一条：
   删完之后再读，设备不能复活。

2. **意图层必须认识用户自己加的设备**。`DEVICES` 是写死的词表，
   用户新建"咖啡机"之后，规则层认不出它，`has_device` 判 False → 整句落到闲聊，
   AI 回一句"我在听"。用户会觉得"我明明加了设备，它却听不懂"。
   用例 `test_custom_device_controllable_by_natural_language` 守这一条。

3. **删房间不丢数据**。房间里有设备时拒绝删除（少爷选的策略），
   并且要说清楚还剩几台——只说"操作失败"，用户不知道下一步该干什么。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import devices_of, reset_devices  # noqa: E402
from homemind.tools.device_store import get_device_store  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_devices()
    reset_schedules()
    reset_tasks()
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()


def _as(family_id: str, member_id: str = "member_001", role: str = "L4"):
    """换个身份调接口（旁路认证，测的是租户隔离不是登录本身）。"""
    return AuthUser(family_id=family_id, member_id=member_id, role=role,
                    name="我", city="深圳")


@pytest.fixture()
def client(tmp_path):
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides[current_user] = lambda: _as("family_sz")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)


@pytest.fixture()
def graph():
    return build_agent_graph(registry=default_registry())


async def _run(graph, text: str, family_id: str = "family_sz") -> dict:
    state = initial_state(user_input=text, family_id=family_id, trace_id="test")
    return await graph.ainvoke(state)


# ---------------- 添加 / 编辑 / 删除 ----------------

def test_add_device_lands_in_store(client: TestClient) -> None:
    res = client.post("/api/console/devices", json={
        "name": "咖啡机", "room": "厨房", "device_type": "extra",
        "min_value": 1, "max_value": 5, "unit": "档", "initial_value": 3,
    })
    assert res.status_code == 200, res.text
    device = res.json()["device"]
    assert device["name"] == "咖啡机"
    assert device["adjustable"] is True
    assert device["value"] == 3

    # 必须真的进了设备桶——接口回 200 但没落库是最恶心的假成功
    assert any(d.name == "咖啡机" for d in devices_of("family_sz").values())


def test_duplicate_device_name_rejected(client: TestClient) -> None:
    """同家庭不允许重名设备。

    重名不会报错，只会让 AI 永远操作排在前面的那台，
    用户会觉得"我说了半天它总开另一盏"，而且没有任何提示。
    """
    client.post("/api/console/devices", json={"name": "咖啡机", "room": "厨房"})
    res = client.post("/api/console/devices", json={"name": "咖啡机", "room": "客厅"})
    assert res.status_code == 409
    assert "已经有一台设备" in res.json()["detail"]


def test_update_device_name_and_room(client: TestClient) -> None:
    device = client.post("/api/console/devices",
                         json={"name": "咖啡机", "room": "厨房"}).json()["device"]
    res = client.put(f"/api/console/devices/{device['id']}",
                     json={"name": "胶囊咖啡机", "room": "餐厅"})
    assert res.status_code == 200
    assert res.json()["device"]["name"] == "胶囊咖啡机"
    assert res.json()["device"]["room"] == "餐厅"


def test_delete_device_removes_it(client: TestClient) -> None:
    device = client.post("/api/console/devices",
                         json={"name": "咖啡机", "room": "厨房"}).json()["device"]
    assert client.delete(f"/api/console/devices/{device['id']}").status_code == 200
    assert all(d.name != "咖啡机" for d in devices_of("family_sz").values())


def test_clearing_all_devices_does_not_reseed(client: TestClient) -> None:
    """把设备**全部删光**之后再读，一套都不能复活。

    这是 `device_seed_log` 存在的唯一理由，也是上一条用例测不到的盲区：
    删掉一台时，`devices_of` 里的 `if bucket: return bucket` 会直接短路，
    根本走不到播种判断，所以"删一台不复活"并不能证明播种逻辑是对的。
    只有删到表都空了，"要不要重新播种"这个问题才真的被问到。

    反过来的写法（按"表空就播种"判断）在这里会整批重建 10 台设备——
    用户清空设备后重启，一看全回来了，只会认为删除功能坏了。
    """
    for device_id in list(devices_of("family_sz")):
        assert client.delete(f"/api/console/devices/{device_id}").status_code == 200

    assert devices_of("family_sz") == {}, "设备全删光后又整批复活了"
    # 房间要留着（删设备不该顺手删房间，用户可能只是想把房间清空），
    # 但每个房间的设备数必须归零——不能出现"房间还有设备"的假象
    rooms = client.get("/api/console/rooms").json()["rooms"]
    assert all(r["deviceCount"] == 0 for r in rooms), f"设备删光了房间还挂着设备：{rooms}"


def test_new_family_still_gets_devices(tmp_path) -> None:
    """删光自己的设备，不该影响"新家庭还能拿到一套设备"。

    两条路径共用同一个 `seed_family`：一条被 seed_log 拦住，
    另一条（从没播过种的新家庭）必须照常播。改播种条件时最容易一起改坏。
    """
    from homemind.tools.device_server import ensure_family

    assert ensure_family("family_brand_new") is True
    assert len(devices_of("family_brand_new")) == 10
    # 幂等：再调一次不该重复播
    assert ensure_family("family_brand_new") is False
    assert len(devices_of("family_brand_new")) == 10


def test_set_value_clamped_to_range(client: TestClient) -> None:
    """超出范围的值要被夹住，并且如实告知。"""
    device = client.post("/api/console/devices", json={
        "name": "咖啡机", "room": "厨房", "device_type": "extra",
        "min_value": 1, "max_value": 5, "unit": "档",
    }).json()["device"]

    res = client.post(f"/api/console/devices/{device['id']}/control",
                      json={"action": "set_value", "value": 10}).json()
    assert res["ok"] is True
    assert res["device"]["value"] == 5, "超过上限应被夹到 5"
    assert "超出可调范围" in res["message"]


# ---------------- 房间 ----------------

def test_room_list_carries_device_count(client: TestClient) -> None:
    rooms = {r["name"]: r["deviceCount"]
             for r in client.get("/api/console/rooms").json()["rooms"]}
    assert rooms.get("客厅", 0) >= 1, "内置房间应该带出设备数"
    assert "主卧" in rooms


def test_delete_non_empty_room_rejected(client: TestClient) -> None:
    """房间里有设备时拒绝删除，并说清还剩几台。"""
    res = client.delete("/api/console/rooms/客厅")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "还有" in detail and "台设备" in detail
    # 拒绝之后房间必须还在，不能删了一半
    assert "客厅" in [r["name"] for r in client.get("/api/console/rooms").json()["rooms"]]


def test_delete_empty_room_works(client: TestClient) -> None:
    client.post("/api/console/rooms", json={"name": "儿童房"})
    assert client.delete("/api/console/rooms/儿童房").status_code == 200
    assert "儿童房" not in [r["name"] for r in client.get("/api/console/rooms").json()["rooms"]]


def test_rename_room_moves_devices(client: TestClient) -> None:
    """房间改名，房间里的设备必须跟着换房间。

    只改房间表不改设备，这些设备会挂在"不存在的房间"下，
    控制台按房间分组时整个消失——用户会以为设备被删了。
    """
    assert client.put("/api/console/rooms/主卧", json={"new_name": "主卧室"}).status_code == 200
    rooms = [d.room for d in devices_of("family_sz").values()]
    assert "主卧室" in rooms
    assert "主卧" not in rooms


def test_rename_room_to_existing_rejected(client: TestClient) -> None:
    """改成已存在的房间名 → 拒绝。

    不合并：悄悄把两个房间的设备并到一起，用户会发现设备"换了位置"。
    """
    res = client.put("/api/console/rooms/主卧", json={"new_name": "客厅"})
    assert res.status_code == 409


# ---------------- 租户隔离 ----------------

def test_foreign_device_not_editable(client: TestClient) -> None:
    """拿别家**独有**的 device_id 来改 → 404，且那台设备一点都不动。

    注意：两个家庭都有 `living_room_light` 这种同 id 设备（同一套模板播的种），
    所以必须挑青岛独有的（`entry_lock` 之类）才能测出"这不是我家的"。
    同 id 互不干扰由下一条用例专门验证。
    """
    app.dependency_overrides[current_user] = lambda: _as("family_qd")
    qd_only = set(devices_of("family_qd")) - set(devices_of("family_sz"))
    app.dependency_overrides[current_user] = lambda: _as("family_sz")

    assert qd_only, "测试前提变了：两家设备 id 完全重合，这条用例失去意义"
    foreign = "entry_lock" if "entry_lock" in qd_only else next(iter(qd_only))
    res = client.put(f"/api/console/devices/{foreign}", json={"name": "被改了"})
    # 深圳家里没有这个 id → 404。故意不返回 403：403 等于告诉对方"这 id 存在但不属于你"
    assert res.status_code == 404
    assert client.delete(f"/api/console/devices/{foreign}").status_code == 404
    assert devices_of("family_qd")[foreign].name != "被改了"


def test_same_device_id_across_families_isolated(client: TestClient) -> None:
    """两个家庭都有 `living_room_light`，但它们是两台不同的设备。

    这是租户隔离最容易漏的一刀：同 id 同名的设备，改一家牵连另一家。
    隔离靠 `(family_id, device_id)` 复合主键，这条用例就是它的守门人。
    """
    client.post("/api/console/devices/living_room_light/control",
                json={"action": "turn_on"})
    assert devices_of("family_sz")["living_room_light"].power is True

    assert devices_of("family_qd")["living_room_light"].power is False, \
        "深圳开灯把青岛的灯也打开了——租户隔离漏了"


def test_foreign_room_isolation(client: TestClient) -> None:
    """别家增删房间，本家的设备与房间一根毫毛都不能动。

    青岛家有"厨房"、深圳家没有（深圳的设备在客厅/主卧/玄关/阳台）——
    这种"两家清单本来就不一样"的数据，最容易测出串味。
    """
    app.dependency_overrides[current_user] = lambda: _as("family_qd")
    before_count = len(devices_of("family_sz"))
    client.post("/api/console/rooms", json={"name": "青岛书房"})
    assert client.delete("/api/console/rooms/青岛书房").status_code == 200

    app.dependency_overrides[current_user] = lambda: _as("family_sz")
    assert len(devices_of("family_sz")) == before_count
    sz_rooms = [r["name"] for r in client.get("/api/console/rooms").json()["rooms"]]
    assert "青岛书房" not in sz_rooms
    assert "客厅" in sz_rooms


# ---------------- 加的设备，AI 得听得懂 ----------------

async def test_custom_device_controllable_by_natural_language(graph) -> None:
    """用户自建的设备必须能被自然语言控制。

    这是"添加电器"这个功能的成败线：加完之后 AI 听不懂，
    等于给用户建了个摆设。根因是 `intent.DEVICES` 是写死词表，
    "咖啡机"不在里面 → has_device 为假 → 整句判成闲聊。
    """
    get_device_store().add("family_sz", "咖啡机", "厨房", "extra",
                           min_value=1, max_value=5, unit="档", initial_value=3)

    result = await _run(graph, "把咖啡机打开")
    types = [i["type"] for i in result["intents"]]
    assert types == ["device_control"], f"应判成设备控制，实际：{types}"

    device = next(d for d in devices_of("family_sz").values() if d.name == "咖啡机")
    assert device.power is True, "设备没被真的打开"


async def test_custom_room_understood(graph) -> None:
    """自建房间也要能被理解。"""
    store = get_device_store()
    store.add("family_sz", "落地灯", "影音室", "light",
              min_value=0, max_value=100, unit="%")

    result = await _run(graph, "把影音室的落地灯打开")
    assert result["intents"][0]["entities"].get("room") == "影音室"
    device = next(d for d in devices_of("family_sz").values() if d.name == "落地灯")
    assert device.power is True


async def test_custom_device_value_with_custom_unit(graph) -> None:
    """「调到 4 档」要抽得出 4。

    数值抽取原本只认"度"这个单位，用户自建的咖啡机是"档"，
    于是永远抽不出目标值，`set_value` 只会回"缺少 value 参数"。
    """
    get_device_store().add("family_sz", "咖啡机", "厨房", "extra",
                           min_value=1, max_value=5, unit="档", initial_value=3)
    await _run(graph, "把咖啡机调到 4 档")
    device = next(d for d in devices_of("family_sz").values() if d.name == "咖啡机")
    assert device.value == 4.0


async def test_device_of_other_family_is_not_recognised(graph) -> None:
    """别家的设备名不该被本家庭认识。"""
    get_device_store().add("family_qd", "泡脚桶", "卫生间", "extra")
    result = await _run(graph, "打开泡脚桶", family_id="family_sz")
    types = [i["type"] for i in result["intents"]]
    assert types != ["device_control"], "别家的设备被当作自家的了"
