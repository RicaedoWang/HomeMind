"""Supervisor：多 Agent 协同的调度中枢（V0.8 核心）。

它干三件事，且**只干三件事**：

1. dispatch   —— 按意图类型把任务分派给对应领域的子 Agent（规则优先，LLM 兜底）
2. detect     —— 执行前做冲突检测（同一设备被要求开又被要求关）
3. run        —— 并发拉起各子 Agent，收集结果；任一 Agent 失败不影响其他

它**不干**的事：不碰工具、不做安全审批。
安全护栏仍然在 safety_gate 唯一节点上——多 Agent 的引入**没有削弱**安全，
因为护栏在工具执行之前，且所有子 Agent 共用同一份执行原语。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.llm import get_llm_client
from ..core.trace import get_logger, span
from .base import AgentOutcome, BaseAgent
from .device_agent import DeviceAgent
from .schedule_agent import ScheduleAgent
from .knowledge_agent import KnowledgeAgent

logger = get_logger("agent.supervisor")

# 相反动作对：用于冲突检测
_OPPOSITE = {"turn_on": "turn_off", "turn_off": "turn_on"}


class Supervisor:
    """调度中枢：持有子 Agent 列表，负责分派与并发编排。"""

    def __init__(self, agents: list[BaseAgent] | None = None) -> None:
        self.agents: list[BaseAgent] = agents or [
            DeviceAgent(),
            ScheduleAgent(),
            KnowledgeAgent(),
        ]
        self._by_name = {a.name: a for a in self.agents}

    # ---------------- 分派 ----------------

    def _pick(self, intent_type: str, tool_name: str | None) -> BaseAgent | None:
        """挑一个既认领该意图、又有该工具权限的 Agent。"""
        for agent in self.agents:
            if agent.can_handle(intent_type) and (tool_name is None or agent.admits(tool_name)):
                return agent
        # 知识问答没有任务，但也要有人接——按意图单独认领
        for agent in self.agents:
            if agent.can_handle(intent_type):
                return agent
        return None

    def dispatch(
        self,
        intents: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把意图与任务分派给子 Agent，返回 assignments。

        assignments: [{agent, domain, task_ids, intent_types, reason}]
        """
        buckets: dict[str, dict[str, Any]] = {}

        # 纯问答型意图（如 knowledge）不产生任务，但必须由对应 Agent 接手
        for intent in intents:
            itype = intent.get("type")
            agent = self._pick(itype, None)
            if agent is None:
                continue
            bucket = buckets.setdefault(agent.name, {
                "agent": agent.name, "domain": agent.domain,
                "task_ids": [], "intent_types": [], "reason": [],
            })
            if itype not in bucket["intent_types"]:
                bucket["intent_types"].append(itype)
            bucket["reason"].append(f"{itype} → {agent.name}（领域匹配）")

        for task in tasks:
            itype = task.get("intent", {}).get("type", "unknown")
            tool_name = task.get("tool_name")
            agent = self._pick(itype, tool_name)
            if agent is None:
                logger.warning("no agent claimed task %s (intent=%s, tool=%s)",
                               task.get("task_id"), itype, tool_name)
                continue
            bucket = buckets.setdefault(agent.name, {
                "agent": agent.name, "domain": agent.domain,
                "task_ids": [], "intent_types": [], "reason": [],
            })
            bucket["task_ids"].append(task.get("task_id"))
            if itype not in bucket["intent_types"]:
                bucket["intent_types"].append(itype)
            bucket["reason"].append(f"{tool_name} → {agent.name}（白名单 {sorted(agent.allowed_tools)}）")

        for bucket in buckets.values():
            bucket["reason"] = "；".join(bucket["reason"])
        return list(buckets.values())

    async def dispatch_with_llm(
        self,
        user_input: str,
        intents: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """规则分派 + 大模型兜底。返回 (assignments, 是否用了模型)。

        什么时候需要模型兜底：意图被判成 unknown/chat 但文本里明显在讲设备或日程。
        规则分不出 → 让模型选领域；模型也拿不准或不可用 → 按规则结果走，不硬猜。
        """
        assignments = self.dispatch(intents, tasks)
        if assignments:
            return assignments, False

        client = get_llm_client()
        if not client.enabled:
            return assignments, False

        catalog = "\n".join(f"- {a.name}：{a.description}" for a in self.agents)
        system = ("你是任务调度器。根据用户这句话，从下面的 Agent 里挑最合适的一个，"
                  "只返回 JSON：{\"agent\": \"<名字>\"}。都不合适就返回 {\"agent\": null}。")
        prompt = f"可选 Agent：\n{catalog}\n\n用户说：{user_input}"
        choice = await client.chat_json(prompt, system=system, max_tokens=80)

        picked = (choice or {}).get("agent")
        if picked in self._by_name:
            agent = self._by_name[picked]
            return [{
                "agent": agent.name, "domain": agent.domain,
                "task_ids": [t.get("task_id") for t in tasks],
                "intent_types": [i.get("type") for i in intents],
                "reason": f"规则未命中，由大模型判定为 {agent.name}",
            }], True
        return assignments, False

    # ---------------- 冲突检测 ----------------

    def detect_conflicts(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同一设备被要求执行相反动作 → 冲突，必须先跟用户确认。

        为什么在执行前检测：冲突的两个操作本身都"合法"，
        但合在一起就是自相矛盾，执行了就是误操作。
        """
        seen: dict[str, tuple[str, str]] = {}
        conflicts: list[dict[str, Any]] = []
        for task in tasks:
            args = task.get("arguments", {})
            device = args.get("device_name")
            action = args.get("action")
            if not device or action not in _OPPOSITE:
                continue
            if device in seen and seen[device][0] != action:
                conflicts.append({
                    "device_name": device,
                    "actions": [seen[device][0], action],
                    "task_ids": [seen[device][1], task.get("task_id")],
                    "detail": f"{device} 同时被要求「{seen[device][0]}」和「{action}」，自相矛盾",
                })
            seen[device] = (action, task.get("task_id"))
        return conflicts

    # ---------------- 并发编排 ----------------

    async def run(
        self,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        registry: Any,
        assignments: list[dict[str, Any]] | None = None,
    ) -> list[AgentOutcome]:
        """按分派结果并发执行各子 Agent，返回 outcomes。

        并发的价值：设备控制和日程创建互不依赖，串行是白白等一倍时间。
        隔离的价值：一个 Agent 抛异常只影响它自己的 outcome。
        """
        if assignments is None:
            assignments = self.dispatch(state.get("intents", []), tasks)

        coros = []
        for item in assignments:
            agent = self._by_name.get(item["agent"])
            if agent is None:
                continue
            task_ids = set(item.get("task_ids", []))
            subset = [t for t in tasks if t.get("task_id") in task_ids]
            coros.append(agent.run(state, subset, registry))

        if not coros:
            return []

        with span("supervisor.run", logger, trace_id=state.get("trace_id", "-")):
            outcomes = await asyncio.gather(*coros, return_exceptions=True)

        results: list[AgentOutcome] = []
        for item, outcome in zip(assignments, outcomes):
            if isinstance(outcome, BaseException):  # 双保险：run 内部已兜底，这里再兜一层
                results.append(AgentOutcome(
                    agent=item["agent"], domain=item.get("domain", ""),
                    ok=False, degraded=True, error=f"{type(outcome).__name__}: {outcome}",
                ))
            else:
                results.append(outcome)
        return results
