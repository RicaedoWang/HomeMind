"""设备管家 Agent：负责一切设备控制。

它比"直接调工具"多做的两件事：
1. 参数完整性校验——没说清控制哪个设备就不动手（宁可问，不可瞎开）。
2. 越权工具拦截——拿到不属于自己的任务直接丢弃并记进 trace，而不是硬着头皮执行。
"""

from __future__ import annotations

from typing import Any

from ..core.models import TaskStatus
from ..agent.execution import execute_task
from .base import AgentOutcome, BaseAgent


class DeviceAgent(BaseAgent):
    name = "device_agent"
    domain = "设备控制"
    description = "开关设备、调节温度亮度、查询设备状态、门锁控制"
    allowed_tools = {"device_control", "device_list", "lock_control"}
    handled_intents = {"device_control"}

    async def handle(
        self,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        registry: Any,
        outcome: AgentOutcome,
    ) -> AgentOutcome:
        for task in tasks:
            tool_name = task.get("tool_name")

            # 白名单之外的工具：丢弃 + 留痕，绝不执行
            if not self.admits(tool_name):
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "工具不在 device_agent 白名单内",
                })
                continue

            # 澄清类任务（时间/参数缺失）不执行
            if task.get("status") == TaskStatus.AWAITING_CONFIRM.value:
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "参数不完整，等待用户澄清",
                })
                continue

            # 需二次确认但用户尚未确认 → 挂起（FR-SEC-001）
            if task.get("requires_confirm") and not state.get("confirmed"):
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "高危动作待用户确认",
                })
                continue

            arguments = task.get("arguments", {})
            if not arguments.get("device_name"):
                # 没说清控制谁：不猜。猜错了就是误操作。
                outcome.skipped_tasks.append({
                    "task_id": task.get("task_id"),
                    "tool_name": tool_name,
                    "reason": "未指明设备，需澄清",
                })
                continue

            result_dict, applied_mem, degraded = await execute_task(registry, task, state)
            outcome.tool_results.append(result_dict)
            outcome.tool_calls += 1
            if applied_mem:
                outcome.applied_memories.append(applied_mem)
            if degraded:
                outcome.degraded = True

        return outcome
