"""链路回归测试。

这些用例不是凑数的，每一条都对应 SRS 里的一条验收标准：
    test_turn_on_device            → FR-INT-001 意图识别
    test_delta_adjust              → FR-INT-002 多步任务（先查后设）
    test_high_risk_blocked         → FR-SEC-001 误操作率 = 0
    test_schedule_requires_confirm → FR-SEC-001 创建前回显确认
    test_idempotent_schedule       → NFR-REL-004 幂等
    test_multi_intent_partial_exec → FR-INT-002 部分执行 + 部分待确认
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.intent import split_multi_intent  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.agent.time_parser import parse_time_expr  # noqa: E402
from homemind.tools.calendar_server import get_schedules, reset_schedules  # noqa: E402
from homemind.tools.device_server import get_device_states, reset_devices  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前重置设备与日程，避免用例间互相污染。"""
    reset_devices()
    reset_schedules()
    yield
    reset_devices()
    reset_schedules()


@pytest.fixture()
def graph():
    return build_agent_graph(registry=default_registry())


async def _run(graph, text: str, confirmed: bool = False) -> dict:
    """跑一次完整链路。"""
    state = initial_state(user_input=text, confirmed=confirmed, trace_id="test")
    return await graph.ainvoke(state)


# ---------------- 时间解析 ----------------

def test_parse_tomorrow_clock():
    """'明天早上九点' 应解析为次日 09:00。"""
    base = datetime(2026, 10, 13, 10, 0)
    result, confidence = parse_time_expr("明天早上九点", base)
    assert result is not None
    assert result.date() == datetime(2026, 10, 14).date()
    assert result.hour == 9
    assert confidence >= 0.9


def test_parse_relative_minutes():
    """'20分钟后' 应解析为当前时间 +20 分钟。"""
    base = datetime(2026, 10, 13, 10, 0)
    result, _ = parse_time_expr("20分钟后", base)
    assert result == datetime(2026, 10, 13, 10, 20)


def test_parse_afternoon_converts_to_24h():
    """'下午三点' 必须转成 15:00，不能留成 3 点。"""
    base = datetime(2026, 10, 13, 10, 0)
    result, _ = parse_time_expr("下午三点", base)
    assert result is not None
    assert result.hour == 15


def test_parse_no_time_returns_none():
    """解析不出时间必须返回 None，交给上层澄清，禁止猜测。"""
    result, confidence = parse_time_expr("提醒我交房租")
    assert result is None
    assert confidence == 0.0


# ---------------- 意图 ----------------

def test_split_multi_intent():
    """多意图应按连接词切分。"""
    segments = split_multi_intent("关掉客厅灯，顺便提醒我明天下午三点开会")
    assert len(segments) == 2
    assert "客厅灯" in segments[0]
    assert "提醒" in segments[1]


# ---------------- 端到端链路 ----------------

async def test_turn_on_device(graph):
    """'把客厅灯打开' 应识别意图并真正改变设备状态。"""
    result = await _run(graph, "把客厅灯打开")
    assert result["intents"][0]["type"] == "device_control"
    assert get_device_states()["living_room_light"].power is True
    assert "已打开客厅灯" in result["final_answer"]


async def test_delta_adjust(graph):
    """'调低两度' 应先查当前值(26)再设为 24。"""
    result = await _run(graph, "客厅空调调低两度")
    device = get_device_states()["living_room_ac"]
    assert device.value == 24.0
    assert "24" in result["final_answer"]


async def test_high_risk_blocked(graph):
    """门锁必须走高危工具并拦截确认，绝不能直接执行。"""
    result = await _run(graph, "把大门门锁打开")
    tasks = result["tasks"]
    assert tasks[0]["tool_name"] == "lock_control"
    assert result["requires_confirm"] is True
    # 未确认 → 门锁状态不变（误操作率 = 0）
    assert get_device_states()["front_door_lock"].power is False


async def test_schedule_requires_confirm(graph):
    """创建日程必须先回显确认，未确认不得落库。"""
    result = await _run(graph, "提醒我明天早上九点交房租")
    assert result["requires_confirm"] is True
    assert len(get_schedules()) == 0  # 未确认，没创建

    # 用户确认后才真正创建
    confirmed = await _run(graph, "提醒我明天早上九点交房租", confirmed=True)
    assert len(get_schedules()) == 1
    assert "已创建提醒" in confirmed["final_answer"]


async def test_idempotent_schedule(graph):
    """相同请求重复执行，不应产生重复日程（幂等）。"""
    await _run(graph, "提醒我明天早上九点交房租", confirmed=True)
    second = await _run(graph, "提醒我明天早上九点交房租", confirmed=True)
    assert len(get_schedules()) == 1
    assert any(r.get("idempotent_hit") for r in second["tool_results"])


async def test_multi_intent_partial_exec(graph):
    """多意图：能做的先做，待确认的挂起回显。"""
    result = await _run(graph, "关掉客厅灯，顺便提醒我明天下午三点开会")
    # 关灯立即执行
    assert get_device_states()["living_room_light"].power is False
    # 日程挂起待确认
    assert len(get_schedules()) == 0
    assert result["requires_confirm"] is True
    assert "另外" in result["final_answer"]


async def test_missing_time_triggers_clarify(graph):
    """时间缺失时不得猜测，必须向用户澄清。"""
    result = await _run(graph, "提醒我交房租")
    assert result["requires_confirm"] is True
    assert len(get_schedules()) == 0


async def test_add_task_via_chat_creates_reminder(graph):
    """『新加今日任务，晚上八点喂猫』必须创建提醒，不能被当成闲聊。

    用户实测抓出来的 bug：这句话被判成 chat，AI 回了句"我在听"——
    任务没建、设备没动、界面没反应，三种沉默里最气人的一种。
    根因是"加任务"这句创建表达不在日程词表里。
    """
    result = await _run(graph, "新加今日任务，晚上八点喂猫")
    assert result["requires_confirm"] is True, \
        f"意图: {[i['type'] for i in result['intents']]}"
    assert len(get_schedules()) == 0  # 未确认不落库

    confirmed = await _run(graph, "新加今日任务，晚上八点喂猫", confirmed=True)
    assert len(get_schedules()) == 1
    schedule = get_schedules()[0]
    # 操作残留（"新加今日任务"）和时间片段（"晚上八点"）都不该混进提醒标题。
    # 时间已经由 scheduled_at 表达，标题里再留一份，回显就成了
    # 「即将创建：晚上八点喂猫（2026-10-20T20:00:00）」—— 同一句话两个时间，看着像冲突。
    assert schedule["content"] == "喂猫"
    assert schedule["scheduled_at"].endswith("T20:00:00")
    assert "已创建提醒" in confirmed["final_answer"]
