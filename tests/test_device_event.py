"""设备事件历史回归（「私有数据」卖点的数据层）。

覆盖：
- 记录与按设备查询
- 租户隔离（A 家的事件 B 家查不到）
- `find_for_query` 按设备名命中 / 按语义兜底 / 无关查询不瞎召回
- 未知 event_type 退化为 fault（不能因为一个参数写错就丢用户数据）
- 知识问答真的用上了历史（mock 模式下走降级分支也要能看出来）
"""

from __future__ import annotations

import pytest

from homemind.tools.device_event_store import DeviceEventStore, EVENT_TYPES


@pytest.fixture
def store(tmp_path):
    return DeviceEventStore(db_path=str(tmp_path / "ev.db"))


# ---------------------------------------------------------------- 记录与查询

def test_record_and_list_for_device(store):
    """记一条事件，能按设备查回来，字段不丢。"""
    store.record(
        "f1", "washer_1", event_type="repair",
        symptom="报 P4，不进水", cause="进水阀堵塞",
        action="清洗滤网", cost=80.0, device_name="洗衣机",
        happened_at=1_700_000_000,
    )
    events = store.list_for_device("f1", "washer_1")
    assert len(events) == 1
    e = events[0]
    assert e["symptom"] == "报 P4，不进水"
    assert e["cause"] == "进水阀堵塞"
    assert e["action"] == "清洗滤网"
    assert e["cost"] == 80.0
    assert e["event_label"] == "维修"
    assert e["device_name"] == "洗衣机"


def test_list_for_device_only_returns_that_device(store):
    """按设备过滤不能把邻居装置的事件也捞进来。"""
    store.record("f1", "washer_1", symptom="洗衣机问题", device_name="洗衣机")
    store.record("f1", "ac_1", symptom="空调问题", device_name="空调")
    assert len(store.list_for_device("f1", "washer_1")) == 1
    assert len(store.list_all("f1")) == 2


def test_tenant_isolation(store):
    """A 家记的事件，B 家一条都看不到。"""
    store.record("f1", "washer_1", symptom="一家的洗衣机坏了", device_name="洗衣机")
    assert store.count("f1") == 1
    assert store.count("f2") == 0
    assert store.list_for_device("f2", "washer_1") == []


def test_unknown_event_type_falls_back(store):
    """未知类型退化成 fault，而不是抛异常把数据丢掉。"""
    store.record("f1", "ac_1", event_type="我们家电工发明的类型", symptom="不制冷")
    events = store.list_for_device("f1", "ac_1")
    assert len(events) == 1
    assert events[0]["event_type"] == "fault"


def test_event_types_cover_four_kinds():
    """四种事件类型都要有中文标签，前端直接吐这个。"""
    assert set(EVENT_TYPES) == {"fault", "repair", "maintain", "replace"}
    assert all(EVENT_TYPES.values())


# ---------------------------------------------------------------- 给问答用的检索

def test_find_for_query_hits_by_device_name(store):
    """问句里提到设备名，就该召回这台设备的历史。"""
    store.record("f1", "washer_1", event_type="repair",
                 symptom="报 P4 不进水", action="清洗滤网", device_name="洗衣机")
    hits = store.find_for_query("f1", "洗衣机报 P4 是什么问题")
    assert hits, "提到洗衣机就该召回洗衣机的事件"
    assert hits[0]["device_name"] == "洗衣机"
    assert hits[0]["score"] == 1.0


def test_find_for_query_isolated_by_family(store):
    """别家的维修史不能被召回——这是跨租户泄露。"""
    store.record("f2", "washer_1", symptom="报 P4 不进水", device_name="洗衣机")
    assert store.find_for_query("f1", "洗衣机报 P4") == []


def test_find_for_query_returns_empty_for_unrelated(store):
    """无关问题不瞎召回，否则会把垃圾拼进 prompt。"""
    store.record("f1", "washer_1", symptom="报 P4 不进水", device_name="洗衣机")
    assert store.find_for_query("f1", "今天股票怎么样") == []


def test_find_for_query_empty_store(store):
    """一条事件都没有时返回空列表，不炸。"""
    assert store.find_for_query("f1", "洗衣机报 P4") == []


# ---------------------------------------------------------------- 接入知识问答

@pytest.mark.asyncio
async def test_knowledge_agent_uses_device_history(store, monkeypatch, tmp_path):
    """知识问答要真的把设备历史说出来，而不是只查说明书。

    测试环境走 mock（client.enabled=False），会进降级分支——
    这时应该优先吐历史记录，因为历史比说明书更个性化。
    """
    from homemind.agents import knowledge_agent as ka
    from homemind.agents.base import AgentOutcome
    from homemind.core.tenancy import FAMILY_ARG
    from homemind.memory.knowledge_store import KnowledgeStore, set_knowledge_store

    set_knowledge_store(KnowledgeStore(db_path=str(tmp_path / "kb.db")))
    monkeypatch.setattr(ka, "get_device_event_store", lambda: store)

    store.record("f1", "washer_1", event_type="repair",
                 symptom="报 P4 不进水", cause="进水阀堵塞",
                 action="清洗滤网", cost=80.0, device_name="洗衣机",
                 happened_at=1_700_000_000)

    agent = ka.KnowledgeAgent()
    outcome = await agent.handle(
        {"user_input": "洗衣机报 P4 怎么办", FAMILY_ARG: "f1"},
        [], None, AgentOutcome(agent="knowledge_agent"),
    )
    assert outcome.answer
    assert "历史记录" in outcome.answer
    assert "清洗滤网" in outcome.answer


@pytest.mark.asyncio
async def test_knowledge_agent_says_not_found_when_nothing(tmp_path, monkeypatch):
    """三源全空就明说没查到，不许编。"""
    from homemind.agents import knowledge_agent as ka
    from homemind.agents.base import AgentOutcome
    from homemind.core.tenancy import FAMILY_ARG
    from homemind.memory.knowledge_store import KnowledgeStore, set_knowledge_store
    from homemind.tools.device_event_store import DeviceEventStore

    set_knowledge_store(KnowledgeStore(db_path=str(tmp_path / "kb.db")))
    monkeypatch.setattr(ka, "get_device_event_store",
                        lambda: DeviceEventStore(db_path=str(tmp_path / "ev.db")))

    agent = ka.KnowledgeAgent()
    outcome = await agent.handle(
        {"user_input": "量子计算机怎么修", FAMILY_ARG: "f1"},
        [], None, AgentOutcome(agent="knowledge_agent"),
    )
    assert "没查到" in outcome.answer
