"""Agent 状态定义（对齐 FR-AGT-004 状态持久化）。

AgentState 是整条链路唯一的"真相来源"：
意图、任务、工具结果、最终应答全部挂在它上面，
配合 LangGraph 的 Checkpoint 就能实现中断恢复与多轮接续。
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """一次对话在 Agent 内部的完整流转状态。

    字段说明：
        trace_id       全链路追踪 ID（FR-OPS-001）
        user_input     用户原始输入
        member_id      家庭成员 ID，用于记忆与权限（FR-MEM-008）
        member_role    成员权限角色 L1~L4，决定能召回哪些记忆
        confirmed      用户是否已对高危动作确认（FR-SEC-001）
        intents        意图识别结果（FR-INT-001）
        tasks          任务规划结果（FR-INT-002）
        tool_results   工具执行结果（FR-TOOL-003）
        recalled_memories 本轮召回到的记忆（FR-MEM-005）
        applied_memories  实际被应用到工具参数上的记忆（记忆影响行为的证据）
        written_memories  本轮新写入的记忆（FR-MEM-004）
        requires_confirm 是否存在待确认的高危动作
        confirm_prompt 需要回显给用户的确认文案
        final_answer   最终应答
        degraded       是否发生了降级（供监控统计）
        agent_assignments 多 Agent 分派结果（V0.8，FR-MUL-001）
        agent_results     各子 Agent 的执行结果与 trace（V0.8，FR-MUL-003）
        conflicts         执行前检测到的操作冲突（V0.8，FR-MUL-004）
    """

    trace_id: str
    user_input: str
    member_id: str
    member_role: str
    confirmed: bool
    intents: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    recalled_memories: list[dict[str, Any]]
    applied_memories: list[dict[str, Any]]
    written_memories: list[dict[str, Any]]
    requires_confirm: bool
    confirm_prompt: str
    final_answer: str
    degraded: bool
    agent_assignments: list[dict[str, Any]]
    agent_results: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]


def initial_state(
    user_input: str,
    member_id: str = "member_001",
    member_role: str = "L4",
    confirmed: bool = False,
    trace_id: str = "-",
) -> AgentState:
    """构造初始状态，字段一次性初始化，避免节点里到处 get 默认值。"""
    return {
        "trace_id": trace_id,
        "user_input": user_input,
        "member_id": member_id,
        "member_role": member_role,
        "confirmed": confirmed,
        "intents": [],
        "tasks": [],
        "tool_results": [],
        "recalled_memories": [],
        "applied_memories": [],
        "written_memories": [],
        "requires_confirm": False,
        "confirm_prompt": "",
        "final_answer": "",
        "degraded": False,
        "agent_assignments": [],
        "agent_results": [],
        "conflicts": [],
    }
