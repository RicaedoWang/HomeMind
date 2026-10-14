"""多 Agent 协同（V0.8）。

对外暴露：
    Supervisor        调度中枢（分派 / 冲突检测 / 并发编排）
    DeviceAgent       设备管家
    ScheduleAgent     日程秘书
    KnowledgeAgent    知识问答（本地检索 + 大模型生成）
    build_multi_agent_graph  多 Agent 版状态图
"""

from .base import AgentOutcome, BaseAgent
from .device_agent import DeviceAgent
from .graph import build_multi_agent_graph
from .knowledge_agent import KnowledgeAgent
from .schedule_agent import ScheduleAgent
from .supervisor import Supervisor

__all__ = [
    "AgentOutcome",
    "BaseAgent",
    "DeviceAgent",
    "ScheduleAgent",
    "KnowledgeAgent",
    "Supervisor",
    "build_multi_agent_graph",
]
