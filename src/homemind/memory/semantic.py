"""语义记忆（对齐 FR-MEM-003）。

作用：把记忆的文本向量化，支持"意思相近就能召回"，
而不是只能靠关键词精确匹配。

实现分层：
- **真向量模式**：配置了 Embedding 服务时，走真实 embedding + 余弦相似度。
- **降级模式（默认）**：没有 embedding 服务时用字符 bigram 的 Jaccard 相似度。
  它不是真语义，但对中文短文本的相关性判断够用，且**零依赖、必定能跑**。

为什么这么做：
Milvus + Embedding 模型这一套在 V0.8 才接。现在如果把它们当硬依赖，
你这台 8GB 显存的机器连 demo 都跑不起来。先让链路跑通、接口留好，
真接 Milvus 时只换 _encode 的实现，上层一行不改。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from ..core.trace import get_logger

logger = get_logger("memory.semantic")

# 可选的外部 embedding 函数：fn(text) -> list[float]
EmbedFunc = Callable[[str], list[float]]


@dataclass
class SemanticItem:
    """一条语义记忆。"""

    memory_id: int
    text: str
    vector: list[float] | None
    member_id: str
    visible_role: str


def _bigrams(text: str) -> set[str]:
    """生成字符二元组集合（中文短文本的简易特征）。"""
    cleaned = "".join(ch for ch in text if not ch.isspace())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """字符 bigram 的 Jaccard 相似度，作为无 embedding 时的降级方案。"""
    set_a, set_b = _bigrams(a), _bigrams(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """余弦相似度。"""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(x * y for x, y in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(x * x for x in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticMemory:
    """语义记忆：向量化存储与相似度召回。"""

    def __init__(self, embed_fn: EmbedFunc | None = None) -> None:
        self._items: dict[int, SemanticItem] = {}
        self._embed_fn = embed_fn
        self._mode = "vector" if embed_fn else "fallback"
        logger.info("semantic memory mode: %s", self._mode)

    @property
    def mode(self) -> str:
        return self._mode

    def add(
        self,
        memory_id: int,
        text: str,
        member_id: str = "",
        visible_role: str = "L3",
    ) -> None:
        """加入一条语义记忆。"""
        vector = None
        if self._embed_fn is not None:
            try:
                vector = self._embed_fn(text)
            except Exception as exc:  # noqa: BLE001 - embedding 失败不能拖垮写入
                logger.warning("embedding failed (%s), fallback to text mode", exc)
        self._items[memory_id] = SemanticItem(
            memory_id=memory_id, text=text, vector=vector,
            member_id=member_id, visible_role=visible_role,
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        member_id: str | None = None,
        viewer_role: str = "L4",
    ) -> list[tuple[int, float]]:
        """语义召回，返回 [(memory_id, 相似度)] 按分数降序。

        权限在召回阶段就过滤（不是召回后再过滤）——
        否则权限校验形同虚设，敏感内容已经进了模型上下文。
        """
        from .long_term import role_level

        query_vec = None
        if self._embed_fn is not None:
            try:
                query_vec = self._embed_fn(query)
            except Exception:  # noqa: BLE001
                query_vec = None

        level = role_level(viewer_role)
        scored: list[tuple[int, float]] = []
        for item in self._items.values():
            if member_id and item.member_id and item.member_id != member_id:
                continue
            if role_level(item.visible_role) > level:
                continue
            if query_vec is not None and item.vector is not None:
                score = cosine_similarity(query_vec, item.vector)
            else:
                score = jaccard_similarity(query, item.text)
            if score > 0:
                scored.append((item.memory_id, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def delete(self, memory_id: int) -> None:
        """删除语义记忆（与长期记忆硬删除配套）。"""
        self._items.pop(memory_id, None)

    def clear(self) -> None:
        self._items.clear()

    def stats(self) -> dict[str, Any]:
        return {"mode": self._mode, "count": len(self._items)}
