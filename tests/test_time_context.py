"""时间上下文注入的回归用例（对齐 FR-INT-004）。

回归自 2026-10-20 少爷实测的两个 bug：

1. 问「现在是什么时间」→ 助手答「我这边看不到当前时间，没法准确告诉你」。
2. 「创建今日任务：晚上七点去遛狗」→ 被创建成 2026-10-14T19:00（6 天前），
   刚建出来就显示「逾期 5 天 18 小时」。

同一个根因：**当前时间从未注入给大模型**。
模型没有系统时钟，问它时间只能说不知道；更糟的是意图解析的 prompt 里
把示例写死成 `scheduled_at: ISO8601（如 2026-10-14T09:00:00）`，
用户说"今日"，模型嘴上答应着，手上照着示例抄了个 10-14。

为什么 133 条单测 + 52 条评测全绿也没抓到：
规则解析路径用 `datetime.now()`，日期本来就算对了；
出问题的只有 LLM 分支，而测试强制 mock，LLM 分支压根不执行。
**所以这批用例必须显式造 enabled=True 的假客户端，把 prompt 抓下来断言**——
断言"prompt 里有没有时间"，而不是断言"模型答得对不对"（后者花钱且不确定）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.agent.time_parser import (  # noqa: E402
    describe_now, is_time_question, strip_time_tokens,
)
from homemind.core.llm import LLMClient  # noqa: E402
from homemind.tools.calendar_server import get_schedules, reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """设备与日程是模块级全局字典，不重置结果就不可复现。"""
    reset_devices()
    reset_schedules()
    yield
    reset_devices()
    reset_schedules()


@pytest.fixture()
def graph():
    return build_agent_graph(registry=default_registry())


async def _run(graph, text: str, confirmed: bool = False) -> dict:
    state = initial_state(user_input=text, confirmed=confirmed, trace_id="test")
    return await graph.ainvoke(state)


class _PromptSpy:
    """假模型客户端：只换网络层，把收到的 prompt 全部记下来。

    `chat_json` 刻意复用 LLMClient 的真实实现（它内部调 self.chat），
    这样测的是真实调用链，不是自己山寨的签名。
    """

    def __init__(self, reply: str = "[]") -> None:
        self.reply = reply
        self.enabled = True
        self.prompts: list[str] = []

    async def chat(self, prompt, system="", temperature=None, max_tokens=800):
        self.prompts.append(prompt)
        return self.reply

    chat_json = LLMClient.chat_json
    parse_json = staticmethod(LLMClient.parse_json)


# ---------------- describe_now：说人话的"现在" ----------------

def test_describe_now_contains_date_and_weekday():
    """固定时刻必须给出年月日 + 星期，星期几不能算错。

    为什么要星期：「下周一」「这周五」这类相对表达全靠星期定位，
    只给日期不给星期，模型还得自己推算是期几，多一步就多一次出错机会。
    """
    stamp = datetime(2026, 10, 20, 13, 6)  # 星期二
    text = describe_now(stamp)
    assert "2026年10月20日" in text
    assert "星期二" in text
    assert "13:06" in text


def test_describe_now_defaults_to_real_clock():
    """不传参时必须用真实时钟（这个函数就是给线上用的）。"""
    assert f"{datetime.now().year}年" in describe_now()


# ---------------- 问时间的识别边界 ----------------

@pytest.mark.parametrize("text", [
    "现在是什么时间", "现在几点", "几点了", "今天几号", "今天星期几", "当前时间",
])
def test_is_time_question_accepts_clock_phrases(text):
    assert is_time_question(text) is True


@pytest.mark.parametrize("text", [
    "今天天气怎么样",      # 问天气，不是问时间
    "看看今天的任务",      # 问任务，不是问时间
    "把我喜欢的时间记一下",  # 陈述偏好
    "客厅灯现在开着吗",    # 问设备状态
    "",
])
def test_is_time_question_rejects_non_clock_phrases(text):
    """判定必须收窄：规则模式没有模型兜底，认错一个词就答非所问。"""
    assert is_time_question(text) is False


# ---------------- prompt 必须带时间（本次 bug 的直接防线） ----------------

async def test_intent_prompt_injects_now_and_has_no_hardcoded_date(monkeypatch):
    """意图解析的 prompt 必须带"现在"，且不得再出现写死的示例日期。

    这条是本次 bug 的正中要害：原 prompt 里写着
    `scheduled_at: ISO8601 时间字符串（如 2026-10-14T09:00:00）`，
    模型没有当前日期，就把这个示例当成了"今天"。
    """
    from homemind.agent import intent as intent_mod

    fake = _PromptSpy("[]")
    # get_llm_client 在 _parse_by_llm 里是带 settings 位置参数调的
    monkeypatch.setattr(intent_mod, "get_llm_client", lambda *_a, **_k: fake)
    parser = intent_mod.IntentParser()
    monkeypatch.setattr(parser.settings.llm, "use_mock", False, raising=False)

    await parser.parse("创建今日任务：晚上七点去遛狗")

    assert fake.prompts, "应该真的调用了模型"
    prompt = fake.prompts[0]
    now = datetime.now()
    assert f"{now.year}年{now.month}月{now.day}日" in prompt, \
        "prompt 里必须写明今天几号，否则模型只能瞎猜"
    assert "2026-10-14" not in prompt, \
        "写死的示例日期会被模型照抄（本次 bug 根因），必须换成'用上面的现在推算'"


async def test_respond_prompt_injects_now(monkeypatch):
    """应答生成的 prompt 同样要带时间，否则问"现在几点"只能答"看不到"。"""
    from homemind.agent import graph as graph_mod

    fake = _PromptSpy("现在是下午一点。")
    monkeypatch.setattr(graph_mod, "get_llm_client", lambda: fake)

    state = initial_state(user_input="现在是什么时间", trace_id="test")
    answer = await graph_mod._llm_generate_answer(state, [])

    assert answer == "现在是下午一点。"
    prompt = fake.prompts[0]
    now = datetime.now()
    assert f"{now.year}年{now.month}月{now.day}日" in prompt


# ---------------- 规则模式（模型不可用）也要答得出时间 ----------------

async def test_time_question_answered_without_llm(graph):
    """mock 模式下问时间，必须回真实时间，不能回"我在听"这种模板废话。

    模型有 prompt 里的时间可用；规则模式没有模型，全靠 respond 节点兜底。
    这条用例走的是 conftest 强制的 mock 路径，正好覆盖兜底分支。
    """
    result = await _run(graph, "现在是什么时间")

    answer = result["final_answer"]
    now = datetime.now()
    assert f"{now.month}月{now.day}日" in answer, f"应答里应含今天的日期：{answer}"
    assert "看不到" not in answer
    assert "我在听" not in answer


# ---------------- 今日任务的日期必须是今天（逾期 bug） ----------------

async def test_today_task_scheduled_today_not_six_days_ago(graph):
    """「创建今日任务」的 scheduled_at 必须落在今天。

    回归截图：任务时间被写成 2026-10-14 19:00（比当时早 5 天 18 小时），
    界面显示「逾期 5 天 18 小时」。这里只断言日期是今天——
    至于 19:00 相对此刻是未来还是过去，取决于跑测试的钟点，不该断言。
    """
    await _run(graph, "创建今日任务：晚上七点去遛狗")          # 第一轮回显
    result = await _run(graph, "创建今日任务：晚上七点去遛狗", confirmed=True)

    assert result["requires_confirm"] is False
    assert len(get_schedules()) == 1
    schedule = get_schedules()[0]

    scheduled = datetime.fromisoformat(schedule["scheduled_at"])
    assert scheduled.date() == datetime.now().date(), \
        f"今日任务被排到了 {scheduled}，不是今天"
    assert scheduled.hour == 19


async def test_today_task_rejects_date_drift(graph):
    """反向保险：哪怕当前时刻就在 19:00 之后，日期仍必须是今天。

    逻辑上等价于上一条，但写成独立用例是为了在"晚上跑测试"时也能看出
    到底是日期算错还是钟点已过——失败信息会直接指出日期。
    """
    await _run(graph, "今晚七点提醒我给猫喂药", confirmed=True)
    schedule = get_schedules()[0]
    scheduled = datetime.fromisoformat(schedule["scheduled_at"])
    drift = abs((scheduled.date() - datetime.now().date()).days)
    assert drift == 0, f"日期偏移了 {drift} 天"


# ---------------- 提醒内容不该夹带时间 ----------------

@pytest.mark.parametrize("raw,expected", [
    ("晚上七点去遛狗", "去遛狗"),
    ("明天早上八点开会", "开会"),
    ("下午三点给妈妈打电话", "给妈妈打电话"),
    ("20分钟后关火", "关火"),
    ("浇花", "浇花"),  # 没有时间词就不该动它
])
def test_strip_time_tokens(raw, expected):
    """时间已经由 scheduled_at 表达，标题里再留一份就是重复。"""
    assert strip_time_tokens(raw) == expected


async def test_reminder_content_has_no_time_prefix(graph):
    """端到端确认：回显与落库的提醒标题都不夹带时间。"""
    result = await _run(graph, "晚上八点提醒我给花浇水")
    assert "晚上八点" not in result.get("confirm_prompt", ""), \
        f"回显里时间出现了两遍：{result.get('confirm_prompt')}"

    await _run(graph, "晚上八点提醒我给花浇水", confirmed=True)
    assert get_schedules()[0]["content"] == "给花浇水"
