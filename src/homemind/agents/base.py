"""子 Agent 抽象基类（V0.8 多 Agent 协同）。

两条设计原则：

1. **最小权限**：每个子 Agent 只能调用白名单内的工具。
   这是安全护栏之外的**第二道防线**——即使 Supervisor 分派错了，
   日程 Agent 也碰不到门锁。护栏挡"该不该做"，白名单挡"有没有资格做"。

2. **失败隔离**：run() 永不向外抛异常。内部异常一律转成 failed outcome，
   保证一个子 Agent 挂掉不会拖垮整轮对话（降级而不是崩盘）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentOutcome:
    """一个子 Agent 的执行结果。

    trace 字段用于可观测：谁跑了、跑了多久、调了几个工具、有没有降级。
    多 Agent 系统里没有这个，出问题就只能靠猜。
    """

    agent: str
    domain: str = ""
    ok: bool = True
    degraded: bool = False
    error: str | None = None
    answer: str | None = None          # 纯文本回答（知识 Agent 用）
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    applied_memories: list[dict[str, Any]] = field(default_factory=list)
    skipped_tasks: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "domain": self.domain,
            "ok": self.ok,
            "degraded": self.degraded,
            "error": self.error,
            "answer": self.answer,
            "tool_results": self.tool_results,
            "applied_memories": self.applied_memories,
            "skipped_tasks": self.skipped_tasks,
            "tool_calls": self.tool_calls,
            "elapsed_ms": self.elapsed_ms,
        }


class BaseAgent:
    """子 Agent 基类：声明领域、工具白名单，实现 handle()。"""

    name: str = "base"
    domain: str = "通用"
    description: str = ""
    allowed_tools: set[str] = set()
    handled_intents: set[str] = set()

    def can_handle(self, intent_type: str) -> bool:
        return intent_type in self.handled_intents

    def admits(self, tool_name: str | None) -> bool:
        """工具是否在本 Agent 的权限白名单内。"""
        return bool(tool_name) and tool_name in self.allowed_tools

    async def run(
        self,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        registry: Any,
    ) -> AgentOutcome:
        """执行入口：包一层计时与异常兜底，子类只实现 handle()。"""
        started = time.perf_counter()
        outcome = AgentOutcome(agent=self.name, domain=self.domain)
        try:
            outcome = await self.handle(state, tasks, registry, outcome)
        except Exception as exc:  # noqa: BLE001 - 子 Agent 异常必须被隔离，不能炸穿主链路
            outcome.ok = False
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.degraded = True
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    async def handle(
        self,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        registry: Any,
        outcome: AgentOutcome,
    ) -> AgentOutcome:
        raise NotImplementedError
