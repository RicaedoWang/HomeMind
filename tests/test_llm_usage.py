"""大模型用量统计回归。

覆盖：
- token 累加（多次调用、多模型分别统计）
- 平均每次调用的 token 数
- 单价未配置时 `cost` 必须是 None（**不许把 None 当 0 然后宣布"成本为零"**）
- 单价配置后能算出金额
- `reset_usage` 清零
- `LLMClient.chat` 真的把响应里的 usage 记下来了

最后一条用 monkeypatch 换掉 httpx.AsyncClient，不联网、不花钱。
"""

from __future__ import annotations

import pytest

from homemind.core import metrics


@pytest.fixture(autouse=True)
def _clean_usage():
    """每个用例前后都清零，避免互相污染（模块级全局状态）。"""
    metrics.reset_usage()
    yield
    metrics.reset_usage()


# ---------------------------------------------------------------- 计数

def test_record_accumulates():
    metrics.record_usage("deepseek-flash", 100, 20)
    metrics.record_usage("deepseek-flash", 300, 60)
    stats = metrics.get_usage_stats()
    assert stats["total"]["calls"] == 2
    assert stats["total"]["prompt_tokens"] == 400
    assert stats["total"]["completion_tokens"] == 80
    assert stats["total"]["total_tokens"] == 480


def test_by_model_split():
    """不同模型分开统计——大小模型分级的成本账就靠这个。"""
    metrics.record_usage("deepseek-flash", 100, 10)
    metrics.record_usage("deepseek-v4-pro", 900, 200)
    by_model = metrics.get_usage_stats()["by_model"]
    assert by_model["deepseek-flash"]["total_tokens"] == 110
    assert by_model["deepseek-v4-pro"]["total_tokens"] == 1100


def test_avg_tokens_per_call():
    metrics.record_usage("m", 100, 50)
    metrics.record_usage("m", 300, 150)
    stats = metrics.get_usage_stats()
    assert stats["avg_tokens_per_call"] == 300.0


def test_empty_stats_is_zero_not_crash():
    stats = metrics.get_usage_stats()
    assert stats["total"]["calls"] == 0
    assert stats["avg_tokens_per_call"] == 0.0


def test_record_tolerates_none_and_negative_garbage():
    """真实 API 偶尔缺字段，统计逻辑不能反过来把业务搞挂。"""
    metrics.record_usage("m", None, None)  # type: ignore[arg-type]
    stats = metrics.get_usage_stats()
    assert stats["total"]["calls"] == 1
    assert stats["total"]["total_tokens"] == 0


# ---------------------------------------------------------------- 成本

def test_cost_is_none_when_prices_not_configured(monkeypatch):
    """没配单价就不给金额——宁可不报，也不能报个错数。"""
    monkeypatch.delenv("HM_LLM_INPUT_PRICE", raising=False)
    monkeypatch.delenv("HM_LLM_OUTPUT_PRICE", raising=False)
    metrics.record_usage("m", 1_000_000, 500_000)
    stats = metrics.get_usage_stats()
    assert stats["cost"] is None
    assert "cost" not in stats["by_model"]["m"]


def test_cost_computed_when_prices_configured(monkeypatch):
    monkeypatch.setenv("HM_LLM_INPUT_PRICE", "0.5")
    monkeypatch.setenv("HM_LLM_OUTPUT_PRICE", "1.5")
    metrics.record_usage("m", 1_000_000, 1_000_000)
    cost = metrics.get_usage_stats()["cost"]
    assert cost is not None
    assert cost["total"] == pytest.approx(2.0)
    assert cost["input_price_per_1m"] == 0.5


def test_invalid_price_ignored(monkeypatch):
    """单价写成中文或负数，一律当没配，不许算出个负数成本。"""
    monkeypatch.setenv("HM_LLM_INPUT_PRICE", "免费")
    monkeypatch.setenv("HM_LLM_OUTPUT_PRICE", "-1")
    assert metrics.estimate_cost(1_000_000, 1_000_000) is None


# ---------------------------------------------------------------- 接进 LLM 客户端

class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


async def test_chat_records_usage_from_response(monkeypatch):
    """LLMClient.chat 必须把响应体里的 usage 记下来。

    这条守的是那个具体的历史 bug：原来 `data["choices"]` 取了内容，
    usage 整个被丢掉，于是"一次对话花多少钱"永远答不上来。
    """
    import httpx

    payload = {
        "choices": [{"message": {"content": "客厅灯已经打开了。"}}],
        "usage": {"prompt_tokens": 128, "completion_tokens": 32},
    }

    class _FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse(payload)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    from homemind.core.llm import LLMClient

    answer = await LLMClient().chat("把客厅灯打开")

    assert answer == "客厅灯已经打开了。"
    stats = metrics.get_usage_stats()
    assert stats["total"]["calls"] == 1
    assert stats["total"]["prompt_tokens"] == 128
    assert stats["total"]["completion_tokens"] == 32


async def test_chat_without_usage_field_still_records_call(monkeypatch):
    """响应里没 usage 字段（老模型/代理）也要记一次调用，token 记 0。"""
    import httpx

    payload = {"choices": [{"message": {"content": "好的"}}]}

    class _FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse(payload)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    from homemind.core.llm import LLMClient

    await LLMClient().chat("你好")
    stats = metrics.get_usage_stats()
    assert stats["total"]["calls"] == 1
    assert stats["total"]["total_tokens"] == 0
