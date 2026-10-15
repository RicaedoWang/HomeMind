"""Web 控制台 API 回归测试。

每一条都对应界面设计文档里的一条交互契约：
    test_state_contract             → 首页一次请求拿齐全部数据（4.1 / 5.1）
    test_device_control_fresh_state → 6.9 控制后必须回"执行后"的状态，不能回旧值
    test_lock_requires_confirm      → 8.4 高危设备未确认不放行（误操作率 = 0）
    test_lock_confirmed_unlocks     → 8.4 确认后正常执行
    test_task_complete_and_reopen   → 6.4 完成任务联动设备 + 6.11 撤销
    test_task_cancel_keeps_record   → 6.5 取消不物理删除，留痕可追溯
    test_scene_activate             → 5.4 场景一键联动多个设备
    test_habits_contract            → 习惯页读的库必须和 AI 写的是同一个
    test_web_assets_served          → 静态站点能被同一条命令托管
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
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
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
def client(tmp_path):
    """受保护接口的客户端：用固定身份旁路认证。

    为什么旁路而不是真去登录：这 8 条测的是"控制台契约"，
    不是"认证"。每层测试只测一件事，是保持确定性最省事的办法。
    认证本身的用例在 tests/test_auth.py。
    """
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides[current_user] = lambda: AuthUser(
        family_id="family_sz", member_id="member_001", role="L4", name="我", city="深圳",
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)


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


def test_habits_contract(client: TestClient) -> None:
    """习惯页必须能正常返回，且读的是 AI 写入的同一个记忆库。

    踩过的坑：这个接口原来自己 new 了一个 MemoryService(另一个 db_path)，
    而 AI 助手写的是默认库——结果"AI 刚记住的习惯，习惯页里一条都查不到"。
    后来它又因为漏 import 直接 500 了，而当时**没有任何用例覆盖它**，
    是人在浏览器里点才发现的。所以补这条：接口不许 500，字段不许缺。
    """
    res = client.get("/api/console/habits")
    assert res.status_code == 200, f"习惯页不该 500：{res.text[:200]}"
    body = res.json()
    assert "habits" in body and "count" in body
    assert isinstance(body["habits"], list)


def test_web_assets_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "HomeMind" in page.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/app.css").status_code == 200


def _device(res: dict, device_id: str) -> dict:
    """控制接口回的是被操作的那**一台**设备，不是列表。"""
    assert res["device"]["id"] == device_id
    return res["device"]


def test_turn_on_actually_turns_on(client: TestClient) -> None:
    """回归：turn_on 必须真的把设备打开，不是"调用了工具"就算完。

    曾经的写法（少一对括号）：

        "action": "unlock" if req.action == "turn_on" else "lock"
        if device.device_type == "lock" else req.action,

    Python 的条件表达式是**右结合**的，实际被解析成
        "unlock" if action == "turn_on" else ("lock" if is_lock else action)
    ——于是**任何设备**的打开动作都被翻译成门锁的 "unlock"：
    工具回"不支持的动作：unlock"，灯/空调/窗帘永远打不开；
    而 turn_off 原样透传，所以表现成"能关不能开"。
    这条用例就是用户那句"开关只可以关闭不可以打开"。
    """
    off = client.post("/api/console/devices/living_room_light/control",
                      json={"action": "turn_off"}).json()
    assert off["ok"] is True
    assert _device(off, "living_room_light")["power"] is False

    on = client.post("/api/console/devices/living_room_light/control",
                     json={"action": "turn_on"}).json()
    assert on["ok"] is True
    assert "不支持的动作" not in on["message"], f"动作被翻译错了：{on['message']}"
    assert on["message"] == "已打开客厅灯"
    dev = _device(on, "living_room_light")
    assert dev["power"] is True
    # 不写死具体亮度（初值可能以后调），但"开着却是 0%"一定是坏的。
    # 注意别写成 "0%" not in statusText——"80%" 里就有 "0%" 这个子串，
    # 我自己第一版就是这么写错的。
    assert dev["statusText"].startswith("已开")
    assert dev["statusText"] != "已开 0%", \
        f"灯开了却是 0% 亮度，等于没开：{dev['statusText']}"

    # 空调也一样：带值打开必须生效
    ac = client.post("/api/console/devices/living_room_ac/control",
                     json={"action": "turn_on", "value": 24}).json()
    assert ac["ok"] is True and _device(ac, "living_room_ac")["power"] is True


def test_failed_action_reports_failure_and_records_no_event(client: TestClient) -> None:
    """工具报错时必须如实说失败，且消息中心一条都不记。

    第二层坑：以前只判 result.ok（工具"被调用"就算成功），
    可 device_control 遇到不支持的动作返回的是 success=False。
    结果 API 回了 ok=True，消息中心还记一条"打开了客厅灯"——
    灯根本没动。消息中心不许放假消息，所以必须看工具自己报的 success。
    """
    from homemind.api.events import events_of, reset_events

    reset_events()
    # set_value 不带 value → 工具明确报错
    res = client.post("/api/console/devices/living_room_light/control",
                      json={"action": "set_value"}).json()
    assert res["ok"] is False, f"操作没生效却回了成功：{res}"
    assert "缺少 value" in res["message"]
    assert events_of("family_sz") == [], "没真执行的动作不该在消息中心留痕"
