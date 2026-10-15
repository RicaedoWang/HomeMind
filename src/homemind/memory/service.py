"""统一记忆服务（对齐 FR-MEM-004 ~ FR-MEM-008）。

这是整个记忆系统的门面，把三层存储整合成四个动作：

    write    写入：结构化落 SQLite + 文本进语义库（双写）
    recall   召回：语义相似度 × 时效衰减 × 重要度 加权排序
    update   更新：同 key 冲突检测，版本 +1 并留痕
    forget   遗忘：硬删除，两个库一起清

召回排序公式（FR-MEM-005，面试必考）：

    score = α × semantic_similarity
          + β × time_decay
          + γ × importance

    time_decay = exp(-λ × Δt)      Δt 为距上次访问的天数
    默认 α=0.6, β=0.2, γ=0.2, λ=0.01（可配置）

为什么不是简单 topK：
纯语义相似会把"三年前随口一提的偏好"和"昨天刚说的习惯"排在一起。
时效衰减让新鲜记忆优先，重要度让关键偏好不被埋没。
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from ..config import Settings, get_settings
from ..core.tenancy import DEFAULT_FAMILY_ID
from ..core.trace import get_logger, span
from .long_term import (
    TYPE_DEVICE_HABIT,
    TYPE_PREFERENCE,
    TYPE_SCENE,
    LongTermMemory,
    role_level,
)
from .semantic import SemanticMemory, jaccard_similarity
from .short_term import ShortTermMemory

logger = get_logger("memory.service")

# ---------------- 记忆抽取规则（FR-MEM-004）----------------
# 偏好表达：描述自己的喜好 → 值前面补"喜欢"
PREF_WORDS = ("我喜欢", "我爱", "我习惯", "我偏好", "我一般", "我通常", "我总是")
# 记忆指令：让 Agent 记住某件事 → 原样存储，绝不能加"喜欢"前缀，
# 否则「记住下周三开家长会」会变成「喜欢下周三开家长会」，语义直接反了
MEMO_CMD_WORDS = ("记住", "以后")
NEG_WORDS = ("我不喜欢", "我讨厌", "别给我", "不要")
HABIT_WORDS = ("每天", "每次", "每周", "总是会")
DEVICE_WORDS = ("灯", "空调", "窗帘", "地暖", "电视", "风扇")


def _normalize_preference_key(text: str) -> str | None:
    """把同主题的偏好归一化成同一个 key，让"改口"触发覆盖而不是堆积。

    问题背景：
        「喜欢睡觉时空调26度」→ key = preference:睡觉时空调26度
        「喜欢空调开24度」   → key = preference:空调开24度
    两条 key 不同，于是并存，Agent 靠相似度挑，很可能挑中过时的那条。
    用户明明已经改口，Agent 还在按旧习惯干活——这是记忆系统最招骂的 bug。

    归一化后两条都变成 preference:空调温度 → 第二次写入触发冲突更新（版本+1），
    旧值进历史，Agent 只会看到最新的那条。
    """
    if re.search(r"\d+(?:\.\d+)?\s*度", text):
        device = next((d for d in DEVICE_WORDS if d in text), None)
        if device:
            return f"preference:{device}温度"
    return None


class MemoryService:
    """统一记忆服务。"""

    def __init__(
        self,
        long_term: LongTermMemory | None = None,
        semantic: SemanticMemory | None = None,
        short_term: ShortTermMemory | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.long_term = long_term or LongTermMemory()
        self.semantic = semantic or SemanticMemory()
        self.short_term = short_term or ShortTermMemory()

    # ---------------- 写入 ----------------

    def extract_memories(self, text: str) -> list[tuple[str, str, str]]:
        """从用户输入里抽取记忆点。

        返回 [(memory_type, key, value)]。
        规则抽取优先，V0.8 可换成模型抽取，接口不变。
        """
        found: list[tuple[str, str, str]] = []
        cleaned = text.strip()

        has_neg = any(w in cleaned for w in NEG_WORDS)
        has_memo_cmd = any(w in cleaned for w in MEMO_CMD_WORDS)
        has_pref = any(w in cleaned for w in PREF_WORDS)
        has_habit = any(w in cleaned for w in HABIT_WORDS)
        has_device = any(d in cleaned for d in DEVICE_WORDS)

        if has_pref or has_neg or has_memo_cmd:
            # 找出触发词，取它之后的内容作为记忆主体
            trigger = None
            for word in list(PREF_WORDS) + list(MEMO_CMD_WORDS) + list(NEG_WORDS):
                if word in cleaned:
                    trigger = word
                    break
            body = cleaned.split(trigger, 1)[1] if trigger else cleaned
            body = re.sub(r"[，,。！!？?、\s]+$", "", body.strip())
            if body:
                if trigger in MEMO_CMD_WORDS:
                    # 「记住X」不是喜好，原样存，别给人家加"喜欢"
                    value = body
                elif has_neg and not any(w in body for w in ("不喜欢", "讨厌", "不要")):
                    value = f"不喜欢{body}"
                elif not has_neg and not body.startswith(("喜欢", "习惯", "偏好")):
                    value = f"喜欢{body}"
                else:
                    value = body
                # 能归一化出主题的用主题 key（改口会覆盖），否则退回文本 key
                key = _normalize_preference_key(body) or f"preference:{body[:16]}"
                found.append((TYPE_PREFERENCE, key, value))

        if has_habit and has_device:
            device = next((d for d in DEVICE_WORDS if d in cleaned), "设备")
            key = f"device_habit:{device}"
            found.append((TYPE_DEVICE_HABIT, key, cleaned))

        # 场景类：只在前面没抽出别的记忆时才写。
        # 否则"我喜欢睡觉时把窗帘关掉"会同时写出 preference 和 scene 两条，
        # 内容几乎一样，用户看到"记了两条"只会觉得 Agent 有病。
        if not found and any(w in cleaned for w in ("聚会", "睡觉", "回家", "出门", "看电影")):
            scene = next(w for w in ("聚会", "睡觉", "回家", "出门", "看电影") if w in cleaned)
            key = f"scene:{scene}"
            found.append((TYPE_SCENE, key, cleaned))

        return found

    def write(
        self,
        member_id: str,
        text: str,
        memory_type: str | None = None,
        memory_key: str | None = None,
        importance: float = 0.6,
        visible_role: str = "L3",
        family_id: str = DEFAULT_FAMILY_ID,
    ) -> list[dict[str, Any]]:
        """写入记忆：规则抽取 + 双写（结构化库 + 语义库）。

        两种用法：
        1. 只传 text：走规则抽取，适合"我喜欢XX"这类自然表达。
        2. 传 memory_type（可选 memory_key）：整句直接入库，
           适合日程事实、公共信息等抽取规则覆盖不到的内容。

        memory_key 相同即视为同一条记忆 → 触发冲突更新（版本 +1 留痕）。
        返回写入成功的记忆列表。
        """
        if memory_type:
            key = memory_key or f"{memory_type}:{text[:16]}"
            items = [(memory_type, key, text)]
        else:
            items = self.extract_memories(text)

        written: list[dict[str, Any]] = []
        for mtype, key, value in items:
            # 语义去重：新内容和库里某条高度相似 → 视为同一条，覆盖而非新增。
            # key 归一化只能挡住"空调温度"这类规则可归纳的，
            # "喜欢睡觉的时候把窗帘关掉" 和 "我喜欢睡觉的时候把窗帘关掉"
            # 这种表述差异要靠相似度兜底。
            dup = self.find_duplicate(member_id, value, family_id=family_id)
            if dup is not None:
                existing, sim = dup
                key = existing.memory_key
                importance = max(importance, existing.importance)
                logger.info("duplicate content (%.2f), merging into %s", sim, key)

            record = self.long_term.upsert(
                member_id=member_id, memory_type=mtype, memory_key=key,
                memory_value=value, importance=importance,
                visible_role=visible_role, family_id=family_id,
            )
            # 语义库同步（双写一致性）
            self.semantic.add(
                memory_id=record.memory_id, text=value,
                member_id=member_id, visible_role=visible_role,
            )
            written.append(record.to_dict())

        if written:
            logger.info("memory written: %s 条", len(written),
                        extra={"extra_fields": {"event": "memory.write",
                                                "count": len(written)}})
        return written

    # ---------------- 召回 ----------------

    def recall(
        self,
        query: str,
        member_id: str | None = None,
        viewer_role: str = "L4",
        top_k: int = 5,
        family_id: str = DEFAULT_FAMILY_ID,
    ) -> list[dict[str, Any]]:
        """记忆召回，返回按综合分排序的记忆列表（只回本家庭的）。

        语义层按 member_id 过滤，理论上已经隔开了两个家庭；
        这里再按 family_id 拦一道是**纵深防御**——
        万一哪天 member_id 撞号（两家都叫 member_001），
        召回阶段也不能把别家的偏好喂进 prompt。
        """
        cfg = self.settings.memory
        now = time.time()

        with span("memory.recall", logger, query=query):
            hits = self.semantic.search(
                query, top_k=top_k * 3, member_id=member_id, viewer_role=viewer_role
            )

            results: list[dict[str, Any]] = []
            for memory_id, similarity in hits:
                record = self.long_term.get_by_id(memory_id)
                if record is None:
                    continue
                # 权限二次校验：召回阶段就过滤，敏感内容不能进上下文
                if role_level(record.visible_role) > role_level(viewer_role):
                    continue
                # 租户二次校验：跨家庭的记忆绝不进上下文
                if record.family_id != family_id:
                    logger.warning("cross-family recall blocked: %s", record.family_id,
                                   extra={"extra_fields": {"event": "memory.cross_family_block",
                                                           "family": record.family_id}})
                    continue

                days = (now - record.last_access_at) / 86400.0
                time_decay = math.exp(-cfg.decay_lambda * days)
                score = (
                    cfg.alpha_semantic * similarity
                    + cfg.beta_time_decay * time_decay
                    + cfg.gamma_importance * record.importance
                )
                results.append({
                    **record.to_dict(),
                    "semantic": round(similarity, 4),
                    "time_decay": round(time_decay, 4),
                    "score": round(score, 4),
                })

            results.sort(key=lambda x: x["score"], reverse=True)
            top = results[:top_k]
            for item in top:
                self.long_term.touch(item["memory_id"])

            logger.info("memory recalled: %s 条", len(top),
                        extra={"extra_fields": {"event": "memory.recall",
                                                "hits": len(top)}})
            return top

    # ---------------- 遗忘与去重 ----------------

    def find_duplicate(
        self,
        member_id: str,
        text: str,
        threshold: float = 0.85,
        family_id: str = DEFAULT_FAMILY_ID,
    ) -> tuple[Any, float] | None:
        """查找与 text 内容高度相似的已有记忆，返回 (记录, 相似度) 或 None。

        只在**本家庭内**比对：两家的"喜欢空调 26 度"是两条各自独立的记忆，
        不能因为内容像就合并成一条。
        """
        best: tuple[Any, float] | None = None
        for r in self.long_term.list_by_member(member_id, viewer_role="L4",
                                               family_id=family_id):
            sim = jaccard_similarity(text, r.memory_value)
            if sim >= threshold and (best is None or sim > best[1]):
                best = (r, sim)
        return best

    def deduplicate(self, member_id: str | None = None, threshold: float = 0.85) -> int:
        """扫描并合并存量重复记忆：内容高度相似的只保留最新一条。

        写入时的去重防得住"以后"，但防不住"已经脏了的库"——
        规则修复前写进去的重复数据还在，必须存量清理。
        保留策略：updated_at 最新的优先（最新的表达最接近用户当前意图）。
        返回删除的条数。
        """
        records = (self.long_term.list_by_member(member_id, viewer_role="L4")
                   if member_id else self.long_term.list_all())
        records.sort(key=lambda r: r.updated_at, reverse=True)

        kept: list[Any] = []
        removed = 0
        for r in records:
            dup_of = next(
                (k for k in kept
                 if jaccard_similarity(r.memory_value, k.memory_value) >= threshold),
                None,
            )
            if dup_of is None:
                kept.append(r)
            else:
                self.semantic.delete(r.memory_id)
                self.long_term.delete_by_id(r.memory_id)
                removed += 1
                logger.info("dedup merged: 「%s」 into 「%s」",
                            r.memory_value, dup_of.memory_value,
                            extra={"extra_fields": {"event": "memory.dedup",
                                                    "removed": r.memory_key}})
        return removed

    def forget(self, member_id: str, memory_key: str,
               family_id: str = DEFAULT_FAMILY_ID) -> bool:
        """主动遗忘（硬删除，双库同清，NFR-COMP-001）。只删本家庭的。"""
        record = None
        for r in self.long_term.list_by_member(member_id, viewer_role="L4",
                                               family_id=family_id):
            if r.memory_key == memory_key:
                record = r
                break
        if record is None:
            return False
        self.semantic.delete(record.memory_id)
        return self.long_term.delete(member_id, memory_key, family_id=family_id)

    def forget_by_keyword(self, member_id: str, keyword: str,
                          family_id: str = DEFAULT_FAMILY_ID) -> int:
        """按关键词批量遗忘。"""
        removed = 0
        for r in self.long_term.list_by_member(member_id, viewer_role="L4",
                                               family_id=family_id):
            if keyword in r.memory_value or keyword in r.memory_key:
                if self.forget(member_id, r.memory_key, family_id=family_id):
                    removed += 1
        return removed

    def decay(self) -> int:
        """自然遗忘：低频冷记忆清理。"""
        return self.long_term.decay(decay_lambda=self.settings.memory.decay_lambda)

    # ---------------- 辅助 ----------------

    def stats(self) -> dict[str, Any]:
        """记忆系统状态，用于演示与监控。"""
        records = self.long_term.list_all()
        return {
            "semantic_mode": self.semantic.mode,
            "short_term_mode": self.short_term.mode,
            "long_term_count": len(records),
            "members": sorted({r.member_id for r in records}),
        }


_service: MemoryService | None = None


def get_memory_service() -> MemoryService:
    """获取记忆服务单例。"""
    global _service
    if _service is None:
        _service = MemoryService()
    return _service
