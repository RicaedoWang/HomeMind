"""记忆接入 Agent 的回归测试（V0.5 第 2 步）。

这些用例锁住的是本项目最核心的差异化能力：
**记忆不只是能被查出来，而是能改变 Agent 的行为。**

    test_memory_changes_behavior        偏好影响工具参数（核心价值）
    test_preference_statement_not_run   陈述句不被当成指令执行（防误操作）
    test_behavior_follows_updated_memory 改口后行为跟着变
    test_permission_in_full_pipeline    权限在完整链路里生效
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.semantic import SemanticMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.memory.short_term import ShortTermMemory  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import get_device_states, reset_devices  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

FATHER = "member_father"
CHILD = "member_child"


@pytest.fixture()
def env(tmp_path):
    """构建带独立记忆库的 Agent 环境。"""
    reset_devices()
    reset_schedules()
    service = MemoryService(
        long_term=LongTermMemory(db_path=str(tmp_path / "mem.db")),
        semantic=SemanticMemory(),
        short_term=ShortTermMemory(),
    )
    graph = build_agent_graph(registry=default_registry(), memory=service)
    return graph, service


async def _run(graph, text, member_id=FATHER, role="L4", confirmed=False):
    state = initial_state(user_input=text, member_id=member_id,
                          member_role=role, confirmed=confirmed, trace_id="test")
    return await graph.ainvoke(state)


async def test_memory_changes_behavior(env):
    """用户第二句没提温度，Agent 必须按记忆里的偏好设定。

    这是整个记忆系统存在的意义——没有它，记忆只是个能查的数据库。
    """
    graph, _ = env
    await _run(graph, "我喜欢睡觉时空调26度")
    result = await _run(graph, "把空调打开")

    # 记忆确实被调用了
    assert result["applied_memories"], "记忆没有被应用到工具参数上"
    # 设备真的按偏好设成了 26 度
    assert get_device_states()["living_room_ac"].value == 26.0


async def test_preference_statement_not_run(env):
    """「我习惯空调开24度」是描述习惯，不能当成开空调的指令执行。

    判错一次，用户家的空调就会自己动——这类误操作比漏执行严重得多。
    """
    graph, _ = env
    result = await _run(graph, "我习惯空调开24度")

    assert result["tool_results"] == [], "偏好陈述绝不能触发工具执行"
    assert get_device_states()["living_room_ac"].power is False
    # 但记忆必须写进去了
    assert result["written_memories"], "偏好应当被记住"


async def test_behavior_follows_updated_memory(env):
    """用户改口后，同样一句「把空调打开」的行为必须跟着变。"""
    graph, _ = env
    await _run(graph, "我喜欢空调26度")
    await _run(graph, "把空调打开")
    assert get_device_states()["living_room_ac"].value == 26.0

    # 改口成 24 度
    await _run(graph, "我习惯空调开24度")
    await _run(graph, "把空调打开")
    assert get_device_states()["living_room_ac"].value == 24.0


async def test_memory_query_lists_habits(env):
    """「你现在记录了我的哪些习惯」必须列出记忆，而不是答非所问。

    这是用户实测撞出来的 bug：查询类意图此前没有处理，
    落进 chat 兜底话术回答"好的，我记下了"。
    """
    graph, _ = env
    await _run(graph, "我喜欢睡觉时空调26度")
    result = await _run(graph, "你现在记录了我的哪些习惯")

    answer = result["final_answer"]
    assert "26度" in answer, f"查询记忆应列出已记住的内容，实际：{answer}"
    assert "好的，我记下了" not in answer


async def test_memory_query_not_written_as_memory(env):
    """查询句本身不该被当成一条习惯记下来。"""
    graph, _ = env
    await _run(graph, "你现在记录了我的哪些习惯")
    result = await _run(graph, "查看记忆")
    # 全程没有写入任何记忆
    assert result["final_answer"].find("还没记住") != -1 or "0 条" in result["final_answer"]


async def test_memory_write_gets_acknowledged(env):
    """说完习惯必须得到"记住了 X"的明确回执，不能回闲聊模板。

    用户实测抓出来的 bug：说完"我习惯晚上十一点睡觉…"，
    AI 回的是"我在听。可以说「我喜欢…」"——他刚说完，你让他再说一遍？
    回执内容必须和实际入库的**完全一致**：应答里的"记住了 X"
    和 write_memory_node 用的是同一个抽取器，不许应答撒谎。
    """
    graph, _ = env
    result = await _run(graph, "我习惯空调开 24 度")

    assert result["written_memories"], "记忆没写进库"
    answer = result["final_answer"]
    assert "记住了" in answer, f"应有明确回执，实际：{answer}"
    assert "我在听" not in answer, "不能回闲聊模板"
    # 回执说的和实际存的是同一句话
    stored = result["written_memories"][0]["memory_value"]
    assert stored in answer, f"回执（{answer}）与入库内容（{stored}）不一致"


async def test_permission_in_full_pipeline(env):
    """家长的高权限记忆，儿童身份在完整链路里也召不到。"""
    graph, _ = env
    await _run(graph, "记住下周三下午三点开家长会", member_id=FATHER, role="L4")

    child = await _run(graph, "家长会是什么时候", member_id=CHILD, role="L2")
    assert child["recalled_memories"] == [], "儿童不应召回家长的高权限记忆"

    admin = await _run(graph, "家长会是什么时候", member_id=FATHER, role="L4")
    assert any("家长会" in m["memory_value"] for m in admin["recalled_memories"])
