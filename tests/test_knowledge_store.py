"""知识库存储层回归（F13 RAG 数据层）。

覆盖：
- 导入按空行切块 + content_hash 去重
- 检索的 Jaccard 退化路径（测试环境无 embedding）
- 检索的 embedding 真向量路径（monkeypatch 假 embed_fn）
- 租户隔离（A 家导入的 B 家查不到）
- 删除只删 source='user'

测试环境 HM_EMBEDDING_ENABLED=false（conftest 强制），所以默认走 Jaccard；
embedding 路径用 monkeypatch 替换 `_embed_fn`，不加载真模型、不联网。
"""

from __future__ import annotations

import sqlite3

import pytest

from homemind.memory.knowledge_store import KnowledgeStore, _pack, _unpack


@pytest.fixture
def store(tmp_path):
    return KnowledgeStore(db_path=str(tmp_path / "kb.db"))


# ---------------------------------------------------------------- 导入

def test_import_splits_chunks_by_blank_line(store):
    result = store.import_text("f1", "空调滤网清洗\n\n空调省电温度\n\n门锁换电池")
    assert result == {"added": 3, "skipped": 0}
    assert store.count("f1") == 3


def test_import_dedup_same_content(store):
    store.import_text("f1", "空调滤网清洗\n\n空调省电温度")
    result = store.import_text("f1", "空调滤网清洗\n\n洗衣机图标含义")
    # "空调滤网清洗" 重复 → skipped；"洗衣机图标含义" 新 → added
    assert result == {"added": 1, "skipped": 1}
    assert store.count("f1") == 3


def test_import_blank_text_returns_zero(store):
    assert store.import_text("f1", "   \n\n  ") == {"added": 0, "skipped": 0}
    assert store.count("f1") == 0


# ---------------------------------------------------------------- 检索

def test_search_fallback_jaccard(store):
    """无 embedding 时走字面匹配，字面重合就能命中。"""
    store.import_text("f1", "空调滤网建议每两周清洗一次")
    hits = store.search("f1", "空调滤网", top_k=1)
    assert hits, "字面匹配应能命中"
    assert "滤网" in hits[0]["content"]
    assert 0 < hits[0]["score"] < 1  # Jaccard 在 (0,1) 区间


def test_search_uses_embedding(monkeypatch, tmp_path):
    """embedding 可用时走向量余弦，字面零重合也能召回同义改写。"""
    store = KnowledgeStore(db_path=str(tmp_path / "kb.db"))

    # 假 embed：把"空调太冷"和"把温度调高点"映射到相同向量（模拟同义改写）。
    # 这两句字面一个重合的字都没有，Jaccard 必为 0，只有向量能召回。
    def fake_embed(text):
        return {"空调太冷": [1.0, 0.0], "把温度调高点": [1.0, 0.0]}.get(
            text, [0.0, 0.0]
        )

    monkeypatch.setattr("homemind.memory.knowledge_store._embed_fn", lambda: fake_embed)

    store.import_text("f1", "空调太冷")
    hits = store.search("f1", "把温度调高点", top_k=1)

    assert hits, "embedding 模式下同义改写应召回"
    assert hits[0]["content"] == "空调太冷"
    assert hits[0]["score"] == pytest.approx(1.0)  # 向量相同 → 余弦 1.0


def test_search_vector_filters_low_score_noise(monkeypatch, tmp_path):
    """向量模式下，低于阈值的余弦分数（无关查询）不该被召回。

    真模型实测：同义改写余弦 0.6–0.8，无关查询 0.3–0.4。若不设阈值，
    无关查询会召回一段答非所问的片段，LLM 被要求"只依据片段回答"，
    只能硬编一个驴唇不对马嘴的答案。宁可说没查到，也不喂垃圾。
    """
    store = KnowledgeStore(db_path=str(tmp_path / "kb.db"))

    def fake_embed(text):
        # 文档 [1,0]；相关查询 [0.99,0.1]→余弦≈0.995；无关查询 [0.3,0.95]→余弦≈0.301
        return {
            "空调太冷": [1.0, 0.0],
            "把温度调高点": [0.99, 0.1],
            "今天股票怎么样": [0.3, 0.95],
        }.get(text, [0.0, 0.0])

    monkeypatch.setattr("homemind.memory.knowledge_store._embed_fn", lambda: fake_embed)

    store.import_text("f1", "空调太冷")
    assert store.search("f1", "把温度调高点"), "相关查询（余弦≈0.995）应召回"
    assert store.search("f1", "今天股票怎么样") == [], "无关查询（余弦≈0.301）应被阈值过滤"


def test_search_no_match_returns_empty(store):
    store.import_text("f1", "空调滤网清洗")
    assert store.search("f1", "明天天气怎么样") == []


# ---------------------------------------------------------------- 租户隔离

def test_tenant_isolation(store):
    store.import_text("f1", "空调滤网建议每两周清洗")
    assert store.search("f2", "空调滤网") == [], "别家的说明书不该被查到"
    assert store.count("f2") == 0


def test_same_content_in_two_families_both_kept(store):
    """同一段说明书，两家各自导入，互不覆盖（family_id 参与唯一键）。"""
    store.import_text("f1", "空调滤网清洗")
    store.import_text("f2", "空调滤网清洗")
    assert store.count("f1") == 1
    assert store.count("f2") == 1


# ---------------------------------------------------------------- 列表 / 删除

def test_list_docs(store):
    store.import_text("f1", "空调滤网清洗\n\n空调省电温度")
    docs = store.list_docs("f1")
    assert len(docs) == 2
    assert docs[0]["source"] == "user"
    assert "embedding" not in docs[0]  # 列表接口不吐向量


def test_delete_removes_doc(store):
    store.import_text("f1", "空调滤网清洗")
    doc_id = store.list_docs("f1")[0]["id"]
    assert store.delete("f1", doc_id) is True
    assert store.count("f1") == 0
    assert store.search("f1", "空调滤网") == []


def test_delete_nonexistent_returns_false(store):
    assert store.delete("f1", 999) is False


def test_delete_cannot_touch_other_family(store):
    store.import_text("f1", "空调滤网清洗")
    doc_id = store.list_docs("f1")[0]["id"]
    # B 家拿着 A 家的 doc_id 去删 → 删不掉（family_id 不匹配）
    assert store.delete("f2", doc_id) is False
    assert store.count("f1") == 1


# ---------------------------------------------------------------- BLOB 打包

def test_pack_unpack_roundtrip():
    vec = [0.1, -0.2, 0.3, 0.5, 0.0]
    blob = _pack(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == 5 * 4  # float32 = 4 字节
    assert _unpack(blob) == pytest.approx(vec)


def test_embedding_column_null_without_embedder(tmp_path):
    """无 embedding 时向量列留 NULL，文本照样落库（不丢数据）。"""
    store = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    store.import_text("f1", "空调滤网清洗")
    con = sqlite3.connect(str(tmp_path / "kb.db"))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT embedding FROM knowledge_doc WHERE family_id='f1'").fetchone()
    con.close()
    assert row["embedding"] is None
