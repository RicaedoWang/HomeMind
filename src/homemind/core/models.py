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
    """设备状态。

    历史上这是"Mock 设备层的内存态"，V1.3 起改为由 `DeviceStore`（SQLite）支撑——
    因为用户要能自己添加/改名/删除设备，代码里的常量一旦成了唯一真相，
    "删掉洗衣机"重启之后就复活了。内存态只是个快照，读写都要落库。

    可调范围（`min_value`/`max_value`/`unit`）是 V1.3 加的：
    用户自建的"咖啡机 3 档""加湿器 60%"需要知道自己能调到哪里，
    否则 `set_value` 收多少都照收，界面也不知道该画什么刻度。
    """

    device_id: str
    name: str
    room: str
    device_type: str
    power: bool = False
    value: float | None = None  # 温度 / 亮度等可调数值
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    builtin: bool = False  # 内置种子设备：可以改、可以删，只用来标注来源

    @property
    def adjustable(self) -> bool:
        """能不能调值。没给范围的设备只说开关，别说"调到 40%"。"""
        return self.min_value is not None and self.max_value is not None

    def clamp(self, value: float) -> float:
        """把用户给的值夹进合法范围。范围缺失时不夹（老数据没范围）。"""
        if self.min_value is None or self.max_value is None:
            return value
        return max(self.min_value, min(self.max_value, value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "room": self.room,
            "device_type": self.device_type,
            "power": self.power,
            "value": self.value,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "unit": self.unit,
            "builtin": self.builtin,
            "adjustable": self.adjustable,
        }
