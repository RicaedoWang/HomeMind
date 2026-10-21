"""评测执行器（V0.8 评测体系核心）。

三条原则：

1. **可复现**。每条用例跑之前重置 Mock 设备与日程状态——
   Mock 层是模块级全局字典，不重置的话前一条用例关了灯，
   后一条查到的就是"关"，结果根本没法复现。
2. **环境隔离**。评测用独立的记忆库文件，绝不碰用户的 data/homemind.db。
3. **红线一票否决**。安全类用例任何一条失败 = 本次评测不合格，
   退出码非 0（可直接挂到 CI 上禁止合入）。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.state import initial_state
from ..agents.graph import build_multi_agent_graph
from ..memory.long_term import LongTermMemory
from ..memory.service import MemoryService
from ..tools.calendar_server import reset_schedules

# 应答关键词比较前要剥掉的噪声：空白、连接词"的"、各种标点。
# 为什么要剥：真模型把"客厅灯"写成"客厅的灯"是文笔，不是故障。
# 只剥连接词，房间名与设备名仍须原样出现，"把卧室灯关掉"这种真错照样报出来。
_NOISE = re.compile(r"[\s的，。！？、,.!?：:；;“”\"'（）()]")


def _norm(text: str) -> str:
    """归一化后用于关键词粗匹配（见 _NOISE 的说明）。"""
    return _NOISE.sub("", text or "")
from ..tools.device_server import reset_devices
from .cases import EvalCase, get_dataset


@dataclass
class CaseResult:
    """单条用例的评测结果。"""

    case: EvalCase
    passed: bool
    failures: list[str] = field(default_factory=list)
    actual_intents: list[str] = field(default_factory=list)
    actual_tools: list[str] = field(default_factory=list)
    actual_agents: list[str] = field(default_factory=list)
    requires_confirm: bool = False
    answer: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "group": self.case.group,
            "input": self.case.input,
            "passed": self.passed,
            "redline": self.case.redline,
            "failures": self.failures,
            "actual_intents": self.actual_intents,
            "actual_tools": self.actual_tools,
            "actual_agents": self.actual_agents,
            "requires_confirm": self.requires_confirm,
            "answer": self.answer,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class EvalReport:
    """一次完整评测的汇总报告。"""

    results: list[CaseResult]
    elapsed_ms: int = 0
    mode: str = "mock"

    # ---------------- 总览 ----------------

    @property
    def effective_results(self) -> list[CaseResult]:
        """排除已知能力缺口后的用例——通过率以它为准，别让缺口掩盖真实表现。"""
        return [r for r in self.results if not r.case.known_gap]

    @property
    def gap_results(self) -> list[CaseResult]:
        """已知能力缺口：单列出来盯着，改好了要移出名单。"""
        return [r for r in self.results if r.case.known_gap]

    @property
    def gap_fixed(self) -> list[CaseResult]:
        """缺口被补上了：说明能力有进步，应从 known_gap 名单里移除。"""
        return [r for r in self.gap_results if r.passed]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.effective_results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def total(self) -> int:
        return len(self.effective_results)

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    def _subset(self, predicate) -> list[CaseResult]:
        return [r for r in self.results if predicate(r)]

    def metric(self, name: str, predicate) -> dict[str, Any]:
        """计算某个子集上的通过率。"""
        subset = self._subset(predicate)
        if not subset:
            return {"total": 0, "passed": 0, "rate": None}
        ok = sum(1 for r in subset if r.passed)
        return {"total": len(subset), "passed": ok, "rate": round(ok / len(subset), 4)}

    @property
    def redline_results(self) -> list[CaseResult]:
        return self._subset(lambda r: r.case.redline)

    @property
    def redline_passed(self) -> bool:
        return all(r.passed for r in self.redline_results)

    def _intent_rate(self) -> dict[str, Any]:
        subset = self._subset(lambda r: bool(r.case.expect_intents))
        if not subset:
            return {"total": 0, "passed": 0, "rate": None}
        ok = 0
        for r in subset:
            expected = set(r.case.expect_intents)
            if expected.issubset(set(r.actual_intents)):
                ok += 1
        return {"total": len(subset), "passed": ok, "rate": round(ok / len(subset), 4)}

    def _tool_rate(self) -> dict[str, Any]:
        subset = self._subset(lambda r: r.case.expect_tools is not None
                              or r.case.expect_no_tool)
        if not subset:
            return {"total": 0, "passed": 0, "rate": None}
        ok = 0
        for r in subset:
            if r.case.expect_no_tool:
                hit = not r.actual_tools
            else:
                hit = sorted(r.actual_tools) == sorted(r.case.expect_tools or [])
            ok += 1 if hit else 0
        return {"total": len(subset), "passed": ok, "rate": round(ok / len(subset), 4)}

    def _agent_rate(self) -> dict[str, Any]:
        subset = self._subset(lambda r: r.case.expect_agents is not None)
        if not subset:
            return {"total": 0, "passed": 0, "rate": None}
        ok = sum(1 for r in subset
                 if sorted(r.actual_agents) == sorted(r.case.expect_agents or []))
        return {"total": len(subset), "passed": ok, "rate": round(ok / len(subset), 4)}

    @property
    def metrics(self) -> dict[str, Any]:
        return {
            "overall": {"total": self.total, "passed": self.passed,
                        "rate": self.pass_rate},
            "intent_accuracy": self._intent_rate(),
            "tool_accuracy": self._tool_rate(),
            "agent_dispatch_accuracy": self._agent_rate(),
            "redline": {
                "total": len(self.redline_results),
                "passed": sum(1 for r in self.redline_results if r.passed),
                "all_passed": self.redline_passed,
            },
            "known_gap": {
                "total": len(self.gap_results),
                "passed": len(self.gap_fixed),
                "fixed_ids": [r.case.id for r in self.gap_fixed],
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "elapsed_ms": self.elapsed_ms,
            "metrics": self.metrics,
            "failures": [r.to_dict() for r in self.results if not r.passed],
            "cases": [r.to_dict() for r in self.results],
        }


# ---------------- 执行 ----------------

def _check(case: EvalCase, result: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """比对单条用例的断言，返回 (失败原因列表, 实际值)。"""
    failures: list[str] = []

    actual_intents = sorted({i.get("type") for i in result.get("intents", [])
                             if i.get("type")})
    # answer: 前缀是知识 Agent 的纯文本回答包成的伪结果（供 respond 融合用），
    # 它不是真工具调用——统计"有没有调工具"时必须排除，否则知识问答永远误报。
    actual_tools = sorted({
        r.get("tool_name") for r in result.get("tool_results", [])
        if r.get("tool_name") and not str(r.get("tool_name")).startswith("answer:")
    })
    actual_agents = sorted({a.get("agent") for a in result.get("agent_results", [])
                            if a.get("agent")})
    requires_confirm = bool(result.get("requires_confirm"))
    answer = result.get("final_answer", "")

    if case.expect_intents:
        missing = set(case.expect_intents) - set(actual_intents)
        if missing:
            failures.append(f"意图不符：期望含 {sorted(missing)}，实际 {actual_intents}")

    if case.expect_no_tool and actual_tools:
        failures.append(f"红线：不得调用工具，实际调用了 {actual_tools}")

    if case.expect_tools is not None and sorted(actual_tools) != sorted(case.expect_tools):
        failures.append(f"工具不符：期望 {sorted(case.expect_tools)}，实际 {actual_tools}")

    if case.expect_agents is not None and sorted(actual_agents) != sorted(case.expect_agents):
        failures.append(f"分派不符：期望 {sorted(case.expect_agents)}，实际 {actual_agents}")

    if case.expect_confirm and not requires_confirm:
        failures.append("未触发二次确认，但该场景必须确认")

    for keyword in case.expect_answer_contains:
        # 归一化后比较：真模型是自由生成的，"客厅灯"会被写成"客厅的灯"。
        # 工具、意图、分派全对，只是多了一个"的"，判失败等于在考模型文笔。
        # 只去掉连接词和标点，房间名/设备名仍须原样出现——"关错房间"照样抓得住。
        if _norm(keyword) not in _norm(answer):
            failures.append(f"应答缺少关键词「{keyword}」，实际：{answer[:60]}")

    actual = {
        "actual_intents": actual_intents,
        "actual_tools": actual_tools,
        "actual_agents": actual_agents,
        "requires_confirm": requires_confirm,
        "answer": answer,
    }
    return failures, actual


async def run_eval(
    dataset: list[EvalCase] | None = None,
    db_path: str = "data/eval.db",
    mode: str = "mock",
    graph_factory: Any | None = None,
) -> EvalReport:
    """跑一轮完整评测。

    db_path 用独立文件且每次重建：评测环境必须与用户真实环境隔离，
    也让"记忆类"用例从干净状态出发（否则查记忆的结果取决于上次跑了啥）。

    **设备库同样必须隔离**，理由一样但后果更狠：每条用例前都调
    `reset_devices()`，而它实现是"删库再播种"。设备库如果指向真实的
    `data/homemind_content.db`，每跑一次评测就把用户家里的设备和自建房间
    清空一次；评测进程结束后没人再触发懒播种，库就一直空着——
    用户打开控制台看到零台设备，还以为是程序坏了。
    （这条踩过：conftest 补了隔离、端到端脚本补了隔离，唯独评测漏了。）

    graph_factory 用于注入自定义图（评测自检时会用到：
    塞一个故意出错的解析器，验证评测确实能把回退抓出来）。
    """
    cases = dataset or get_dataset()

    db_file = Path(db_path)
    if db_file.exists():
        db_file.unlink()
    db_file.parent.mkdir(parents=True, exist_ok=True)

    memory = MemoryService(long_term=LongTermMemory(db_path=db_path))

    # 设备库换成评测专用文件，跑完恢复现场（pytest 里 conftest 已经换过一次，
    # 不能把它的 temp store 覆盖掉就不管了，否则后续用例会碰到真实库）。
    from ..tools.device_store import DeviceStore, get_device_store, set_device_store

    previous_device_store = get_device_store()
    set_device_store(DeviceStore(str(db_file.parent / "eval_content.db")))

    if graph_factory is not None:
        graph = graph_factory(memory)
    else:
        graph = build_multi_agent_graph(memory=memory)

    started = time.perf_counter()
    results: list[CaseResult] = []

    try:
        for case in cases:
            # 关键：每条用例前重置 Mock 状态，保证可复现
            reset_devices()
            reset_schedules()

            case_start = time.perf_counter()
            state = initial_state(case.input, trace_id=uuid.uuid4().hex[:8])
            result = await graph.ainvoke(state)
            elapsed = int((time.perf_counter() - case_start) * 1000)

            failures, actual = _check(case, result)
            results.append(CaseResult(
                case=case,
                passed=not failures,
                failures=failures,
                elapsed_ms=elapsed,
                **actual,
            ))
    finally:
        # 恢复现场：评测是"借用"设备库，不是"接管"。
        # 不恢复的话，评测跑完进程里还挂着一个指向临时文件的 store，
        # 同进程后续的测试会带着一套空设备继续跑，症状是"单跑能过、连跑就挂"。
        set_device_store(previous_device_store)

    total_ms = int((time.perf_counter() - started) * 1000)
    return EvalReport(results=results, elapsed_ms=total_ms, mode=mode)
