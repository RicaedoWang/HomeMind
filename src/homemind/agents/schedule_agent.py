"""日程秘书 Agent：负责提醒与日程。

日程是**创建类**操作，误创建的代价是用户被莫名其妙的提醒轰炸，
所以本 Agent 对时间字段零容忍：解析不出时间就要求澄清，绝不替用户编一个。
"""

from __future__ import annotations

from typing import Any

from ..core.models import TaskStatus
from ..agent.execution import execute_task
from .base import AgentOutcome, BaseAgent


class ScheduleAgent(BaseAgent):
    name = "schedule_agent"
    domain = "日程提醒"
    description = "创建提醒、查询日程，含时间解析校验"
    allowed_tools = {"create_reminder", "list_schedule"}
    handled_intents = {"schedule"}

    async def handle(
        self,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        registry: Any,
        outcome: AgentOutcome,
    ) -> AgentOutcome:
        for task in tasks:
            tool_name = task.get("tool_name")

            if not self.admits(tool_name):
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "工具不在 schedule_agent 白名单内",
                })
                continue

            # 时间没解析出来 → 强制澄清，绝不猜一个时间塞进去
            if task.get("status") == TaskStatus.AWAITING_CONFIRM.value:
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": task.get("clarify") or "时间不明确，等待澄清",
                })
                continue

            if task.get("requires_confirm") and not state.get("confirmed"):
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "创建类操作待用户回显确认",
                })
                continue

            arguments = task.get("arguments", {})
            if not arguments.get("content") or not arguments.get("scheduled_at"):
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "缺少内容或时间，需澄清",
                })
                continue

            result_dict, applied_mem, degraded = await execute_task(registry, task, state)
            outcome.tool_results.append(result_dict)
            outcome.tool_calls += 1
            if degraded:
                outcome.degraded = True

        return outcome
