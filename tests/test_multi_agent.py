"""V0.8 多 Agent 协同测试。

重点验证三件事：
1. 分派正确——什么意图归谁
2. 权限收得住——子 Agent 碰不到白名单外的工具
3. 协同不削弱安全——冲突检测在**执行前**拦下矛盾操作
"""

from __future__ import annotations

import pytest

from homemind.agent.state import initial_state
from homemind.agents import Supervisor, build_multi_agent_graph
from homemind.agents.base import AgentOutcome, BaseAgent
from homemind.agents.device_agent import DeviceAgent
from homemind.agents.knowledge_agent import KnowledgeAgent, retrieve
from homemind.agents.schedule_agent import ScheduleAgent
from homemind.core.models import Intent, IntentType
from homemind.memory.long_term import LongTermMemory
from homemind.memory.service import MemoryService
from homemind.tools.registry import default_registry


def _intent_dict(itype: str, **entities) -> dict:
    return {"type": itype, "confidence": 0.95, "raw_text": "测试", "entities": entities}


def _task(task_id: str, itype: str, tool_name: str, **arguments) -> dict:
    return {
        "task_id": task_id,
        "intent": _intent_dict(itype),
        "tool_name": tool_name,
        "arguments": arguments,
        "status": "pending",
        "requires_confirm": False,
    }


# ---------------- 分派 ----------------

def test_dispatch_device_control_to_device_agent():
    """设备控制类任务归设备管家。"""
    sup = Supervisor()
    tasks = [_task("t1", "device_control", "device_control",
                   action="turn_off", device_name="客厅灯")]
    assignments = sup.dispatch([_intent_dict("device_control")], tasks)
    assert [a["agent"] for a in assignments] == ["device_agent"]


def test_dispatch_splits_multiple_domains():
    """一句话涉及两个领域 → 分派给两个 Agent（后续并发执行）。"""
    sup = Supervisor()
    tasks = [
        _task("t1", "device_control", "device_control", action="turn_on", device_name="空调"),
        _task("t2", "schedule", "create_reminder", content="开会",
              scheduled_at="2026-10-15T08:00:00"),
    ]
    intents = [_intent_dict("device_control"), _intent_dict("schedule")]
    assignments = sup.dispatch(intents, tasks)
    assert {a["agent"] for a in assignments} == {"device_agent", "schedule_agent"}


def test_dispatch_knowledge_without_tasks():
    """知识问答不产生任务，但仍要有人接手。"""
    sup = Supervisor()
    assignments = sup.dispatch([_intent_dict("knowledge")], [])
    assert [a["agent"] for a in assignments] == ["knowledge_agent"]


# ---------------- 权限白名单 ----------------

@pytest.mark.asyncio
async def test_whitelist_blocks_foreign_tool():
    """把门锁任务塞给日程 Agent：工具调用次数必须是 0。"""
    agent = ScheduleAgent()
    bad_task = _task("t1", "device_control", "lock_control",
                     action="unlock", device_name="门锁")
    outcome = await agent.run({"user_input": "开门", "confirmed": True},
                              [bad_task], default_registry())
    assert outcome.tool_calls == 0
    assert outcome.skipped_tasks[0]["reason"] == "工具不在 schedule_agent 白名单内"


@pytest.mark.asyncio
async def test_device_agent_requires_device_name():
    """没说清控制哪个设备 → 不动手，要求澄清（宁可问，不可瞎开）。"""
    agent = DeviceAgent()
    task = _task("t1", "device_control", "device_control", action="turn_on")
    outcome = await agent.run({"user_input": "打开", "confirmed": True},
                              [task], default_registry())
    assert outcome.tool_calls == 0
    assert "未指明设备" in outcome.skipped_tasks[0]["reason"]


# ---------------- 冲突检测 ----------------

def test_conflict_detected_for_opposite_actions():
    """同一设备被要求开又被要求关 → 检出冲突。"""
    sup = Supervisor()
    tasks = [
        _task("t1", "device_control", "device_control", action="turn_on", device_name="客厅灯"),
        _task("t2", "device_control", "device_control", action="turn_off", device_name="客厅灯"),
    ]
    conflicts = sup.detect_conflicts(tasks)
    assert len(conflicts) == 1
    assert conflicts[0]["device_name"] == "客厅灯"


