"""设备事件 API + 用量 API 端到端（真实 JWT，不做依赖旁路）。

覆盖：
- GET /usage 返回结构（token 数与成本字段）
- POST /device-events 记录后可查回
- 设备不属于本家庭 → 404（**只校验 family_id 不够，必须查设备归属**）
- 现象/原因/处理动作全空 → 400
- 按 device_id 过滤列表

存储都落在 conftest 隔离的内容库，不碰真实数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.store import DEMO_PASSWORD, AccountStore  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    reset_devices()
    reset_schedules()
    reset_tasks()
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)
    reset_devices()
    reset_schedules()
    reset_tasks()


def _login(client: TestClient, username: str = "wo") -> dict[str, str]:
    res = client.post("/api/auth/login",
                      json={"username": username, "password": DEMO_PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


WASHER = "balcony_washer"   # 默认种子里的洗衣机
ROBOT = "living_room_robot"  # 默认种子里的扫地机器人


def _seed(client: TestClient, h: dict[str, str]) -> None:
    """触发默认设备播种（首次访问 state 时懒播种）。"""
    res = client.get("/api/console/state", headers=h)
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------- 用量

def test_usage_endpoint_shape(client):
    """用量端点结构固定：前端和面试演示都直接读这个。"""
    h = _login(client)
    body = client.get("/api/console/usage", headers=h).json()
    assert "total" in body and "by_model" in body
    assert {"calls", "prompt_tokens", "completion_tokens", "total_tokens"} <= set(body["total"])
    # 没配单价时 cost 必须是 None，不能编一个金额
    assert body["cost"] is None or "total" in body["cost"]


# ---------------------------------------------------------------- 事件

def test_device_event_create_and_list(client):
    h = _login(client)
    _seed(client, h)

    assert client.get("/api/console/device-events", headers=h).json()["count"] == 0

    res = client.post("/api/console/device-events", json={
        "device_id": WASHER,
        "event_type": "repair",
        "symptom": "报 P4，不进水",
        "cause": "进水阀滤网堵塞",
        "action": "拆下滤网清洗",
        "cost": 80.0,
    }, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True

    events = client.get("/api/console/device-events", headers=h).json()["events"]
    assert len(events) == 1
    assert events[0]["symptom"] == "报 P4，不进水"
    assert events[0]["event_label"] == "维修"
    assert events[0]["device_name"] == "洗衣机"


def test_device_event_filter_by_device(client):
    h = _login(client)
    _seed(client, h)

    for dev in (WASHER, ROBOT):
        client.post("/api/console/device-events",
                    json={"device_id": dev, "symptom": "有条记录"}, headers=h)

    only = client.get(f"/api/console/device-events?device_id={WASHER}", headers=h).json()
    assert only["count"] == 1
    assert only["events"][0]["device_id"] == WASHER


def test_device_event_rejects_foreign_device(client):
    """设备不属于本家庭时必须 404。

    这条守的是越权：device_id 是用户可自由输入的字符串，
    只校验 family_id 就等于允许往别人家设备名下写事件。
    """
    h = _login(client)
    res = client.post("/api/console/device-events", json={
        "device_id": "dev_not_mine", "symptom": "伪造的记录",
    }, headers=h)
    assert res.status_code == 404
    assert client.get("/api/console/device-events", headers=h).json()["count"] == 0


def test_device_event_requires_content(client):
    """现象/原因/处理全空 → 400，不许落一条什么信息都没有的空记录。"""
    h = _login(client)
    _seed(client, h)
    res = client.post("/api/console/device-events",
                      json={"device_id": WASHER}, headers=h)
    assert res.status_code == 400


def test_device_event_requires_auth(client):
    """没带 token 直接 401。"""
    assert client.get("/api/console/device-events").status_code == 401
    assert client.get("/api/console/usage").status_code == 401
