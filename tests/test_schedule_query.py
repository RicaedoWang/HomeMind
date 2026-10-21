"""日程「查询」与「创建」的拆分回归（V1.4）。

回归自 2026-10-21 实测。用户说「看看今天的提醒」，系统不是不理他，
而是**真的建了一条标题叫「看看」、时间今天 09:00 的提醒**：

    '看看今天的提醒'  → schedule  content='看看'   at=2026-10-21T09:00:00
    '今天有什么安排'  → schedule  content='有什么' at=2026-10-21T09:00:00

根因不是某一行写错，是**类型划分缺了一维**：
`IntentType.SCHEDULE` 同时背着"给我建一条"和"看看我有哪些"，
而执行层只有 `create_reminder` 一条路。只要判成 schedule，就必然创建——
一个意图类型对应两种副作用，误创建永远治不干净。

修法：新增 `SCHEDULE_QUERY`（只读、走 `list_schedule`、不需要确认），
判定排在 `SCHEDULE` **之前**——两句话都含"提醒"，光看名词分不开，
只能靠"查询动词 + 日程名词"这个搭配来认。

顺序为什么这么排：查询被当创建 = 凭空多一条脏数据，要用户自己发现并手删；
创建被当查询 = 用户重复说一遍。**前者更烦**，与"误执行比漏执行严重"同源。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.agent.intent import (  # noqa: E402
    SCHEDULE_QUERY_PATTERN, IntentParser,
)
from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_pending, reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.core.models import IntentType  # noqa: E402
from homemind.tools.calendar_server import get_schedules, reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402

# 用户真会说的查询句。改动 SCHEDULE_QUERY_PATTERN 时这张表必须一条不落地绿——
# 它们是需求，不是"顺便覆盖一下"。
QUERY_SENTENCES = [
    "看看今天的提醒",
    "查看我的日程",
    "我有哪些提醒",
    "今天有什么安排",
    "明天有什么安排",
    "我的日程",
    "还有什么提醒",
]

# 对照组：这些是在**创建**，一句都不许被判成查询。
# 少一条都可能被"查询优先"的顺序吃掉——这就是把顺序排在前面的代价。
CREATE_SENTENCES = [
    "提醒我九点关灯",
    "明天提醒我交水费",
    "新加今日任务，晚上八点喂猫",
    "记得提醒我买猫粮",
    "下午三点提醒我开会",
]


@pytest.fixture(autouse=True)
def _clean():
    """日程与任务都是模块级全局列表，不重置结果不可复现。"""
    reset_devices()
    reset_schedules()
    reset_tasks()
    reset_pending()
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()
    reset_pending()


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


def _parse(text: str) -> list:
    return asyncio.run(IntentParser().parse(text, "family_sz"))


def _ask(client: TestClient, text: str, confirmed: bool = False) -> dict:
    return client.post(
        "/api/console/assistant", json={"text": text, "confirmed": confirmed}
    ).json()


# ---------------------------------------------------------------- 意图层

@pytest.mark.parametrize("text", QUERY_SENTENCES)
def test_query_sentence_parsed_as_query(text: str):
    """一句人要看的查询，必须判成 SCHEDULE_QUERY。"""
    intents = _parse(text)
    assert intents, f"{text!r} 解析出空意图"
    assert intents[0].type == IntentType.SCHEDULE_QUERY, (
        f"{text!r} 被判成 {intents[0].type.value}——查询句被当成创建会凭空造脏数据"
    )


@pytest.mark.parametrize("text", CREATE_SENTENCES)
def test_create_sentence_still_creates(text: str):
    """对照组：真正的创建请求一句都不许被"查询优先"吃掉。"""
    intents = _parse(text)
    assert intents[0].type == IntentType.SCHEDULE, (
        f"{text!r} 被判成 {intents[0].type.value}——用户要建提醒，系统却没建"
    )


@pytest.mark.parametrize("text", QUERY_SENTENCES)
def test_query_carries_no_schedule_fields(text: str):
    """查询意图不许带 content / scheduled_at。

    一旦它带着这两个字段，下游随时可能拿它去 create_reminder——
    本次要修的就是这个 bug，不能在查询分支里再留一条后路。
    """
    entities = _parse(text)[0].entities
    assert not entities.get("content"), f"{text!r} 抽出了 content={entities.get('content')!r}"
    assert not entities.get("scheduled_at"), f"{text!r} 抽出了 scheduled_at"


def test_pattern_matches_query_not_bare_noun():
    """只认搭配，不认裸名词。

    单靠"提醒""日程"这些名词分不开两句：都含"提醒"。
    查询必须"查询动词 + 日程名词"或"限定词 + 日程名词"成对出现。
    """
    assert SCHEDULE_QUERY_PATTERN.search("看看我的提醒")
    assert SCHEDULE_QUERY_PATTERN.search("今天的安排")
    # 只有名词、没有查询语义的，不能匹配
    assert not SCHEDULE_QUERY_PATTERN.search("提醒我九点关灯")
    assert not SCHEDULE_QUERY_PATTERN.search("别忘了带钥匙")


# ---------------------------------------------------------------- 链路层

@pytest.mark.parametrize("text", QUERY_SENTENCES)
def test_query_creates_nothing(client: TestClient, text: str):
    """核心断言：问一句查询，日程表必须一条都没多。

    这条是本次 bug 的照妖镜。之前"看看今天的提醒"会建出
    标题「看看」、时间今天 09:00 的提醒，日程数从 0 变 1。
    """
    before = len(get_schedules("family_sz"))
    _ask(client, text)
    after = len(get_schedules("family_sz"))
    assert after == before, (
        f"{text!r} 让日程从 {before} 条变成 {after} 条——查询不该有副作用"
    )


def test_query_answer_is_not_chitchat_filler(client: TestClient):
    """应答不能说"我在听，可以说我喜欢…"。

    list_schedule 一开始只回 `count: 0` 没有 message，应答节点认为
    "这次调用无话可说"，直接掉进闲聊兜底模板：数据全对，话说得完全不搭。
    """
    answer = _ask(client, "看看今天的提醒").get("answer", "")
    assert answer, "查询返回了空应答"
    assert "我在听" not in answer, f"查询掉进了闲聊兜底：{answer!r}"
    assert "没有任何提醒" in answer or "提醒" in answer, f"应答不搭边：{answer!r}"


def test_query_lists_existing_reminders(client: TestClient):
    """有提醒时要真列出来，不能永远说"没有"。"""
    # 先建一条（走完整两轮确认）
    _ask(client, "提醒我晚上七点浇花")
    _ask(client, "确认", confirmed=True)
    assert len(get_schedules("family_sz")) == 1, "前置条件没成立：提醒没建出来"

    answer = _ask(client, "看看今天的提醒").get("answer", "")
    assert "浇花" in answer, f"列出来了但没带上提醒内容：{answer!r}"


def test_query_does_not_require_confirm(client: TestClient):
    """查询走 list_schedule 且不要求确认。

    查询要"确认"等于每次看日程都得点两下，纯添堵。
    """
    body = _ask(client, "我有哪些提醒")
    assert not body.get("requiresConfirm"), f"查询竟然要求确认：{body}"


def test_create_still_requires_confirm(client: TestClient):
    """对照组：创建仍然要回显确认（FR-SEC-001 防误创建）。"""
    body = _ask(client, "提醒我九点关灯")
    assert body.get("requiresConfirm"), "创建提醒不再要求确认了——误创建口子又开了"
    assert "即将创建" in body.get("answer", "")
    assert len(get_schedules("family_sz")) == 0, "还没确认就把提醒建出来了"


# ---------------------------------------------------------------- 双模同步

def test_llm_prompt_mentions_schedule_query():
    """规则词表与 LLM prompt 必须两边同步（项目铁律）。

    只改规则不改 prompt，会出现"规则模式好用、真模型一跑就翻车"——
    模型那边 schedule 依然既当创建又当查询，同一个 bug 换个模式复发。
    """
    from homemind.config import get_settings
    settings = get_settings()
    original = settings.llm.use_mock
    try:
        settings.llm.use_mock = False
        parser = IntentParser(settings)
        captured: dict[str, str] = {}

        class _StubClient:
            enabled = True

            async def chat_json(self, prompt, system=None):
                captured["prompt"] = prompt
                return []

        import homemind.agent.intent as intent_mod
        original_get = intent_mod.get_llm_client
        intent_mod.get_llm_client = lambda _s: _StubClient()
        try:
            asyncio.run(parser.parse("看看今天的提醒", "family_sz"))
        finally:
            intent_mod.get_llm_client = original_get
    finally:
        settings.llm.use_mock = original

    prompt = captured.get("prompt", "")
    assert "schedule_query" in prompt, "LLM prompt 里没有 schedule_query 这个类型"
    assert "不创建任何东西" in prompt, "prompt 没告诉模型查询是只读的"
