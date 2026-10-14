"""Web 控制台 API 回归测试。

每一条都对应界面设计文档里的一条交互契约：
    test_state_contract             → 首页一次请求拿齐全部数据（4.1 / 5.1）
    test_device_control_fresh_state → 6.9 控制后必须回"执行后"的状态，不能回旧值
    test_lock_requires_confirm      → 8.4 高危设备未确认不放行（误操作率 = 0）
    test_lock_confirmed_unlocks     → 8.4 确认后正常执行
    test_task_complete_and_reopen   → 6.4 完成任务联动设备 + 6.11 撤销
    test_task_cancel_keeps_record   → 6.5 取消不物理删除，留痕可追溯
    test_scene_activate             → 5.4 场景一键联动多个设备
    test_web_assets_served          → 静态站点能被同一条命令托管
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.console import reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_devices()
    reset_schedules()
    reset_tasks()
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_state_contract(client: TestClient) -> None:
    """首页只需要一次请求：环境、任务、设备、场景、能耗、消息、家庭。"""
    data = client.get("/api/console/state").json()
    for key in ("env", "tasks", "devices", "scenes", "energy", "messages", "family"):
        assert key in data, f"缺少字段 {key}"
    assert data["pendingCount"] >= 1
    assert len(data["devices"]) >= 5
    assert data["env"]["indoor"]["temp"] is not None


def test_device_control_fresh_state(client: TestClient) -> None:
    """回归：控制接口必须在工具调用之后再读设备状态。

    曾经把 _device_payload() 提到调用之前，结果返回"已打开客厅灯"
    却带着 power=False、statusText="已关闭" 的旧值——
    前端会显示"后端说开了、卡片还关着"的撕裂状态。
    """
    res = client.post("/api/console/devices/living_room_light/control",
                      json={"action": "set_value", "value": 60}).json()
    assert res["ok"] is True
    assert res["device"]["power"] is True
    assert res["device"]["value"] == 60.0
    assert "60" in res["device"]["statusText"]


def test_lock_requires_confirm(client: TestClient) -> None:
    res = client.post("/api/console/devices/front_door_lock/control",
                      json={"action": "turn_on"}).json()
    assert res["ok"] is False
    assert res["requiresConfirm"] is True
    # 关键：被拦下时设备状态不能变
    assert res["device"]["power"] is False


def test_lock_confirmed_unlocks(client: TestClient) -> None:
    res = client.post("/api/console/devices/front_door_lock/control",
                      json={"action": "turn_on", "confirmed": True}).json()
    assert res["ok"] is True
    assert res["device"]["power"] is True
    assert "开锁" in res["device"]["statusText"]


def test_task_complete_and_reopen(client: TestClient) -> None:
    """完成任务 → 联动关联设备 → 撤销后回到待办。"""
    res = client.post("/api/console/tasks/t-plant/complete", json={}).json()
    assert res["ok"] is True
    assert res["task"]["status"] == "done"
    assert any(item["ok"] for item in res["linked"]), "完成浇花任务应联动打开浇花器"

    back = client.post("/api/console/tasks/t-plant/reopen", json={}).json()
    assert back["task"]["status"] == "pending"
    assert "completedAt" not in back["task"]


def test_task_cancel_keeps_record(client: TestClient) -> None:
    res = client.post("/api/console/tasks/t-pet/cancel", json={"reason": "已经做过了"}).json()
    assert res["task"]["status"] == "cancelled"
    assert res["task"]["cancelReason"] == "已经做过了"
    # 取消不是删除：记录仍在列表里，可追溯
    tasks = client.get("/api/console/state").json()["tasks"]
    assert any(t["id"] == "t-pet" for t in tasks)


def test_scene_activate(client: TestClient) -> None:
    res = client.post("/api/console/scenes/home/activate", json={}).json()
    assert res["ok"] is True
    assert len(res["affected"]) >= 2
    assert all(item["ok"] for item in res["affected"])


def test_web_assets_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "HomeMind" in page.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/app.css").status_code == 200
