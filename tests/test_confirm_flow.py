"""回显确认的第二轮回归（对齐 FR-SEC-001、FR-INT-004）。

回归自 2026-10-20 实测：回显文案写着「确认请回复「确认」」，
用户就真的回了一个"确认"——结果**什么都没执行**，应答却回
"任务已创建：去遛狗，今晚七点。"

两处毛病叠在一起：
1. "确认"两个字单独进来，意图解析判成聊天，tasks 为空，执行层无事可做；
2. 应答层翻到历史里那句"即将创建：去遛狗"，就当成了已完成，张口就编。

为什么端到端验收 28/28 全过也没抓到：验收脚本走的是"重发原句 + confirmed=true"，
那正是**前端点按钮**的路径。按钮能用、手打不能，两条路两种结果。
提示文案却明确引导用户去手打——这等于把用户往坑里领。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import (  # noqa: E402
    _is_pure_confirm, reset_pending, reset_tasks,
)
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.tools.calendar_server import get_schedules, reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    """设备、日程、任务、待确认缓存全是模块级状态，不重置就不可复现。"""
    reset_devices()
    reset_schedules()
    reset_tasks()
    reset_pending()
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()
    reset_pending()


@pytest.fixture()
def client(tmp_path):
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides[current_user] = lambda: AuthUser(
        family_id="family_sz", member_id="member_001", role="L4", name="我", city="深圳",
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)


def _ask(client: TestClient, text: str, confirmed: bool = False) -> dict:
    return client.post(
        "/api/console/assistant", json={"text": text, "confirmed": confirmed}
    ).json()


# ---------------- 核心回归：手打「确认」必须真执行 ----------------

def test_typed_confirm_really_creates_reminder(client: TestClient) -> None:
    """照着提示手打"确认"，必须真的建出提醒，而不是只管嘴上答应。

    修复前：第一轮回显正常，第二轮判成 chat，日程表仍是 0 条，
    但应答说"任务已创建"。用户以为建好了，回头翻任务列表什么都没有。
    """
    first = _ask(client, "晚上八点提醒我给花浇水")
    assert first["requiresConfirm"] is True
    assert "即将创建" in first["answer"]
    assert len(get_schedules()) == 0, "未确认就落库，等于回显确认形同虚设"

    second = _ask(client, "确认", confirmed=True)

    assert len(get_schedules()) == 1, "手打「确认」必须真的执行"
    schedule = get_schedules()[0]
    assert schedule["content"] == "给花浇水"
    # 应答与实际必须一致：不能出现"已创建"但库里空的撒谎
    assert "给花浇水" in second["answer"]
    assert second["actions"], "动作卡来自真实工具回执，执行了就该有"


def test_typed_confirm_answer_is_not_fabricated(client: TestClient) -> None:
    """反向保险：如果真没执行，应答不许说"已创建"。

    这条与上一条互补——上一条防"执行了没反映"，这条防"没执行却说做了"。
    """
    _ask(client, "晚上八点提醒我给花浇水")           # 挂起待确认
    assert len(get_schedules()) == 0

    # 说一句纯闲聊，确认它是"真的没执行"，而不是悄悄补执行了上一步
    chat = _ask(client, "今天天气不错")
    assert len(get_schedules()) == 0, "闲聊不该顺手把上一步确认掉"
    assert "已创建" not in chat["answer"]


# ---------------- 两种确认路径必须等价 ----------------

def test_button_path_and_typed_path_equivalent(client: TestClient) -> None:
    """点按钮（重发原句）与手打"确认"应产出同一条提醒。"""
    _ask(client, "晚上八点提醒我给花浇水")

    # 前端「确认执行」的真实行为：原句 + confirmed=true
    via_button = _ask(client, "晚上八点提醒我给花浇水", confirmed=True)
    assert len(get_schedules()) == 1
    assert via_button["actions"]

    reset_schedules()
    reset_pending()

    _ask(client, "晚上八点提醒我给花浇水")
    _ask(client, "确认", confirmed=True)
    assert len(get_schedules()) == 1, "手打路径结果应与按钮路径一致"


def test_pending_cleared_after_execution(client: TestClient) -> None:
    """执行完必须清掉待确认原文。

    否则用户过十分钟随口一句"好"，会把十分钟前那句操作补执行——
    这是最典型的"设备莫名其妙自己动了"。
    """
    _ask(client, "晚上八点提醒我给花浇水")
    _ask(client, "确认", confirmed=True)
    assert len(get_schedules()) == 1

    _ask(client, "好", confirmed=True)      # 已经没有待确认项了
    assert len(get_schedules()) == 1, "待确认原文没清干净，事后一句「好」又建了一条"


# ---------------- 纯确认词的判定边界 ----------------

@pytest.mark.parametrize("text", ["确认", "确定", "好的", "OK", "是的。", "可以"])
def test_pure_confirm_accepted(text: str) -> None:
    assert _is_pure_confirm(text) is True


@pytest.mark.parametrize("text", [
    "确认一下明天的天气",   # 带内容，不是单纯确认
    "好想睡觉",            # 以"好"开头但不是确认
    "可以帮我关灯吗",       # 疑问句
    "我想确认下家里的灯",
    "",
])
def test_pure_confirm_rejected(text: str) -> None:
    """判定必须窄：认错了就把用户一句正经话当成"执行上一步"。"""
    assert _is_pure_confirm(text) is False