def test_no_conflict_for_same_device_same_action():
    """相同动作不算冲突，别误伤。"""
    sup = Supervisor()
    tasks = [
        _task("t1", "device_control", "device_control", action="turn_on", device_name="客厅灯"),
        _task("t2", "device_control", "device_control", action="turn_on", device_name="客厅灯"),
    ]
    assert sup.detect_conflicts(tasks) == []


# ---------------- 失败隔离 ----------------

@pytest.mark.asyncio
async def test_agent_exception_is_isolated():
    """子 Agent 内部崩溃：不能炸穿主链路，要转成 failed outcome。"""

    class BoomAgent(BaseAgent):
        name = "boom"
        domain = "故障"
        allowed_tools = set()
        handled_intents = {"device_control"}

        async def handle(self, state, tasks, registry, outcome: AgentOutcome) -> AgentOutcome:
            raise RuntimeError("boom")

    sup = Supervisor(agents=[BoomAgent()])
    state = {"user_input": "开灯", "trace_id": "-", "confirmed": True,
             "intents": [_intent_dict("device_control")], "recalled_memories": []}
    tasks = [_task("t1", "device_control", "device_control",
                   action="turn_on", device_name="客厅灯")]
    outcomes = await sup.run(state, tasks, default_registry())
    assert len(outcomes) == 1
    assert outcomes[0].ok is False
    assert outcomes[0].degraded is True
    assert "RuntimeError" in outcomes[0].error


# ---------------- 知识检索 ----------------

def test_knowledge_retrieve_hits_relevant_doc():
    """检索能命中相关说明书片段。"""
    hits = retrieve("空调滤网怎么清洗", top_k=2)
    assert hits, "应检索到相关文档"
    assert "滤网" in hits[0][0] + hits[0][1]


@pytest.mark.asyncio
async def test_knowledge_agent_never_calls_tools():
    """知识 Agent 只回答，绝不操作设备。"""
    agent = KnowledgeAgent()
    outcome = await agent.run({"user_input": "空调滤网要多久洗一次"},
                              [_task("t1", "knowledge", "device_control",
                                     action="turn_on", device_name="空调")],
                              default_registry())
    assert outcome.tool_calls == 0
    assert outcome.answer


# ---------------- 端到端 ----------------

@pytest.mark.asyncio
async def test_multi_graph_conflict_suspends_execution(tmp_path):
    """完整链路：冲突任务在执行前被挂起，一个工具都不能调。"""

    class StubParser:
        def __init__(self, intents: list[Intent]) -> None:
            self._intents = intents

        async def parse(self, text: str) -> list[Intent]:
            return self._intents

    intents = [
        Intent(type=IntentType.DEVICE_CONTROL, confidence=0.95,
               raw_text="把客厅灯打开", entities={"device_name": "客厅灯", "action": "turn_on"}),
        Intent(type=IntentType.DEVICE_CONTROL, confidence=0.95,
               raw_text="再把客厅灯关掉", entities={"device_name": "客厅灯", "action": "turn_off"}),
    ]
    memory = MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "multi.db")))
    graph = build_multi_agent_graph(parser=StubParser(intents), memory=memory)

    result = await graph.ainvoke(initial_state("把客厅灯打开再关掉", trace_id="t-conflict"))

    assert len(result.get("conflicts", [])) == 1
    assert result.get("tool_results") == [], "冲突未确认前不得执行任何操作"
    assert "冲突" in result.get("final_answer", "")


@pytest.mark.asyncio
async def test_multi_graph_end_to_end_device_control(tmp_path):
    """完整链路：设备控制由 device_agent 执行并给出应答。"""
    memory = MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "multi2.db")))
    graph = build_multi_agent_graph(memory=memory)

    result = await graph.ainvoke(initial_state("把客厅的灯关掉", trace_id="t-e2e"))

    assert any(a["agent"] == "device_agent" for a in result.get("agent_assignments", []))
    assert result.get("tool_results"), "应执行了设备控制"
    assert "客厅灯" in result.get("final_answer", "")
