"""核心数据模型。

所有跨模块流转的数据结构都定义在这里，避免各模块各写一套 dict 导致字段名对不上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntentType(str, Enum):
    """意图类型枚举。

    V0.1 只落地 device_control / schedule / chat 三类，
    knowledge 在 V0.8 接 RAG 后启用。
    """

    DEVICE_CONTROL = "device_control"
    SCHEDULE = "schedule"
    KNOWLEDGE = "knowledge"
    MEMORY_WRITE = "memory_write"  # 用户在表达偏好/习惯，不是在下指令
    MEMORY_QUERY = "memory_query"  # 用户想查看 Agent 记住了什么
    CHAT = "chat"
    UNKNOWN = "unknown"


class TaskStatus(str, Enum):
    """任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    AWAITING_CONFIRM = "awaiting_confirm"


@dataclass
class Intent:
    """意图识别结果。"""

    type: IntentType
    confidence: float
    raw_text: str
    entities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "entities": self.entities,
        }


@dataclass
class Task:
    """任务规划产出的单个任务。"""

    task_id: str
    intent: Intent
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    # 高危动作需要二次确认（对齐 FR-SEC-001）
    requires_confirm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "intent": self.intent.to_dict(),
            "status": self.status.value,
            "depends_on": self.depends_on,
            "requires_confirm": self.requires_confirm,
        }


@dataclass
class ToolResult:
    """工具调用结果（对齐 FR-TOOL-003 治理要求）。"""

    tool_name: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    # 降级标记：主链路失败后走了备选方案
    degraded: bool = False
    # 幂等标记：命中幂等键，未真正重复执行
    idempotent_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "degraded": self.degraded,
            "idempotent_hit": self.idempotent_hit,
        }


@dataclass
class DeviceState:
    """设备状态（Mock 设备层的内存态，真实设备接入后由设备层维护）。"""

    device_id: str
    name: str
    room: str
    device_type: str
    power: bool = False
    value: float | None = None  # 温度 / 亮度等可调数值

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "room": self.room,
            "device_type": self.device_type,
            "power": self.power,
            "value": self.value,
        }
