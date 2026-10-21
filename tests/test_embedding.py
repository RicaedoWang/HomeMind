"""本地 embedding 接入的回归。

## 这份用例的三个重点

1. **真向量路径本身**（`SemanticMemory(embed_fn=...)`）——之前这条分支从没被测过，
   因为项目一直在跑 Jaccard 降级。这是"分支永远不执行"的典型：写好了、跑不到、坏了也不知道。

2. **失败必须降级，不能崩**：模型没装、下载失败、单条推理报错，三种情况都不能
   让服务起不来。embedding 是增强，不是硬依赖。

3. **去重不许被向量污染**（最容易被忽略的一条）：
   实测 bge-small-zh 对「把客厅灯打开」和「把客厅灯关掉」给出余弦 **0.855**——
   它分不清开和关。而去重阈值正好 0.85。如果哪天有人"顺手"把 `find_duplicate`
   也改成用向量，"开灯"会把"关灯"的记忆覆盖掉，而且**测试全绿、用户遭殃**。
   所以这里专门锁一条：向量再像，去重也只看字面。

测试环境由 `conftest.py` 强制 `HM_EMBEDDING_ENABLED=false`，不会加载真模型。
下面的假 embedding 都是手写的确定性函数。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from homemind.memory.embedder import LocalEmbedder, get_embedder, set_embedder  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.semantic import SemanticMemory, jaccard_similarity  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402


def _fake_embed(text: str) -> list[float]:
    """按主题返回不同方向的向量，模拟"同义句靠近、异义句远离"。"""
    if "灯" in text:
        return [1.0, 0.0, 0.0]
    if "空调" in text or "温度" in text or "冷" in text or "热" in text:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


# ---------------------------------------------------------------- 真向量路径

def test_vector_mode_marks_itself():
    sm = SemanticMemory(embed_fn=_fake_embed)
    assert sm.mode == "vector"


def test_vector_mode_recalls_same_topic_across_wording():
    """换了说法也要能召回——这正是字面匹配做不到的。"""
    sm = SemanticMemory(embed_fn=_fake_embed)
    sm.add(1, "把客厅灯打开")
    # "开一下灯" 与 "把客厅灯打开" 字面重合很少，但主题相同
    hits = sm.search("开一下灯")
    assert hits and hits[0][0] == 1, f"没召回：{hits}"


def test_vector_mode_does_not_recall_other_topic():
    sm = SemanticMemory(embed_fn=_fake_embed)
    sm.add(1, "把客厅灯打开")   # 方向 [1,0,0]
    hits = sm.search("空调好冷")  # 方向 [0,1,0]，余弦 0
    assert hits == [], f"跨主题不该召回：{hits}"


def test_add_calls_embed_fn_once_per_item():
    calls: list[str] = []

    def spy(text: str) -> list[float]:
        calls.append(text)
        return _fake_embed(text)

    sm = SemanticMemory(embed_fn=spy)
    sm.add(1, "把客厅灯打开")
    sm.add(2, "空调调高点")
    assert len(calls) == 2, f"每条记忆应只算一次向量，实际 {len(calls)} 次"


# ---------------------------------------------------------------- 失败降级

def test_embed_fn_raising_falls_back_to_jaccard():
    """写入时 embedding 报错，这条记忆退化成文本模式，不能整体崩。"""
    def broken(_text: str) -> list[float]:
        raise RuntimeError("model crashed")

    sm = SemanticMemory(embed_fn=broken)
    sm.add(1, "我喜欢空调26度")          # 向量拿不到 → None
    hits = sm.search("我喜欢空调26度")   # 走 jaccard
    assert hits, "降级后连完全相同的一句都召不回来"
    assert hits[0][1] == pytest.approx(1.0)


def test_query_embed_failure_falls_back():
    """检索时 embedding 报错，同样要能降级返回结果。"""
    sm = SemanticMemory(embed_fn=_fake_embed)
    sm.add(1, "把客厅灯打开")

    def broken(_text: str) -> list[float]:
        raise RuntimeError("query embed failed")

    sm._embed_fn = broken
    hits = sm.search("把客厅灯打开")
    assert hits, "query 向量化失败后应该退回字面匹配，而不是返回空"


def test_returns_none_is_treated_as_no_vector():
    """embed_fn 返回 None（不是抛异常）也要能走通。"""
    sm = SemanticMemory(embed_fn=lambda _t: None)
    sm.add(1, "把客厅灯打开")
    assert sm.search("把客厅灯打开"), "返回 None 时应该退化成字面匹配"


# ---------------------------------------------------------------- LocalEmbedder 行为

@pytest.fixture()
def clean_embedder():
    """每条用例前后重置单例，避免互相影响。"""
    set_embedder(None)
    yield
    set_embedder(None)


def test_embedder_freezes_after_load_failure(monkeypatch, clean_embedder):
    """加载失败后要"封口"，不能每条记忆都去重试一次。

    没封口的后果：模型装不上时，每一次写入都要等一次完整的超时/重试，
    实测 fastembed 会重试 3 次共 70 秒——整个服务相当于死了。
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("fastembed"):
            raise ImportError("simulated: fastembed not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    e = LocalEmbedder()
    assert e("把客厅灯打开") is None
    assert e.has_failed is True
    # 第二次调用直接返回，不再尝试 import（用计数器证明）
    attempts_before = e._load_attempts
    assert e("再来一条") is None
    assert e._load_attempts == attempts_before, "失败后不应重复尝试加载"


def test_embedder_empty_text_returns_none(clean_embedder):
    """空文本不该触发模型加载。"""
    e = LocalEmbedder()
    assert e("") is None
    assert e("   ") is None
    assert e.is_ready is False, "空文本竟然把模型加载了"


def test_embedder_singleton(clean_embedder):
    assert get_embedder() is get_embedder()


def test_set_embedder_replaces_singleton(clean_embedder):
    custom = LocalEmbedder(model_name="fake/model")
    set_embedder(custom)
    assert get_embedder() is custom


# ---------------------------------------------------------------- 与 MemoryService 的接线

def test_service_skips_embedder_when_disabled(tmp_path):
    """测试环境 embedding 是关的，所以服务不该注入 embed_fn。"""
    svc = MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "m.db")))
    assert svc.semantic.mode == "fallback", (
        "embedding 关闭时仍然走了向量模式——conftest 的隔离可能失效了"
    )


