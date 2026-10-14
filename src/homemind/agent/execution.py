"""任务执行的公共原语（V0.8 抽出）。

为什么单独抽这一层：
V0.8 引入多 Agent 后，「单 Agent 主链路」和「各子 Agent」都要调工具。
如果两边各写一份执行逻辑，幂等键、增量调节、记忆补全这些治理细节
就会出现两份实现，迟早裂成"主链路有幂等、子 Agent 没有"这种缝——
**安全治理必须只有唯一一处实现**，否则"误操作率=0"这条验收标准无从审计。

所以这里放三类共享原语：
1. apply_memory_to_arguments —— 用记忆补全工具参数
2. resolve_delta             —— 增量调节（"调低两度"需要先查当前值）
3. execute_task              —— 带治理的单任务执行（超时/重试/熔断/幂等由注册中心负责）
"""

from __future__ import annotations

import re
from typing import Any

from ..tools.registry import ToolRegistry, make_idempotency_key


def apply_memory_to_arguments(
    arguments: dict[str, Any],
    recalled: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """用召回到的记忆补全工具参数。

    典型场景：用户说"把空调打开"，记忆里存着"喜欢 26 度"
    → 自动补成"调到 26 度"，而不是傻乎乎地只开机。

    这就是记忆系统从花架子变成真能力的一跃：
    记忆不只是能被查出来，而是能改变 Agent 的行为。
    """
    if arguments.get("device_name") != "空调":
        return arguments, None
    if arguments.get("action") != "turn_on":
        return arguments, None
    if arguments.get("value") is not None:
        return arguments, None  # 用户明说了温度，不覆盖

    for mem in recalled:
        text = mem.get("memory_value", "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*度", text)
        if match and ("空调" in text or "温度" in text):
            patched = dict(arguments)
            patched["action"] = "set_value"
            patched["value"] = float(match.group(1))
            return patched, {
                "memory_key": mem.get("memory_key"),
                "memory_value": text,
                "score": mem.get("score"),
                "applied_as": f"set_value {match.group(1)}度",
            }
    return arguments, None


async def resolve_delta(registry: ToolRegistry, arguments: dict[str, Any]) -> dict[str, Any]:
    """增量调节解析：先查当前值，再算出目标值。

    例："空调调低两度" → 查到当前 26 度 → 目标 24 度。
    这两步必须串行，所以放在执行层而不是规划层。
    """
    device_name = arguments.get("device_name")
    room = arguments.get("room")
    delta = float(arguments.get("delta", 1.0))

    query = await registry.call("device_control",
                                {"action": "query", "device_name": device_name, "room": room})
    current = 26.0
    if query.ok and query.data.get("device"):
        current = query.data["device"].get("value") or 26.0

    direction = -1 if arguments.get("action") == "delta_down" else 1
    new_value = round(float(current) + direction * delta, 1)
    return {"action": "set_value", "device_name": device_name, "room": room, "value": new_value}


async def execute_task(
    registry: ToolRegistry,
    task: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """执行单个任务，返回 (结果字典, 命中的记忆, 是否降级)。

    治理全部下沉到 ToolRegistry（超时/重试/熔断/幂等），这里只负责：
    参数预处理（增量解析 / 记忆补全）+ 生成幂等键 + 收集降级标记。
    """
    tool_name = task.get("tool_name")
    arguments = dict(task.get("arguments", {}))
    applied_mem: dict[str, Any] | None = None

    if arguments.get("action") in ("delta_up", "delta_down"):
        arguments = await resolve_delta(registry, arguments)
    else:
        arguments, applied_mem = apply_memory_to_arguments(
            arguments, state.get("recalled_memories", [])
        )

    idem_key = None
    spec = registry.get(tool_name)
    if spec and spec.mutating:
        # 带上原始输入，避免不同指令凑巧同参数被误判为重复请求
        idem_key = make_idempotency_key(tool_name, arguments,
                                        context=state.get("user_input", ""))

    result = await registry.call(tool_name, arguments, idempotency_key=idem_key)
    degraded = (not result.ok) or result.degraded
    return result.to_dict(), applied_mem, degraded
