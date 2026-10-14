"""V0.8 多 Agent 协同演示。

跑法：
    python scripts/demo_multi_agent.py

默认强制 mock 模型（不烧 token）。想看真模型效果：
    HM_USE_MOCK_LLM=false python scripts/demo_multi_agent.py

六段演示：
    1. 单领域分派       —— 设备控制只由设备管家接手
    2. 多领域并发       —— 设备 + 日程两个 Agent 同时干活
    3. 知识问答         —— 知识 Agent 检索说明书并作答，不碰任何设备
    4. 越权拦截         —— 塞给日程 Agent 一个门锁任务，被工具白名单丢弃
    5. 冲突检测         —— 同一设备被要求开又被要求关，执行前拦下
    6. 失败隔离         —— 一个 Agent 崩了，其他 Agent 照常返回结果
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 默认用 mock 模型，避免每跑一次 demo 都烧真 token
os.environ.setdefault("HM_USE_MOCK_LLM", "true")

from homemind.agent.state import initial_state  # noqa: E402
from homemind.agents import Supervisor  # noqa: E402
from homemind.core.models import Intent, IntentType  # noqa: E402
from homemind.agents.base import AgentOutcome, BaseAgent  # noqa: E402
from homemind.agents.graph import build_multi_agent_graph  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class BoomAgent(BaseAgent):
    """故意崩掉的子 Agent，用来验证失败隔离。"""

    name = "boom_agent"
    domain = "故障演示"
    description = "每次运行必定抛异常"
    allowed_tools = set()
    handled_intents = {"device_control"}

    async def handle(self, state, tasks, registry, outcome: AgentOutcome) -> AgentOutcome:
        raise RuntimeError("模拟子 Agent 内部崩溃")


def show_trace(result: dict) -> None:
    """打印各子 Agent 的执行 trace——多 Agent 系统没有这个就是黑盒。"""
    traces = result.get("agent_results", [])
    if not traces:
        print("      （无子 Agent 执行）")
        return
    for t in traces:
        flag = "降级" if t.get("degraded") else "正常"
        print(f"      · {t['agent']:<16} {flag}  工具调用 {t['tool_calls']} 次  "
              f"耗时 {t['elapsed_ms']}ms"
              + (f"  跳过 {len(t['skipped_tasks'])} 个任务" if t.get("skipped_tasks") else ""))
        for s in t.get("skipped_tasks", []):
            print(f"          ↳ 跳过 {s.get('tool_name')}：{s.get('reason')}")
        if t.get("error"):
            print(f"          ↳ 错误：{t['error']}")


async def run(graph, text: str, title: str, confirmed: bool = False) -> dict:
    print(f"\n{'=' * 66}")
    print(f"{title}")
    print(f"{'=' * 66}")
    print(f"  用户说：{text}" + ("  （用户已确认）" if confirmed else ""))
    state = initial_state(text, trace_id=uuid.uuid4().hex[:8], confirmed=confirmed)
    result = await graph.ainvoke(state)

    assignments = result.get("agent_assignments", [])
    if assignments:
        print("  分派结果：")
        for a in assignments:
            print(f"      → {a['agent']}（{a['domain']}） 任务 {len(a['task_ids'])} 个")
            print(f"        理由：{a['reason']}")
    else:
        print("  分派结果：无（纯闲聊或记忆类，不调工具）")

    conflicts = result.get("conflicts", [])
    if conflicts:
        print("  冲突检测：")
        for c in conflicts:
            print(f"      ⚠ {c['detail']}")

    print("  执行 trace：")
    show_trace(result)
    print(f"  最终应答：{result.get('final_answer')}")
    return result


async def main() -> None:
    memory = MemoryService(long_term=LongTermMemory(db_path="data/demo_multi.db"))
    graph = build_multi_agent_graph(memory=memory)

    print("\n" + "#" * 66)
    print("# HomeMind V0.8 · 多 Agent 协同演示")
    print("#" * 66)

    # 1. 单领域
    await run(graph, "把客厅的灯关掉", "【1/6】单领域分派：设备控制")

    # 2. 多领域并发
    await run(graph, "把空调打开，另外提醒我明天早上八点开会",
              "【2/6】多领域并发：设备 + 日程两个 Agent 同时干活")

    # 2b. 用户回复"确认"后的第二轮
    await run(graph, "把空调打开，另外提醒我明天早上八点开会",
              "【2b/6】用户回复确认后的第二轮：两个 Agent 真正并发执行", confirmed=True)

    # 3. 知识问答（mock 模式下没有模型润色，会走"如实给出资料原文"的降级分支，属预期）
    await run(graph, "空调滤网要多久洗一次？", "【3/6】知识问答：说明书检索，不碰设备")
    print("  注：mock 模式下无模型润色，Agent 降级为直接给出资料原文——"
          "这比编造说明书内容强。")

    # 4. 越权拦截：手工构造一个"日程 Agent 拿到门锁任务"的分派
    print(f"\n{'=' * 66}")
    print("【4/6】越权拦截：把门锁任务硬塞给日程 Agent")
    print(f"{'=' * 66}")
    from homemind.agents.schedule_agent import ScheduleAgent

    agent = ScheduleAgent()
    bad_task = {
        "task_id": "task_bad",
        "intent": {"type": "device_control"},
        "tool_name": "lock_control",
        "arguments": {"action": "unlock", "device_name": "门锁"},
        "status": "pending",
        "requires_confirm": True,
    }
    outcome = await agent.run({"user_input": "开门", "confirmed": True},
                              [bad_task], default_registry())
    print(f"  日程 Agent 白名单：{sorted(agent.allowed_tools)}")
    print(f"  实际工具调用次数：{outcome.tool_calls}（应为 0）")
    print(f"  被拦截原因：{[s['reason'] for s in outcome.skipped_tasks]}")

    # 5. 冲突检测：注入一个会产出矛盾任务的 parser，走完整链路验证
    #    （不能手工伪造 state——parse_intent 节点会重新解析并覆盖它）
    class StubParser:
        """注入用：直接返回构造好的意图，跳过规则与模型解析。"""

        def __init__(self, intents: list) -> None:
            self._intents = intents

        async def parse(self, text: str) -> list:
            return self._intents

    on_intent = Intent(type=IntentType.DEVICE_CONTROL, confidence=0.95,
                       raw_text="把客厅灯打开", entities={"device_name": "客厅灯", "action": "turn_on"})
    off_intent = Intent(type=IntentType.DEVICE_CONTROL, confidence=0.95,
                        raw_text="再把客厅灯关掉", entities={"device_name": "客厅灯", "action": "turn_off"})
    conflict_graph = build_multi_agent_graph(parser=StubParser([on_intent, off_intent]),
                                             memory=memory)

    result = await run(conflict_graph, "把客厅灯打开再关掉",
                       "【5/6】冲突检测：同一设备被要求开又被要求关（走完整链路）")
    print(f"  实际执行的工具调用：{[r['tool_name'] for r in result.get('tool_results', [])]}"
          f"（冲突任务应被挂起，列表应为空）")

    # 6. 失败隔离
    print(f"\n{'=' * 66}")
    print("【6/6】失败隔离：一个 Agent 崩溃，其他 Agent 照常返回")
    print(f"{'=' * 66}")
    broken = Supervisor(agents=[BoomAgent()])
    boom_state = {
        "user_input": "把客厅灯打开", "trace_id": "-",
        "intents": [{"type": "device_control", "confidence": 0.9, "raw_text": "把客厅灯打开"}],
        "recalled_memories": [], "confirmed": True,
    }
    boom_tasks = [{"task_id": "t_boom", "intent": boom_state["intents"][0],
                   "tool_name": "device_control",
                   "arguments": {"action": "turn_on", "device_name": "客厅灯"},
                   "status": "pending", "requires_confirm": False}]
    outcomes = await broken.run(boom_state, boom_tasks, default_registry())
    for o in outcomes:
        print(f"      · {o.agent}: ok={o.ok} degraded={o.degraded} error={o.error}")
    print("  结论：异常被隔离在子 Agent 内部，主链路不会崩。")

    print(f"\n{'#' * 66}")
    print("# 演示结束")
    print("#" * 66)


if __name__ == "__main__":
    asyncio.run(main())
