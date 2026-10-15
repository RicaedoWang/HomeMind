"""多轮对话记忆回归测试（"滑动窗口 + 摘要压缩"模式）。

对应两份契约：
    窗口契约  短期记忆只保留最近 N 条原文，溢出部分压成摘要，信息不凭空消失
    隔离契约  会话按登录者隔离，换账号互不可见；登出即清空

mock 模式下摘要走"逐条拼接"降级路径——这条路径是确定性的，可以放心断言；
LLM 摘要的真实效果靠起服务实测，不靠单测（与全项目的测试原则一致）。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.memory.short_term import ShortTermMemory, get_short_term_memory  # noqa: E402


# ---------------------------------------------------------- 窗口契约


@pytest.mark.asyncio
async def test_window_compression_keeps_summary() -> None:
    """超过 2×窗口：原文裁到窗口大小，溢出内容进摘要，不凭空消失。"""
    mem = ShortTermMemory()
    sid = "test:compress"
    await mem.clear(sid)

    window = mem._settings.memory.short_term_window
    for i in range(window * 2 + 5):        # 明显超窗
        await mem.append(sid, "user", f"消息{i:02d} 开窗 Marker{i:02d}")

    messages = await mem.get(sid)
    assert len(messages) <= window, "原文必须裁到窗口以内"

    ctx = await mem.get_context(sid)
    assert ctx["summary"], "超窗后必须产生摘要"
    # 降级拼接路径：最早的溢出消息要出现在摘要里（信息不丢）
    assert "消息00" in ctx["summary"]
    # 窗口内的最新消息仍是原文
    assert messages[-1]["content"].startswith(f"消息{window * 2 + 4:02d}")
    await mem.clear(sid)


@pytest.mark.asyncio
async def test_no_summary_below_threshold() -> None:
    """窗口内不触发压缩：没有摘要，原文齐全。"""
    mem = ShortTermMemory()
    sid = "test:no-compress"
    await mem.clear(sid)
    for i in range(5):
        await mem.append(sid, "user", f"m{i}")
    ctx = await mem.get_context(sid)
    assert ctx["summary"] in (None, "")
    assert len(ctx["messages"]) == 5
    await mem.clear(sid)


# ---------------------------------------------------------- 隔离契约


@pytest.mark.asyncio
async def test_sessions_isolated_per_member() -> None:
    """A、B 两个会话互不可见——这是"换账号不串话"的根基。"""
    mem = ShortTermMemory()
    await mem.clear("test:a")
    await mem.clear("test:b")
    await mem.append("test:a", "user", "只有A知道的事")
    await mem.append("test:b", "user", "只有B知道的事")

    a = await mem.get("test:a")
    b = await mem.get("test:b")
    assert len(a) == 1 and "只有A知道的事" in a[0]["content"]
    assert len(b) == 1 and "只有B知道的事" in b[0]["content"]
    await mem.clear("test:a")
    await mem.clear("test:b")


@pytest.mark.asyncio
async def test_logout_clears_member_session(client_factory=None) -> None:
    """登出必须清掉该成员的控制台会话（防止下个登录者问出上一家的事）。"""
    mem = get_short_term_memory()
    sid = "console:family_sz:member_001"
    await mem.clear(sid)
    await mem.append(sid, "user", "我要被登出了")
    assert len(await mem.get(sid)) == 1

    from homemind.auth.tokens import AuthUser
    from homemind.api.auth import set_account_store
    from pathlib import Path as _P

    set_account_store(AccountStore(db_path=str(_P("data/eval_accounts_logout.db"))))
    app.dependency_overrides[current_user] = lambda: AuthUser(
        family_id="family_sz", member_id="member_001", role="L4", name="我", city="深圳",
    )
    try:
        with TestClient(app) as client:
            res = client.post("/api/auth/logout")
        assert res.status_code == 200
        assert res.json()["ok"] is True
        assert await mem.get(sid) == [], "登出后该成员会话必须被清空"
    finally:
        app.dependency_overrides.clear()
        set_account_store(None)
        await mem.clear(sid)


# ---------------------------------------------------------- 助手链路贯通


@pytest.fixture()
def client(tmp_path):
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides[current_user] = lambda: AuthUser(
        family_id="family_sz", member_id="member_001", role="L4", name="我", city="深圳",
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)


@pytest.mark.asyncio
async def test_assistant_persists_conversation_history(client: TestClient) -> None:
    """连问两轮：短期记忆里必须有 user/assistant 交替的四条记录。

    mock 模式下应答是模板，但"历史有没有被记录和注入"是确定性可断言的。
    """
    mem = get_short_term_memory()
    sid = "console:family_sz:member_001"
    await mem.clear(sid)

    r1 = client.post("/api/console/assistant", json={"text": "打开客厅灯"})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/console/assistant", json={"text": "我刚刚干了什么?"})
    assert r2.status_code == 200

    ctx = await mem.get_context(sid)
    roles = [m["role"] for m in ctx["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assert ctx["messages"][0]["content"] == "打开客厅灯"
    assert ctx["messages"][1]["content"], "第一轮的应答也要进记忆"
    await mem.clear(sid)


@pytest.mark.asyncio
async def test_history_reaches_the_graph_state(client: TestClient) -> None:
    """历史必须真的进入 AgentState（否则注入 prompt 无从谈起）。"""
    from homemind.agent.graph import build_agent_graph
    from homemind.agent.state import initial_state
    from homemind.tools.registry import default_registry

    graph = build_agent_graph(registry=default_registry())
    state = initial_state(
        user_input="我刚刚干了什么?",
        chat_history=[{"role": "user", "content": "打开窗帘"},
                      {"role": "assistant", "content": "已打开窗帘"}],
        chat_summary="此前用户测试过场景联动",
    )
    result = await graph.ainvoke(state)
    assert result["final_answer"], "带历史跑图不应崩，且应有应答"
