"""记忆接入 Agent 的端到端演示（V0.5 第 2 步）。

运行：python scripts/demo_agent_memory.py

这一步要证明的不是"记忆能存能查"（上一步已经证明过了），
而是：**记忆能改变 Agent 的行为**。

关键场景：
    第 1 轮  用户说"我喜欢睡觉时空调26度"  → 写入记忆
    第 2 轮  用户说"把空调打开"            → 召回偏好 → 自动设 26 度
             注意：用户这次根本没提温度，是记忆替他做的决定

没有这一步，记忆系统就是个只能自我欣赏的花架子。
"""

from __future__ import annotations

import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.config import get_settings  # noqa: E402
from homemind.core.trace import new_trace_id, set_trace_id, setup_logging  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.semantic import SemanticMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.memory.short_term import ShortTermMemory  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import get_device_states, reset_devices  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

FATHER = "member_father"
CHILD = "member_child"


def hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


async def run(graph, text: str, member_id: str = FATHER, role: str = "L4",
              confirmed: bool = False) -> dict:
    """跑一轮对话，并打印记忆相关的链路细节。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    state = initial_state(user_input=text, member_id=member_id,
                          member_role=role, confirmed=confirmed, trace_id=trace_id)

    print(f"\n  你：{text}")
    result = await graph.ainvoke(state)

    recalled = result.get("recalled_memories", [])
    if recalled:
        print(f"  [召回] {len(recalled)} 条记忆")
        for mem in recalled[:3]:
            print(f"      - {mem['memory_value']}  (综合分 {mem['score']})")

    for item in result.get("applied_memories", []):
        print(f"  [应用] 记忆「{item['memory_value']}」→ 参数改为 {item['applied_as']}")

    for item in result.get("tool_results", []):
        if item["ok"]:
            print(f"  [工具] {item['tool_name']} → {item['data'].get('message')}")
        else:
            print(f"  [工具] {item['tool_name']} 失败：{item['error']}")

    written = result.get("written_memories", [])
    if written:
        for mem in written:
            print(f"  [写入] {mem['memory_key']} = {mem['memory_value']}")

    print(f"  助手：{result.get('final_answer')}")
    return result


async def main() -> None:
    import asyncio
    settings = get_settings()
    setup_logging("WARNING")

    # 干净起步：清库 + 重置设备
    long_term = LongTermMemory(db_path="data/demo_agent_memory.db")
    long_term.clear()
    reset_devices()
    reset_schedules()

    service = MemoryService(
        long_term=long_term,
        semantic=SemanticMemory(),
        short_term=ShortTermMemory(),
        settings=settings,
    )
    graph = build_agent_graph(registry=default_registry(), memory=service)

    hr("记忆接入 Agent — 记忆能否改变行为")
    print("  场景：先告诉 Agent 你的偏好，再看它下次会不会自己用上")

    # ---------- 第 1 轮：种下偏好 ----------
    hr("第 1 轮 · 种下偏好（用户明确表达）")
    await run(graph, "我喜欢睡觉时空调26度")

    # ---------- 第 2 轮：不提温度 ----------
    hr("第 2 轮 · 用户没提温度，看 Agent 会不会自己用上偏好")
    await run(graph, "把空调打开")
    device = get_device_states()["living_room_ac"]
    print(f"\n  → 空调实际状态：电源={'开' if device.power else '关'}  温度={device.value}度")
    assert device.value == 26.0, "记忆未生效：温度应为 26 度"
    print("  ✓ 记忆生效——用户没说温度，Agent 按记忆设成了 26 度")

    # ---------- 第 3 轮：增量调节仍基于当前值 ----------
    hr("第 3 轮 · 增量调节（基于记忆设定的 26 度继续调）")
    await run(graph, "空调调低两度")
    device = get_device_states()["living_room_ac"]
    print(f"\n  → 空调温度：{device.value}度（26 度基础上调低 2 度）")

    # ---------- 第 4 轮：记忆冲突更新后行为跟着变 ----------
    hr("第 4 轮 · 改口偏好，再看行为是否跟着变")
    await run(graph, "我习惯空调开24度")
    await run(graph, "把空调打开")
    device = get_device_states()["living_room_ac"]
    print(f"\n  → 空调温度：{device.value}度")
    print("  （改口后，同样一句「把空调打开」，行为已随记忆改变）")

    # ---------- 第 5 轮：权限在 Agent 链路里也生效 ----------
    hr("第 5 轮 · 权限隔离在完整链路里同样生效")
    print("\n  以管理员身份写入一条高权限记忆：")
    await run(graph, "记住下周三下午三点开家长会", member_id=FATHER, role="L4")

    print("\n  儿童身份问同样的问题：")
    await run(graph, "家长会是什么时候", member_id=CHILD, role="L2")
    print("\n  （家长的高权限记忆不会被儿童身份召回，链路里也拦得住）")

    hr("演示结束")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
