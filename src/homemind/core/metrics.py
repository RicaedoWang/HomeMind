"""大模型用量统计（token 计数 + 可选成本估算）。

## 为什么要有这个模块

面试被问"你一次对话花多少钱""1000 户一年多少成本"，答"花不了多少"
等于没答。但 `core/llm.py` 原来拿到响应体只取了 `message`，把 `usage`
字段整个丢掉了——所以**没有任何数据可讲**。

这个模块只做两件事：

1. **记 token**（事实）：每次调用累加 `prompt_tokens` / `completion_tokens`。
2. **算成本**（可选）：价格从环境变量读，**读不到就不算**，返回 None。

## 为什么价格不写死在代码里

模型价格是会变的，写死就等于埋一颗定时炸弹：哪天价格调整，代码里的
数字还在，讲出去的成本账全是错的。所以价格只从配置读：

    HM_LLM_INPUT_PRICE   每百万输入 token 的价格（美元或其他货币，单位自定）
    HM_LLM_OUTPUT_PRICE  每百万输出 token 的价格

没配 → `estimate_cost()` 返回 None → 调用方必须如实说"我还没配价格"，
而不是编一个数。**宁可显示"未配置"，也不能显示一个错的钱数。**

## 为什么是内存累加而不是落库

这个数据的用途是"讲清楚成本量级"，不是审计账本。进程重启清零可以接受，
反而避免了往用户真实库里塞一张只在开发期有用的表。要长期统计的话，
`get_usage_stats()` 的返回值本身就够写进日志或导出了。
"""

from __future__ import annotations

import os
import threading
from typing import Any

_lock = threading.Lock()

# model -> {"calls", "prompt_tokens", "completion_tokens"}
_usage: dict[str, dict[str, int]] = {}


def record_usage(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """累加一次调用的 token 用量。

    参数一律容错：真实 API 偶尔会缺字段或给 None，这里全部当 0 处理，
    不能让统计逻辑反过来把业务请求搞挂。
    """
    p = int(prompt_tokens or 0)
    c = int(completion_tokens or 0)
    key = model or "unknown"
    with _lock:
        slot = _usage.setdefault(key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        slot["calls"] += 1
        slot["prompt_tokens"] += p
        slot["completion_tokens"] += c


def _price(env_key: str) -> float | None:
    """从环境变量读单价（每百万 token）。未配置或非法 → None。"""
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> dict[str, Any] | None:
    """按配置的单价估算成本。

    返回 None 表示**单价没配**——调用方必须如实说"没配价格"，
    不许把 None 当成 0 然后宣布"成本几乎为零"。
    """
    in_price = _price("HM_LLM_INPUT_PRICE")
    out_price = _price("HM_LLM_OUTPUT_PRICE")
    if in_price is None or out_price is None:
        return None
    total = (prompt_tokens / 1_000_000) * in_price + (completion_tokens / 1_000_000) * out_price
    return {
        "input_price_per_1m": in_price,
        "output_price_per_1m": out_price,
        "total": round(total, 6),
    }


def get_usage_stats() -> dict[str, Any]:
    """返回累计用量。结构固定，前端/接口可以直接吐。

    {
      "total": {"calls": N, "prompt_tokens": N, "completion_tokens": N, "total_tokens": N},
      "by_model": {...},
      "avg_tokens_per_call": float,      # 一次调用平均消耗总 token
      "cost": {...} | None               # 单价未配置时为 None
    }
    """
    with _lock:
        snapshot = {m: dict(v) for m, v in _usage.items()}

    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    by_model: dict[str, dict[str, Any]] = {}

    for model, slot in snapshot.items():
        calls = slot["calls"]
        p = slot["prompt_tokens"]
        c = slot["completion_tokens"]
        total["calls"] += calls
        total["prompt_tokens"] += p
        total["completion_tokens"] += c

        entry: dict[str, Any] = {
            "calls": calls,
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": p + c,
            "avg_tokens_per_call": round((p + c) / calls, 1) if calls else 0.0,
        }
        cost = estimate_cost(p, c)
        if cost is not None:
            entry["cost"] = cost
        by_model[model] = entry

    total_tokens = total["prompt_tokens"] + total["completion_tokens"]
    total["total_tokens"] = total_tokens

    return {
        "total": total,
        "by_model": by_model,
        "avg_tokens_per_call": round(total_tokens / total["calls"], 1) if total["calls"] else 0.0,
        "cost": estimate_cost(total["prompt_tokens"], total["completion_tokens"]),
        "note": (
            "token 数为真实 API 返回的 usage 累计；成本为按环境变量单价估算，"
            "未配置 HM_LLM_INPUT_PRICE / HM_LLM_OUTPUT_PRICE 时不给出金额。"
        ),
    }


def reset_usage() -> None:
    """清零。测试用，也方便演示前重置基线。"""
    with _lock:
        _usage.clear()
