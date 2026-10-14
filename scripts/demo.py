"""命令行 Demo：跑通完整链路并打印每一环。

运行方式（项目根目录）：
    pip install -r requirements.txt
    python scripts/demo.py

这个脚本的意义不是"打印几行字"，而是**逐环节暴露链路内部状态**——
意图是什么、规划出几个任务、调了哪个工具、耗时多少、有没有降级。
排查问题时你看的就是这份输出。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文会变成豆腐块，强制 UTF-8
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# 把 src 目录加入模块搜索路径，无需 pip install -e .
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.config import get_settings  # noqa: E402
from homemind.core.trace import get_logger, new_trace_id, set_trace_id, setup_logging  # noqa: E402
from homemind.tools.calendar_server import get_schedules  # noqa: E402
from homemind.tools.device_server import get_device_states  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

logger = get_logger("demo")

# 演示语料：覆盖单意图、多意图、增量调节、高危动作、时间澄清
CASES: list[tuple[str, bool]] = [
    ("把客厅灯打开", False),
    ("客厅空调调低两度", False),
    ("客厅空调现在多少度", False),
    ("提醒我明天早上九点交房租", False),
    ("提醒我明天早上九点交房租", True),   # 第二轮：用户确认后真正执行
    ("把大门门锁打开", False),            # 高危：必须拦截确认
    ("关掉客厅灯，顺便提醒我明天下午三点开会", False),
]


def hr(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


async def run_case(graph, text: str, confirmed: bool) -> None:
    """跑单条用例并打印链路细节。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    state = initial_state(user_input=text, confirmed=confirmed, trace_id=trace_id)

    print(f"\n[输入] {text}" + ("   <<已确认>>" if confirmed else ""))
    print(f"[trace] {trace_id}")

    result = await graph.ainvoke(state)

    # 1) 意图
    for item in result.get("intents", []):
        print(f"  [意图] {item['type']}  (置信度 {item['confidence']})  实体={item['entities']}")

    # 2) 任务规划
    for task in result.get("tasks", []):
        print(f"  [任务] {task.get('tool_name') or '(无工具)'}  参数={task.get('arguments')}"
              + ("  [需确认]" if task.get("requires_confirm") else ""))

    # 3) 工具调用
    for item in result.get("tool_results", []):
        flag = "  [命中幂等]" if item.get("idempotent_hit") else ""
        if item["ok"]:
            print(f"  [工具] {item['tool_name']}  OK  {item['duration_ms']}ms"
                  f"  -> {item['data'].get('message')}{flag}")
        else:
            print(f"  [工具] {item['tool_name']}  FAIL  {item['error']}  [已降级]")

    # 4) 应答
    print(f"  [应答] {result.get('final_answer')}")
    if result.get("degraded"):
        print("  [!] 本轮发生降级")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    hr(f"HomeMind V0.1 链路演示   模式: {'规则解析(Mock)' if settings.llm.use_mock else '大模型'}")
    print(f"环境: {settings.env}   工具超时: {settings.tool.timeout_seconds}s"
          f"   熔断阈值: {settings.tool.circuit_breaker_threshold}")

    registry = default_registry()
    graph = build_agent_graph(registry=registry)

    hr("MCP 工具清单（list_tools）")
    for tool in registry.list_tools():
        print(f"  - {tool['name']}: {tool['description']}")

    hr("链路执行")
    for text, confirmed in CASES:
        await run_case(graph, text, confirmed)

    hr("最终状态")
    print("[设备]")
    for dev in get_device_states().values():
        print(f"  {dev.name}({dev.room})  电源={'开' if dev.power else '关'}  值={dev.value}")
    print("\n[日程]")
    for sch in get_schedules():
        print(f"  {sch['schedule_id']}  {sch['scheduled_at']}  {sch['content']}")

    hr("演示结束")


if __name__ == "__main__":
    asyncio.run(main())
