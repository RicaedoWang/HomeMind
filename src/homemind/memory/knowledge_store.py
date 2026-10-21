"""知识库存储：用户导入的设备说明书 / 使用常识（F13 RAG 的数据层）。

V1.5 之前知识库是 `agents/knowledge_agent.py` 里的 `DOCS` 常量——5 个硬编码片段，
用户既不能增、也不能删，检索靠字符 bigram Jaccard 字面匹配。

这一版把「用户能导入的说明书」落库，补齐 RAG 真正缺的那半截：

- **内置常识（DOCS）留代码里**，跟版本走、只读——符合 V1.2「内置留代码、用户落库」。
- **用户导入的说明书落 `knowledge_doc` 表**，可增删改，按 `family_id` 隔离。
- **检索用本地 embedding（真向量）**，模型不可用时退化 Jaccard。

## 为什么向量要落库（BLOB）而不是检索时现算

导入时 embed 一次、存成 BLOB；检索时读出来解码算余弦。如果现算，
每次查询都要把本家庭全部文档重新 embed 一遍（几百块 × 5.6ms = 秒级），
这个延迟用户当场就能感觉到。存 BLOB 后检索只有「读 + 解码 + 点积」，
亚毫秒级。

## 为什么 BLOB 而不是 JSON

512 维 float32 的二进制是 2KB/条；JSON 文本约 5KB 还慢。检索时
`struct.unpack` 解码，纯 C 实现。

## embedding 开关

和 `memory/service.py` 一致：读 `settings.embedding.enabled`。
测试里 conftest 把它设成 false，这里就绝不碰 `get_embedder()`——
不加载 90MB 模型、不联网、可重复。
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

from ..auth.family_store import DEFAULT_DB_PATH
from ..config import get_settings
from ..core.trace import get_logger
from .embedder import get_embedder
from .semantic import cosine_similarity, jaccard_similarity

logger = get_logger("memory.knowledge_store")


def _connect(db_path: str) -> sqlite3.Connection:
    """统一连接入口（委托 core.db，WAL / 外键 / busy_timeout 一起生效）。"""
    from ..core.db import connect

    return connect(db_path)


def _embed_fn():
    """按配置返回 embedding 函数；禁用时返回 None（走 Jaccard）。"""
    if get_settings().embedding.enabled:
        return get_embedder()
    return None


def _pack(vec: list[float]) -> bytes:
    """float32 数组打包成 BLOB。"""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    """BLOB 解码回 float 数组。"""
    n = len(blob) // 4
    if n == 0:
        return []
    return list(struct.unpack(f"{n}f", blob))


def _hash(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def _auto_title(chunk: str, limit: int = 12) -> str:
    """没给标题时，取正文前若干字当标题。"""
    head = chunk.strip().replace("\n", " ")
    return head[:limit] + ("…" if len(head) > limit else "")


# 向量模式下的最低余弦召回阈值。
# bge-small-zh 实测：同义改写 0.6–0.8，无关查询 0.3–0.4。
# 低于 0.4 的属于"字面上勉强沾边、语义上不相关"的噪声，召回会让 LLM
# 依据一段答非所问的片段硬编答案——宁可说"没查到"，也不喂垃圾。
# 注意：这个阈值只对向量计分生效，Jaccard 退化模式仍用 > 0（字面命中本就够相关）。
MIN_COSINE = 0.4


class KnowledgeStore:
    """知识库（设备说明书 / 使用常识）的持久化与检索。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_doc (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id    TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    embedding    BLOB,
                    source       TEXT NOT NULL DEFAULT 'user',
                    content_hash TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    UNIQUE (family_id, content_hash)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_family "
                "ON knowledge_doc(family_id)"
            )

    # ------------------------------------------------------------ 导入

    def import_text(
        self, family_id: str, text: str, title: str = ""
    ) -> dict[str, int]:
        """把一段文本按空行切块导入。返回 {"added": N, "skipped": M}。

        切块的理由：一篇说明书几千字，整段塞进一个 512 维向量会把语义稀释掉，
        按空行切块后每块几百字，检索才精准。去重靠 `content_hash` 唯一键，
        同一家庭重复粘贴同一段不会重复落库。

        embedding 可用时向量化存 BLOB；模型加载失败时该列留 NULL，
        检索时自动退化 Jaccard——文本照样落库，不会因为没模型就丢数据。
        """
        chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
        if not chunks:
            return {"added": 0, "skipped": 0}

        embed = _embed_fn()
        added = skipped = 0
        now = time.time()
        for chunk in chunks:
            h = _hash(chunk)
            if self._exists(family_id, h):
                skipped += 1
                continue
            blob = None
            if embed is not None:
                vec = embed(chunk)  # 失败返回 None，不抛异常（embedder 已封口）
                if vec:
                    blob = _pack(vec)
            chunk_title = title.strip() if title.strip() else _auto_title(chunk)
            with _connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO knowledge_doc
                       (family_id, title, content, embedding, source, content_hash, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (family_id, chunk_title, chunk, blob, "user", h, now),
                )
            added += 1

        if added:
            logger.info("knowledge imported: %s (%d added, %d skipped)",
                        family_id, added, skipped,
                        extra={"extra_fields": {"event": "knowledge.import",
                                                "family_id": family_id}})
        return {"added": added, "skipped": skipped}

    # ------------------------------------------------------------ 检索

    def search(
        self, family_id: str, query: str, top_k: int = 2
    ) -> list[dict[str, Any]]:
        """在本家庭知识库检索，返回 [{title, content, score}] 按分数降序。

        向量模式：query → embedding → 解码 BLOB → 余弦。
        退化模式（模型禁用 / 加载失败 / 该条没存向量）：Jaccard 字面匹配。
        """
        rows = self._load_family(family_id)
        if not rows:
            return []

        embed = _embed_fn()
        query_vec = None
        if embed is not None:
            query_vec = embed(query)  # 失败返回 None → 整体退化 Jaccard

        scored: list[dict[str, Any]] = []
        for row in rows:
            vec = _unpack(row["embedding"]) if row["embedding"] else []
            if query_vec and vec:
                score = cosine_similarity(query_vec, vec)
                # 向量模式下，低分就是无关，直接丢弃（见 MIN_COSINE 的说明）
                if score < MIN_COSINE:
                    continue
            else:
                score = jaccard_similarity(query, row["title"] + row["content"])
            if score > 0:
                scored.append({
                    "id": row["id"],
                    "title": row["title"],
                    "content": row["content"],
                    "score": score,
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------ 列表 / 删除

    def list_docs(self, family_id: str) -> list[dict[str, Any]]:
        """本家庭全部知识条目（不含 embedding，列表页用不着）。"""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT id, title, content, source, created_at
                   FROM knowledge_doc WHERE family_id=? ORDER BY id""",
                (family_id,),
            ).fetchall()
        return [{
            "id": r["id"],
            "title": r["title"],
            "content": r["content"],
            "source": r["source"],
            "created_at": r["created_at"],
        } for r in rows]

    def delete(self, family_id: str, doc_id: int) -> bool:
        """删一条。只删 `source='user'` 的——内置常识不落库，这层过滤是防御性的：
        万一哪天内置片段也进来了，删除入口也不能把它们删掉（内置只读）。"""
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_doc WHERE family_id=? AND id=? AND source='user'",
                (family_id, doc_id),
            )
        if cur.rowcount:
            logger.info("knowledge deleted: %s/%s", family_id, doc_id,
                        extra={"extra_fields": {"event": "knowledge.delete",
                                                "family_id": family_id}})
            return True
        return False

    def count(self, family_id: str) -> int:
        with _connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM knowledge_doc WHERE family_id=?",
                (family_id,),
            ).fetchone()[0]

    # ------------------------------------------------------------ 重置（测试）

    def reset_family(self, family_id: str) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM knowledge_doc WHERE family_id=?", (family_id,))

    def reset_all(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM knowledge_doc")

    # ------------------------------------------------------------ 内部

    def _exists(self, family_id: str, content_hash: str) -> bool:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM knowledge_doc WHERE family_id=? AND content_hash=?",
                (family_id, content_hash),
            ).fetchone()
        return row is not None

    def _load_family(self, family_id: str) -> list[sqlite3.Row]:
        with _connect(self.db_path) as conn:
            return conn.execute(
                "SELECT * FROM knowledge_doc WHERE family_id=?",
                (family_id,),
            ).fetchall()


# ---------------------------------------------------------------- 单例与测试接缝

_knowledge_store: KnowledgeStore | None = None


def get_knowledge_store() -> KnowledgeStore:
    global _knowledge_store
    if _knowledge_store is None:
        _knowledge_store = KnowledgeStore()
    return _knowledge_store


def set_knowledge_store(store: KnowledgeStore | None) -> None:
    """测试接缝：换成临时库（与 set_device_store / set_scene_store 同套路）。

    知识库是用户可写的数据，走默认路径就是往真实内容库里写测试文档——
    和家庭 / 设备 / 场景一模一样的风险，所以留同一个接缝。
    """
    global _knowledge_store
    _knowledge_store = store
