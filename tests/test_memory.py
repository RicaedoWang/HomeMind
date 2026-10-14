"""记忆系统回归测试（V0.5）。

每条用例对应 SRS 一条验收标准：
    test_write_and_recall       → FR-MEM-003/004/005 写入与语义召回
    test_recall_score_formula   → FR-MEM-005 排序公式可解释
    test_conflict_update        → FR-MEM-006 冲突更新版本留痕
    test_permission_filter      → FR-MEM-008 权限在召回阶段过滤
    test_hard_delete           → FR-MEM-007/NFR-COMP-001 主动遗忘硬删除
    test_decay                 → FR-MEM-007 自然遗忘衰减
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.semantic import SemanticMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.memory.short_term import ShortTermMemory  # noqa: E402

FATHER = "member_father"


@pytest.fixture()
def service(tmp_path):
    """每个用例用独立临时库，互不污染。"""
    long_term = LongTermMemory(db_path=str(tmp_path / "test_memory.db"))
    return MemoryService(
        long_term=long_term,
        semantic=SemanticMemory(),
        short_term=ShortTermMemory(),
    )


def test_write_and_recall(service):
    """写入偏好后，语义相近的查询能召回它。"""
    written = service.write(FATHER, "我喜欢睡觉时空调26度")
    assert len(written) >= 1

    results = service.recall("睡觉的时候空调开多少", member_id=FATHER, top_k=3)
    assert len(results) >= 1
    assert any("26度" in r["memory_value"] for r in results)


def test_recall_score_formula(service):
    """召回结果必须带可解释的分数分解，而不是只给个排序。"""
    service.write(FATHER, "我喜欢睡觉时空调26度", importance=0.8)
    results = service.recall("睡觉空调", member_id=FATHER, top_k=1)
    assert results, "应能召回"

    top = results[0]
    for field in ("semantic", "time_decay", "importance", "score"):
        assert field in top, f"召回结果缺少 {field} 字段"

    # 校验分数确实按 α=0.6 β=0.2 γ=0.2 算出
    expected = (0.6 * top["semantic"] + 0.2 * top["time_decay"] + 0.2 * top["importance"])
    assert abs(top["score"] - expected) < 0.01


def test_conflict_update_versions(service):
    """同一 key 反复写入 → 版本递增 + 旧值留痕。"""
    key = "preference:睡觉温度"
    service.write(FATHER, "喜欢睡觉时空调26度", memory_type="preference", memory_key=key)
    service.write(FATHER, "喜欢睡觉时空调24度", memory_type="preference", memory_key=key)
    service.write(FATHER, "喜欢睡觉时空调25度", memory_type="preference", memory_key=key)

    records = [m for m in service.long_term.list_all() if m.memory_key == key]
    assert len(records) == 1, "同 key 应只有一条记录，不能堆积"
    assert records[0].version == 3
    assert records[0].memory_value == "喜欢睡觉时空调25度"

    history = service.long_term.history(key)
    assert len(history) == 2, "两次更新应留两条变更历史"
    assert history[0]["old_value"] == "喜欢睡觉时空调26度"
    assert history[1]["new_value"] == "喜欢睡觉时空调25度"


def test_permission_filter(service):
    """儿童视角不得召回家长的高权限记忆，且是召回阶段过滤。"""
    service.write(FATHER, "下周三下午三点开家长会",
                  memory_type="schedule_fact", visible_role="L4")
    service.write(FATHER, "家里 WiFi 密码是 home2026",
                  memory_type="family_public", visible_role="L1")

    admin = service.recall("家长会", member_id=FATHER, viewer_role="L4", top_k=5)
    assert any("家长会" in r["memory_value"] for r in admin), "管理员应能看到"

    child = service.recall("家长会", member_id=FATHER, viewer_role="L2", top_k=5)
    assert not any("家长会" in r["memory_value"] for r in child), "儿童绝不能看到"

    # 低权限公共记忆儿童可见
    child_public = service.recall("WiFi 密码", member_id=FATHER, viewer_role="L2", top_k=5)
    assert any("WiFi" in r["memory_value"] for r in child_public)


def test_hard_delete(service):
    """主动遗忘必须硬删除，删了就检索不到。"""
    service.write(FATHER, "记住我周末聚会时喜欢把灯光调暗")
    assert service.recall("聚会", member_id=FATHER, top_k=3), "删除前应能召回"

    removed = service.forget_by_keyword(FATHER, "聚会")
    assert removed >= 1

    after = service.recall("聚会", member_id=FATHER, top_k=3)
    assert not after, "硬删除后必须检索不到"


def test_no_duplicate_memory_for_one_sentence(service):
    """一句话不能抽出内容重复的多条记忆。

    用户实测："我喜欢睡觉的时候把窗帘关掉"被记成
    preference 和 scene 两条几乎一样的内容。
    """
    written = service.write(FATHER, "我喜欢睡觉的时候把窗帘关掉")
    values = [w["memory_value"] for w in written]
    assert len(values) == 1, f"一句话只应记一条，实际 {len(values)} 条: {values}"


def test_write_merges_similar_content(service):
    """新写入内容与已有记忆高度相似时，应合并而不是新增。

    场景：库里已有一条旧表达，用户换了个说法再说一遍，
    应该覆盖旧的那条，而不是并存两条几乎一样的。
    """
    # 模拟已有记忆（key 与新抽取的不同，但内容几乎一样）
    service.long_term.upsert(FATHER, "scene", "scene:睡觉",
                             "我喜欢睡觉的时候把窗帘关掉")
    service.semantic.add(1, "我喜欢睡觉的时候把窗帘关掉", FATHER)

    written = service.write(FATHER, "喜欢睡觉的时候把窗帘关掉")
    assert len(service.long_term.list_all()) == 1, \
        f"相似内容应合并成一条，实际 {len(service.long_term.list_all())} 条"
    assert written[0]["memory_key"] == "scene:睡觉", "应沿用已有记忆的 key"


def test_deduplicate_merges_dirty_data(service):
    """存量清理：修复前写进库的重复数据要能扫出来合并掉。"""
    # 模拟脏库：两条 key 不同、内容几乎一样的记忆
    service.long_term.upsert(FATHER, "preference", "k1", "喜欢睡觉的时候把窗帘关掉")
    service.long_term.upsert(FATHER, "scene", "k2", "我喜欢睡觉的时候把窗帘关掉")
    service.semantic.add(1, "喜欢睡觉的时候把窗帘关掉", FATHER)
    service.semantic.add(2, "我喜欢睡觉的时候把窗帘关掉", FATHER)

    removed = service.deduplicate(FATHER)
    assert removed == 1
    assert len(service.long_term.list_all()) == 1

    # 不同内容的记忆绝不能被误合并
    service.long_term.upsert(FATHER, "preference", "k3", "喜欢睡觉时空调26度")
    service.semantic.add(3, "喜欢睡觉时空调26度", FATHER)
    assert service.deduplicate(FATHER) == 0, "不同内容的记忆不应被合并"


def test_decay_cleans_cold_memory(service):
    """长期未访问且重要度衰减到阈值以下的记忆应被清理。"""
    service.write(FATHER, "我喜欢睡觉时空调26度", importance=0.3)

    # 把最后访问时间推到 400 天前
    import sqlite3
    old = time.time() - 400 * 86400
    with sqlite3.connect(service.long_term.db_path) as conn:
        conn.execute("UPDATE memory_struct SET last_access_at=?", (old,))

    cleaned = service.decay()
    assert cleaned >= 1
    assert len(service.long_term.list_all()) == 0
