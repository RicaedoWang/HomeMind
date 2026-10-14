"""多 Agent 版状态图（V0.8）。

图结构与单 Agent 版的差别只有两处，其余节点全部复用：

    START → recall_memory → parse_intent → supervisor → safety_gate
              ─┬─(可执行)→ execute_agents → respond → write_memory → END
               └─(无需执行)→ respond → write_memory → END

**关键：安全护栏的位置没变。**
多 Agent 最容易出的事故是"每个 Agent 各自校验、各自放行"，
结果护栏形同虚设。这里 Supervisor 只做分派决策（不碰工具），
工具执行仍然统一走 safety_gate 之后、且各子 Agent 共用同一份执行原语
（execution.execute_task），治理逻辑依旧只有一处。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from ..agent.graph import (
    parse_intent_node,
    recall_memory_node,
    respond_node,
    safety_gate_node,
    write_memory_node,
)
from ..agent.state import AgentState
from ..agent.intent import IntentParser
from ..core.trace import get_logger, span
from ..memory.service import MemoryService, get_memory_service
from ..tools.registry import ToolRegistry, default_registry
from .supervisor import Supervisor

logger = get_logger("agent.multi_graph")


async def supervisor_node(state: AgentState, supervisor: Supervisor) -> dict[str, Any]:
    """分派决策 + 冲突检测（都不碰工具，只是"想清楚谁干什么"）。"""
    with span("node.supervisor", logger, trace_id=state.get("trace_id", "-")):
        assignments, used_llm = await supervisor.dispatch_with_llm(
            state["user_input"], state.get("intents", []), state.get("tasks", [])
        )
        conflicts = supervisor.detect_conflicts(state.get("tasks", []))
        if conflicts:
            logger.warning("conflicts detected: %s", conflicts,
                           extra={"extra_fields": {"event": "agent.conflict"}})
        return {
            "agent_assignments": assignments,
            "conflicts": conflicts,
            "degraded": state.get("degraded", False) or used_llm,
        }


async def execute_agents_node(
    state: AgentState,
    supervisor: Supervisor,
    registry: ToolRegistry,
) -> dict[str, Any]:
    """并发拉起各子 Agent 执行，汇总结果。

    冲突任务在用户确认前一律挂起——冲突的两个操作单看都合法，
    合起来就是自相矛盾，执行了就是误操作。
    """
    results: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    degraded = state.get("degraded", False)

    with span("node.execute_agents", logger, trace_id=state.get("trace_id", "-")):
        # 冲突任务先剔除，交给 safety_gate 去要确认
        conflict_ids: set[str] = set()
        for conflict in state.get("conflicts", []):
            conflict_ids.update(conflict.get("task_ids", []))
        pending_tasks = [
            t for t in state.get("tasks", [])
            if not (t.get("task_id") in conflict_ids and not state.get("confirmed"))
        ]

        outcomes = await supervisor.run(state, pending_tasks, registry,
                                        state.get("agent_assignments"))

        agent_results: list[dict[str, Any]] = []
        for outcome in outcomes:
            agent_results.append(outcome.to_dict())
            results.extend(outcome.tool_results)
            applied.extend(outcome.applied_memories)
            if outcome.degraded:
                degraded = True
            # 纯回答型 Agent（知识问答）没有工具结果，把答案包成伪结果，
            # 让下游 respond 节点无需为多 Agent 特判
            if outcome.answer and not outcome.tool_results:
                results.append({
                    "tool_name": f"answer:{outcome.agent}",
                    "ok": True,
                    "data": {"message": outcome.answer},
                })

    return {
        "tool_results": results,
        "applied_memories": applied,
        "agent_results": agent_results,
        "degraded": degraded,
    }


def _route_multi(state: AgentState) -> str:
    """条件边：有分派就执行（知识问答没有任务也要执行）。"""
    if state.get("confirmed"):
        return "execute"
    if state.get("agent_assignments"):
        return "execute"
    return "respond"


def build_multi_agent_graph(
    parser: IntentParser | None = None,
    registry: ToolRegistry | None = None,
    memory: MemoryService | None = None,
    supervisor: Supervisor | None = None,
):
    """构建多 Agent 版状态图。依赖注入，便于测试替换。"""
    _parser = parser or IntentParser()
    _registry = registry or default_registry()
    _memory = memory or get_memory_service()
    _supervisor = supervisor or Supervisor()

    async def _recall(state: AgentState) -> dict[str, Any]:
        return await recall_memory_node(state, _memory)

    async def _parse(state: AgentState) -> dict[str, Any]:
        return await parse_intent_node(state, _parser)

    async def _supervise(state: AgentState) -> dict[str, Any]:
        return await supervisor_node(state, _supervisor)

    async def _execute(state: AgentState) -> dict[str, Any]:
        return await execute_agents_node(state, _supervisor, _registry)

    async def _respond(state: AgentState) -> dict[str, Any]:
        return await respond_node(state, _memory)

    async def _write(state: AgentState) -> dict[str, Any]:
        return await write_memory_node(state, _memory)

    graph = StateGraph(AgentState)
    graph.add_node("recall_memory", _recall)
    graph.add_node("parse_intent", _parse)
    graph.add_node("supervisor", _supervise)
    graph.add_node("safety_gate", safety_gate_node)
    graph.add_node("execute_agents", _execute)
    graph.add_node("respond", _respond)
    graph.add_node("write_memory", _write)

    graph.set_entry_point("recall_memory")
    graph.add_edge("recall_memory", "parse_intent")
    graph.add_edge("parse_intent", "supervisor")
    graph.add_edge("supervisor", "safety_gate")
    graph.add_conditional_edges(
        "safety_gate",
        _route_multi,
        {"execute": "execute_agents", "respond": "respond"},
    )
    graph.add_edge("execute_agents", "respond")
    graph.add_edge("respond", "write_memory")
    graph.add_edge("write_memory", END)

    return graph.compile()
