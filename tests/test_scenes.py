"""场景自定义测试。

覆盖的契约：
    test_builtin_scenes_present      → 内置场景还在，且标记为只读
    test_create_scene                → 新建后立刻出现在清单里
    test_update_scene                → 改名与改动作都生效
    test_delete_scene                → 删除后消失
    test_builtin_scene_is_readonly   → 内置场景改不了也删不掉（403）
    test_cross_family_scene_404      → 别家的场景 id 一律当不存在
    test_foreign_device_rejected     → ★ 用别家设备 id 建场景必须 400
    test_value_out_of_range          → 数值越界 400
    test_empty_actions_rejected      → 空场景 400
    test_new_scene_actually_works    → 新建的场景真能执行，数值真落到设备上
    test_missing_device_is_reported  → 设备不在时执行结果要如实标失败

★ 那条是这次最关键的安全用例：场景是"一次动多台设备"的批量操作，
如果不对每个 device_id 做家庭校验，单设备接口有 403、场景接口却能改别家的灯，
隔离就在这条缝上漏光了。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from homemind.api.auth import set_account_store
from homemind.api.console import reset_console_state
from homemind.api.main import app
from homemind.auth.deps import current_user
from homemind.auth.family_store import get_scene_store
from homemind.auth.store import AccountStore
from homemind.auth.tokens import AuthUser
from homemind.tools.device_server import devices_of, reset_devices

SZ = dict(family_id="family_sz", member_id="member_001", role="L4", name="我", city="深圳")
QD = dict(family_id="family_qd", member_id="member_101", role="L4", name="小明", city="青岛")


def _as(**kwargs) -> None:
    """切换当前身份。默认参数绑死值，避免 lambda 晚绑定把身份串了。"""
    app.dependency_overrides[current_user] = lambda: AuthUser(**(kwargs or SZ))


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    reset_devices()
    reset_console_state()
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    _as(**SZ)
    yield
    reset_devices()
    reset_console_state()
    set_account_store(None)
    app.dependency_overrides.pop(current_user, None)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _new_scene_payload(name: str = "夜读",
                       device_id: str = "living_room_light") -> dict:
    return {
        "name": name,
        "icon": "moon",
        "actions": [{"device_id": device_id, "action": "set_value", "value": 15}],
    }


# ---------------- 读 ----------------

def test_builtin_scenes_present(client):
    """内置场景还在，并被标记成只读（前端据此不显示编辑按钮）。"""
    scenes = client.get("/api/console/scenes").json()["scenes"]

    names = [s["name"] for s in scenes]
    assert "回家" in names
    builtin = [s for s in scenes if s["builtin"]]
    assert builtin, "内置场景不该因为改造而消失"
    assert all(s["editable"] is False for s in builtin)


# ---------------- 增删改 ----------------

def test_create_scene(client):
    """新建后立刻能在清单里看到，且是自定义（可编辑）。"""
    resp = client.post("/api/console/scenes", json=_new_scene_payload())

    assert resp.status_code == 200, resp.text
    scene = resp.json()["scene"]
    assert scene["name"] == "夜读"
    assert scene["builtin"] is False
    assert scene["editable"] is True
    assert scene["actions"][0]["value"] == 15

    ids = [s["id"] for s in client.get("/api/console/scenes").json()["scenes"]]
    assert scene["id"] in ids


def test_update_scene(client):
    """改名与改动作都要落库。"""
    scene_id = client.post("/api/console/scenes", json=_new_scene_payload()).json()["scene"]["id"]

    resp = client.put(f"/api/console/scenes/{scene_id}", json={
        "name": "睡前",
        "icon": "moon",
        "actions": [{"device_id": "bedroom_light", "action": "set_value", "value": 5}],
    })

    assert resp.status_code == 200, resp.text
    updated = resp.json()["scene"]
    assert updated["name"] == "睡前"
    assert updated["actions"][0]["device_id"] == "bedroom_light"
    assert updated["actions"][0]["value"] == 5


def test_delete_scene(client):
    scene_id = client.post("/api/console/scenes", json=_new_scene_payload()).json()["scene"]["id"]

    assert client.delete(f"/api/console/scenes/{scene_id}").status_code == 200

    ids = [s["id"] for s in client.get("/api/console/scenes").json()["scenes"]]
    assert scene_id not in ids


def test_builtin_scene_is_readonly(client):
    """内置场景改不动也删不掉。

    让用户把"回家"删了，控制台首页就少一块；改坏了更糟——
    这些场景是演示和测试的基线，不能被用户编辑带歪。
    """
    assert client.put("/api/console/scenes/home",
                      json=_new_scene_payload()).status_code == 403
    assert client.delete("/api/console/scenes/home").status_code == 403


# ---------------- 隔离与校验 ----------------

def test_cross_family_scene_404(client):
    """别家的场景 id 一律 404，不透露"存在但不是你的"。"""
    _as(**QD)
    qd_scene = client.post("/api/console/scenes", json=_new_scene_payload("青岛的场景")).json()["scene"]

    _as(**SZ)
    assert client.delete(f"/api/console/scenes/{qd_scene['id']}").status_code == 404
    assert client.put(f"/api/console/scenes/{qd_scene['id']}",
                      json=_new_scene_payload()).status_code == 404


def test_foreign_device_rejected(client):
    """★ 用别家的设备 id 建场景必须被拒。

    这是批量接口最容易被忽略的越权口子：单设备接口有 __family_id 校验，
    场景接口如果只信前端传来的 device_id，就等于开了个后门。
    """
    resp = client.post("/api/console/scenes", json={
        "name": "偷偷改青岛",
        "icon": "moon",
        "actions": [{"device_id": "living_room_heater", "action": "turn_on", "value": None}],
    })

    assert resp.status_code == 400, "青岛地暖不属于深圳家，应被拒"
    assert "不属于这个家" in resp.json()["detail"]


def test_value_out_of_range(client):
    resp = client.post("/api/console/scenes", json={
        "name": "越界",
        "icon": "moon",
        "actions": [{"device_id": "living_room_light", "action": "set_value", "value": 150}],
    })

    assert resp.status_code == 400
    assert "0-100" in resp.json()["detail"]


def test_empty_actions_rejected(client):
    resp = client.post("/api/console/scenes", json={
        "name": "空场景", "icon": "moon", "actions": [],
    })

    assert resp.status_code == 400
    assert "至少" in resp.json()["detail"]


def test_unknown_action_rejected(client):
    resp = client.post("/api/console/scenes", json={
        "name": "瞎搞", "icon": "moon",
        "actions": [{"device_id": "living_room_light", "action": "explode", "value": None}],
    })

    assert resp.status_code == 400
    assert "不支持的动作" in resp.json()["detail"]


# ---------------- 执行 ----------------

def test_new_scene_actually_works(client):
    """自建场景要真能跑：执行后设备状态真的变了。

    "能建"和"能用"是两件事。只测建不测跑，等于交付一个只能看的玩具。
    """
    scene_id = client.post("/api/console/scenes", json=_new_scene_payload()).json()["scene"]["id"]

    resp = client.post(f"/api/console/scenes/{scene_id}/activate", json={"confirmed": False})

    assert resp.status_code == 200, resp.text
    affected = resp.json()["affected"]
    assert affected[0]["ok"] is True
    light = devices_of("family_sz")["living_room_light"]
    assert light.power is True and light.value == 15, \
        f"场景说成功了，灯却是 {light.power} / {light.value}"


def test_missing_device_is_reported(client):
    """场景里的设备不在了（换家/被移除），执行结果要如实标失败。

    以前这种设备是 `continue` 直接跳过：用户看到"执行成功"、某台设备却没动，
    会以为系统在糊弄他。现在跳过也要留一条 ok=False 的回执。
    """
    scene = get_scene_store().create(
        "family_sz", "带幽灵设备", "moon",
        [{"device_id": "ghost_device", "action": "turn_on", "value": None},
         {"device_id": "living_room_light", "action": "turn_on", "value": 40}],
    )

    resp = client.post(f"/api/console/scenes/{scene.scene_id}/activate",
                       json={"confirmed": False})

    affected = resp.json()["affected"]
    ghost = next(a for a in affected if a["deviceId"] == "ghost_device")
    assert ghost["ok"] is False
    assert "不存在" in ghost["message"]
    assert any(a["ok"] for a in affected), "正常的设备不该被幽灵设备连累"
