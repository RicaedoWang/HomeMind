"""真实事件流 + AI 提醒进今日任务 —— V1.1 三条交互契约的回归测试。

背景（都是用户实测抓出来的）：

1. 家庭消息原来是**写死的种子文案**（"洗衣机洗涤完成""门锁 18:20 开启"），
   跟用户做过什么毫无关系，点开就穿帮。现在消息中心是真实事件流，
   每一次操作都得留下一条——所以这里测的是"操作之后消息里真的多了对应那条"。
2. 跟 AI 说"提醒我晚上九点关灯"，AI 回了"已创建"，
   但今日任务框里什么都不出现——提醒存在 calendar_server，
   任务存在 console._TASKS，两套存储没合并。现在合并到展示层，这里测合并结果。
3. 提醒能完成/取消之后，"恢复"按钮点了报 404（reopen 只认家务任务），
   删除又根本没有接口。这两条各自对应用例，防止改回去。

事件与任务共用一份重置入口（reset_console_state）：
只清任务不清事件，会留下"任务重置了、消息里还有上一条完成记录"的撕裂状态，
断言就不可复现。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_console_state  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.core.tenancy import FAMILY_ARG  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

SZ = {"family_id": "family_sz", "member_id": "member_001", "role": "L4",
      "name": "我", "city": "深圳"}
QD = {"family_id": "family_qd", "member_id": "member_101", "role": "L4",
      "name": "小明", "city": "青岛"}


@pytest.fixture(autouse=True)
def _clean():
    reset_console_state()
    reset_devices()
    yield
    reset_console_state()
    reset_devices()


@pytest.fixture()
def client(tmp_path):
    """深圳家庭的受保护客户端。"""
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides[current_user] = lambda: AuthUser(**SZ)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)


class _TenantClient:
    """每次发请求前切回自己身份：dependency_overrides 是全局单例。"""

    def __init__(self, profile: dict[str, str]) -> None:
        self._profile = profile
        self._raw = TestClient(app)

    def _use(self) -> None:
        app.dependency_overrides[current_user] = lambda: AuthUser(**self._profile)

    def get(self, url: str):
        self._use()
        return self._raw.get(url)

    def post(self, url: str, json: dict | None = None):
        self._use()
        return self._raw.post(url, json=json or {})

    def delete(self, url: str):
        self._use()
        return self._raw.delete(url)


def _titles(messages: list[dict]) -> list[str]:
    return [m["title"] for m in messages]


# ---------------------------------------------------------------- 需求 1：真实事件流


def test_no_seed_messages(client: TestClient) -> None:
    """消息中心不再有写死的种子消息：初始为空，只有真实操作才会有内容。"""
    data = client.get("/api/console/state").json()
    assert data["messages"] == [], "初始消息流必须是空的（种子文案已删除）"
    assert data["unreadCount"] == 0


def test_device_control_records_real_event(client: TestClient) -> None:
    """打开客厅灯 → 消息里出现"打开了客厅灯"，且带上真实操作人与位置。"""
    before = client.get("/api/console/state").json()["messages"]
    assert before == []

    res = client.post("/api/console/devices/living_room_light/control",
                      json={"action": "turn_on", "value": 60}).json()
    assert res["ok"] is True

    messages = client.get("/api/console/state").json()["messages"]
    assert any("打开了客厅灯" in t for t in _titles(messages)), \
        f"操作后没有产生真实事件：{_titles(messages)}"
    evt = next(m for m in messages if "打开了客厅灯" in m["title"])
    assert evt["type"] == "device"
    assert "客厅" in evt["body"], "事件里要有设备位置"
    assert evt["actor"] == "我", "事件里要有真实操作人"
    assert evt["unread"] is True


def test_failed_operation_records_nothing(client: TestClient) -> None:
    """被拦下的操作不写事件：消息中心不放假消息。"""
    # 门锁未确认 → 后端拒绝执行
    res = client.post("/api/console/devices/front_door_lock/control",
                      json={"action": "turn_on"}).json()
    assert res["ok"] is False and res["requiresConfirm"] is True

    messages = client.get("/api/console/state").json()["messages"]
    assert messages == [], "没真正执行的操作不该留下事件"


def test_scene_activate_records_event_with_devices(client: TestClient) -> None:
    """执行场景 → 事件里写清楚联动了哪几台设备，而不是一句"已执行"。"""
    client.post("/api/console/scenes/home/activate", json={})
    messages = client.get("/api/console/state").json()["messages"]
    evt = next((m for m in messages if m["type"] == "scene"), None)
    assert evt is not None, f"场景执行后没有事件：{_titles(messages)}"
    assert "回家" in evt["title"]
    assert "客厅灯" in evt["body"], "联动了哪几台设备要在正文里说清楚"


def test_task_complete_records_event(client: TestClient) -> None:
    """完成任务 → 事件里出现"完成了…"，且带上联动设备。"""
    client.post("/api/console/tasks/t-plant/complete", json={})
    messages = client.get("/api/console/state").json()["messages"]
    evt = next((m for m in messages if m["type"] == "task"), None)
    assert evt is not None
    assert "完成了" in evt["title"] and "绿萝" in evt["title"]


def test_login_records_event(tmp_path) -> None:
    """登录本身是一条真实事件：刚进家门时消息中心不该空着（也不能是编的）。"""
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides.clear()
    try:
        with TestClient(app) as c:
            res = c.post("/api/auth/login", json={"username": "wo", "password": "demo1234"})
            assert res.status_code == 200
            token = res.json()["token"]
            data = c.get("/api/console/state",
                         headers={"Authorization": f"Bearer {token}"}).json()
        titles = _titles(data["messages"])
        assert any("登录了控制台" in t for t in titles), f"登录没留下事件：{titles}"
    finally:
        app.dependency_overrides.clear()
        set_account_store(None)


def test_events_are_family_scoped(tmp_path) -> None:
    """深圳家做的事，青岛家一条都看不到——消息也必须按家庭隔离。"""
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    sz = _TenantClient(SZ)
    qd = _TenantClient(QD)
    try:
        sz.post("/api/console/devices/living_room_light/control",
                json={"action": "turn_on", "value": 60})
        sz_titles = _titles(sz.get("/api/console/state").json()["messages"])
        assert any("打开了客厅灯" in t for t in sz_titles)

        qd_titles = _titles(qd.get("/api/console/state").json()["messages"])
        assert qd_titles == [], f"青岛家看到了深圳家的事件：{qd_titles}"
    finally:
        app.dependency_overrides.clear()
        set_account_store(None)


def test_mark_message_read(client: TestClient) -> None:
    """点消息条目 → 真标为已读，未读数下降；再点别家的 id → 404。"""
    client.post("/api/console/devices/living_room_light/control", json={"action": "turn_on"})
    state = client.get("/api/console/state").json()
    evt_id = state["messages"][0]["id"]
    assert state["unreadCount"] >= 1

    res = client.post(f"/api/console/messages/{evt_id}/read", json={}).json()
    assert res["ok"] is True

    after = client.get("/api/console/state").json()
    assert next(m for m in after["messages"] if m["id"] == evt_id)["unread"] is False
    assert after["unreadCount"] == state["unreadCount"] - 1


def test_mark_all_read(client: TestClient) -> None:
    """一键已读：返回被标记条数，之后未读数归零。"""
    client.post("/api/console/devices/living_room_light/control", json={"action": "turn_on"})
    client.post("/api/console/scenes/home/activate", json={})
    res = client.post("/api/console/messages/read-all", json={}).json()
    assert res["marked"] >= 2
    assert client.get("/api/console/state").json()["unreadCount"] == 0


def test_read_other_family_message_404(tmp_path) -> None:
    """拿别家的事件 id 标记已读 → 404（不透露"它存在"这件事）。"""
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    sz = _TenantClient(SZ)
    qd = _TenantClient(QD)
    try:
        sz.post("/api/console/devices/living_room_light/control", json={"action": "turn_on"})
        evt_id = sz.get("/api/console/state").json()["messages"][0]["id"]
        assert qd.post(f"/api/console/messages/{evt_id}/read", {}).status_code == 404
    finally:
        app.dependency_overrides.clear()
        set_account_store(None)


# ---------------------------------------------------------------- 需求 2：AI 提醒进今日任务


async def _create_reminder(family_id: str, content: str) -> dict:
    """走真实工具注册中心建提醒——和 AI 说话时走的是同一条路。"""
    when = (datetime.now() + timedelta(hours=3)).isoformat()
    result = await default_registry().call(
        "create_reminder",
        {"content": content, "scheduled_at": when, FAMILY_ARG: family_id},
    )
    assert result.ok, result.error
    return (result.data or {})["schedule"]


def test_ai_reminder_shows_in_today_tasks(client: TestClient) -> None:
    """跟 AI 加的提醒必须出现在今日任务框里（两套存储在展示层合并）。"""
    import asyncio

    schedule = asyncio.run(_create_reminder("family_sz", "晚上九点关灯"))

    tasks = client.get("/api/console/state").json()["tasks"]
    hit = next((t for t in tasks if t["id"] == schedule["schedule_id"]), None)
    assert hit is not None, f"提醒没进今日任务：{[t['title'] for t in tasks]}"
    assert hit["title"] == "晚上九点关灯"
    assert hit["category"] == "reminder"
    assert hit["source"] == "ai"
    assert hit["isSchedule"] is True   # 前端据此走"提醒"文案而不是"家务"


def test_reminder_complete_cancel_reopen(client: TestClient) -> None:
    """提醒的完整生命周期：完成 → 恢复 → 取消 → 恢复。

    reopen 曾经只认家务任务，提醒点"恢复"直接 404，
    前端只能弹"撤销失败"——这条就是为了防止它改回去。
    """
    import asyncio

    schedule = asyncio.run(_create_reminder("family_sz", "晚上九点关灯"))
    sid = schedule["schedule_id"]

    assert client.post(f"/api/console/tasks/{sid}/complete", json={}).json()["ok"] is True
    assert client.get("/api/console/state").json()["tasks"][0]["status"] == "done"

    # 恢复：以前这里 404
    res = client.post(f"/api/console/tasks/{sid}/reopen", json={})
    assert res.status_code == 200, f"提醒恢复失败（回归）：{res.status_code}"
    assert client.get("/api/console/state").json()["tasks"][0]["status"] == "pending"

    client.post(f"/api/console/tasks/{sid}/cancel", json={"reason": "改天再说"})
    assert client.get("/api/console/state").json()["tasks"][0]["status"] == "cancelled"

    assert client.post(f"/api/console/tasks/{sid}/reopen", json={}).status_code == 200


def test_reminder_delete(client: TestClient) -> None:
    """提醒能加也要能删：删掉之后从今日任务和提醒清单里一起消失。"""
    import asyncio

    schedule = asyncio.run(_create_reminder("family_sz", "晚上九点关灯"))
    sid = schedule["schedule_id"]
    assert any(t["id"] == sid for t in client.get("/api/console/state").json()["tasks"])

    res = client.delete(f"/api/console/schedules/{sid}")
    assert res.status_code == 200

    state = client.get("/api/console/state").json()
    assert not any(t["id"] == sid for t in state["tasks"]), "删了还挂在今日任务里"
    assert not any(s["schedule_id"] == sid for s in state["schedules"])


def test_delete_other_family_reminder_404(tmp_path) -> None:
    """拿别家的 schedule_id 来删 → 404，且一条都不动。"""
    import asyncio

    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    sz = _TenantClient(SZ)
    qd = _TenantClient(QD)
    try:
        schedule = asyncio.run(_create_reminder("family_sz", "晚上九点关灯"))
        assert qd.delete(f"/api/console/schedules/{schedule['schedule_id']}").status_code == 404
        assert any(t["id"] == schedule["schedule_id"]
                   for t in sz.get("/api/console/state").json()["tasks"])
    finally:
        app.dependency_overrides.clear()
        set_account_store(None)


def test_reminder_events_are_recorded(client: TestClient) -> None:
    """提醒的每一次状态变化都进消息中心，不只是"家务任务"才有记录。"""
    import asyncio

    schedule = asyncio.run(_create_reminder("family_sz", "晚上九点关灯"))
    sid = schedule["schedule_id"]
    client.post(f"/api/console/tasks/{sid}/complete", json={})

    titles = _titles(client.get("/api/console/state").json()["messages"])
    assert any("提醒已完成" in t for t in titles), f"提醒完成没留痕：{titles}"


# ---------------------------------------------------------------- 需求 3：可点元素的后端支撑


def _memory_service(tmp_path):
    """独立的记忆服务：长期记忆换到 tmp 库，语义/短期本来就是内存实现。

    绝不能让它落到 data/homemind.db——那里面是用户真记住的偏好。
    """
    from homemind.memory.long_term import LongTermMemory
    from homemind.memory.service import MemoryService

    return MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "mem.db")))


def test_habits_forget(client: TestClient, tmp_path) -> None:
    """习惯卡片可点 → "让 AI 忘掉"必须真的删掉（长期记忆 + 语义索引双清）。"""
    from homemind.memory.service import set_memory_service

    service = _memory_service(tmp_path)
    set_memory_service(service)
    try:
        service.write("member_001", "我习惯空调开 24 度", family_id="family_sz")
        habits = client.get("/api/console/habits").json()["habits"]
        assert habits, "AI 记住的习惯没出现在习惯页"
        key = habits[0]["key"]

        res = client.post("/api/console/habits/forget",
                          json={"memory_key": key}).json()
        assert res["ok"] is True

        after = client.get("/api/console/habits").json()
        assert not any(h["key"] == key for h in after["habits"]), "点了忘掉但还在"
        titles = _titles(client.get("/api/console/state").json()["messages"])
        assert any("忘掉了一条习惯" in t for t in titles)
    finally:
        set_memory_service(None)


def test_forget_other_family_habit_404(client: TestClient, tmp_path) -> None:
    """删别家的记忆 → 404，不泄露"这条记忆存在"。"""
    from homemind.memory.service import set_memory_service

    service = _memory_service(tmp_path)
    set_memory_service(service)
    try:
        # 记忆属于青岛家，但登录身份是深圳家
        service.write("member_001", "我习惯地暖 22 度", family_id="family_qd")
        res = client.post("/api/console/habits/forget",
                          json={"memory_key": "preference:地暖温度"})
        assert res.status_code == 404, "跨家庭删记忆居然成功了"
    finally:
        set_memory_service(None)
