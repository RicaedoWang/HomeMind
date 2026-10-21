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
from homemind.core.llm import LLMClient
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
    """检索能命中相关说明书片段（内置常识兜底）。"""
    hits = retrieve("空调滤网怎么清洗", "f1", top_k=2)
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

        async def parse(self, text: str, family_id: str | None = None) -> list[Intent]:
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


# ---------------- 真模型兜底路径（sup-01） ----------------

class _RealShapedClient:
    """假"真模型"客户端：只换掉网络层，chat_json 用**真实实现**。

    为什么必须复用真实的 chat_json：如果这里自己重写一个能接受 max_tokens 的
    假方法，测的就是假签名——调用方传错参数照样"通过"，等于没测。
    绑定真实实现后，签名对不上会原样抛 TypeError。
    """

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.enabled = True
        self.calls: list[dict] = []

    async def chat(self, prompt, system="", temperature=None, max_tokens=800):
        self.calls.append({"system": system, "max_tokens": max_tokens})
        return self.payload

    chat_json = LLMClient.chat_json
    # 必须包 staticmethod：LLMClient.parse_json 是静态方法，裸赋值成类属性会退化成
    # 普通函数，通过 self 访问时把 self 也传进去，直接参数个数不符。
    parse_json = staticmethod(LLMClient.parse_json)


async def test_supervisor_llm_fallback_signature_matches_client(monkeypatch):
    """规则分不出时走模型兜底，且调用实参必须与 LLMClient 签名一致。

    回归自 2026-10-19 真模型实测：supervisor 传了 max_tokens=80，而
    LLMClient.chat_json 当时没有这个参数 →
    `TypeError: chat_json() got an unexpected keyword argument 'max_tokens'`，
    多 Agent 整条链直接 traceback。

    为什么 105 条单测一条都没抓到：mock 模式下 `if not client.enabled` 在
    调用之前就 return 了，那行代码在强制 mock 的测试里**永远执行不到**。
    所以这条用例必须显式造一个 enabled=True 的客户端，把那段死代码走通。
    """
    from homemind.agents import supervisor as sup_mod

    fake = _RealShapedClient('{"agent": "device_agent"}')
    monkeypatch.setattr(sup_mod, "get_llm_client", lambda: fake)

    assignments, used_llm = await Supervisor().dispatch_with_llm("把灯弄亮一点吧", [], [])

    assert used_llm is True, "规则没命中时应交给模型判定"
    assert assignments and assignments[0]["agent"] == "device_agent"
    assert fake.calls, "应该真的调用了模型"
    assert fake.calls[0]["max_tokens"] == 80, \
        "只选一个 Agent 名字，别按长文本的额度烧 token"


async def test_supervisor_llm_fallback_survives_garbage_output(monkeypatch):
    """模型返回垃圾/选了个不存在的 Agent 时，按规则结果走，不硬猜、不抛错。"""
    from homemind.agents import supervisor as sup_mod

    fake = _RealShapedClient('{"agent": "不存在的Agent"}')
    monkeypatch.setattr(sup_mod, "get_llm_client", lambda: fake)

    assignments, used_llm = await Supervisor().dispatch_with_llm("嗯嗯啊啊", [], [])

    assert assignments == [], "没有合适的 Agent 就返回空，交给上层降级"
    assert used_llm is False