# ---------------------------------------------------------------- 最关键的一条

def test_dedup_ignores_vectors_even_when_similarity_is_high(tmp_path):
    """去重必须只看字面，不看向量 —— 即使向量认为两条几乎一样。

    实测 bge-small-zh：「把客厅灯打开」vs「把客厅灯关掉」余弦 = 0.855，
    而去重阈值就是 0.85。改用向量的话，用户说完"开灯"会把"关灯"的记忆顶掉。

    这条用例同时验证：即使语义层是向量模式，`find_duplicate` 依然用 Jaccard 判。
    """
    # 这两句在字面上相似度不高（不同字多），向量上却很高
    a, b = "把客厅灯打开", "把客厅灯关掉"
    assert jaccard_similarity(a, b) < 0.85, "前提：字面相似度应低于阈值"
    assert jaccard_similarity(a, b) == pytest.approx(0.4286, abs=0.01)

    # 注意必须用文件库：`:memory:` 每次 connect 都是独立空库，
    # 而 LongTermMemory 每次操作都开新连接，建表在 A 连接、查表在 B 连接就找不到了。
    svc = MemoryService(long_term=LongTermMemory(db_path=str(tmp_path / "dedup.db")))
    # 故意把语义层换成"什么都判很像"的向量函数，模拟真模型的 0.855 行为
    svc.semantic._embed_fn = lambda _t: [1.0, 0.0, 0.0]

    svc.long_term.upsert(
        family_id="family_sz", member_id="m1", memory_type="preference",
        memory_key="pref:light", memory_value=b, visible_role="L4",
    )
    dup = svc.find_duplicate("m1", a, family_id="family_sz")
    assert dup is None, (
        f"「{a}」被判定与「{b}」重复（相似度 {dup[1] if dup else '-'}）——"
        f"去重改用向量了？这两句是反义，合并会丢数据"
    )
