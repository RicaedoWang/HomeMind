"""记忆系统演示（V0.5）。

运行：python scripts/demo_memory.py

演示覆盖 SRS 里记忆模块的全部验收点：
    1. 记忆写入（抽取 + 双写）
    2. 语义召回 + 排序公式明细
    3. 冲突更新（版本 +1 留痕）
    4. 权限隔离（儿童读不到家长记忆）
    5. 主动遗忘（硬删除后检索不到）
    6. 自然遗忘（冷记忆衰减清理）
"""

from __future__ import annotations

import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from homemind.config import get_settings  # noqa: E402
from homemind.core.trace import setup_logging  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.semantic import SemanticMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.memory.short_term import ShortTermMemory  # noqa: E402

FATHER = "member_father"   # L4 管理员
CHILD = "member_child"     # L2 儿童


def hr(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def show_memories(items: list, title: str = "记忆列表") -> None:
    # 兼容 MemoryRecord 对象与 dict 两种入参
    items = [i.to_dict() if hasattr(i, "to_dict") else i for i in items]
    print(f"\n  [{title}] 共 {len(items)} 条")
    for item in items:
        print(f"    - [{item['memory_type']}] {item['memory_key']}")
        print(f"        值: {item['memory_value']}  "
              f"重要度={item['importance']}  可见角色={item['visible_role']}  "
              f"版本=v{item['version']}")


def show_recall(items: list[dict]) -> None:
    print("\n  [召回结果] 按综合分降序")
    print("    score = 0.6×语义 + 0.2×时效衰减 + 0.2×重要度")
    for item in items:
        print(f"    - {item['memory_value']}")
        print(f"        语义={item['semantic']}  时效={item['time_decay']}  "
              f"重要度={item['importance']}  → 综合分={item['score']}")


def main() -> None:
    settings = get_settings()
    setup_logging("WARNING")  # 演示时只打告警，保持输出清爽

    # 每次演示清库，保证结果可复现
    long_term = LongTermMemory(db_path="data/demo_memory.db")
    long_term.clear()
    service = MemoryService(
        long_term=long_term,
        semantic=SemanticMemory(),
        short_term=ShortTermMemory(),
        settings=settings,
    )

    hr("HomeMind 记忆系统 V0.5")
    print(f"语义模式: {service.semantic.mode}（无 embedding 服务时的降级方案）")
    print(f"召回权重: α={settings.memory.alpha_semantic}  "
          f"β={settings.memory.beta_time_decay}  γ={settings.memory.gamma_importance}  "
          f"λ={settings.memory.decay_lambda}")

    # ---------- 1. 写入 ----------
    hr("1. 记忆写入（规则抽取 + 结构化/语义双写）")
    texts = [
        "我喜欢睡觉时空调26度",
        "我习惯每天晚上十一点关客厅灯",
        "记住我周末聚会时喜欢把灯光调暗",
    ]
    for text in texts:
        print(f"\n  用户说：{text}")
        written = service.write(FATHER, text)
        for item in written:
            print(f"    → 抽取记忆 [{item['memory_type']}] {item['memory_key']} = {item['memory_value']}")

    show_memories(long_term.list_all(), "库中的全部记忆")

    # ---------- 2. 召回 ----------
    hr("2. 语义召回（含排序公式明细）")
    print("\n  查询：睡觉的时候空调开多少")
    results = service.recall("睡觉的时候空调开多少", member_id=FATHER, viewer_role="L4", top_k=3)
    show_recall(results)

    # ---------- 3. 冲突更新 ----------
    hr("3. 冲突更新：同一偏好改口（同 key 触发版本 +1）")
    SLEEP_KEY = "preference:睡觉温度"
    print(f"\n  写入：喜欢睡觉时空调26度   (key={SLEEP_KEY})")
    service.write(FATHER, "喜欢睡觉时空调26度",
                  memory_type="preference", memory_key=SLEEP_KEY, importance=0.6)
    print(f"  改口：喜欢睡觉时空调24度   (key={SLEEP_KEY} 相同 → 判定为同一条)")
    service.write(FATHER, "喜欢睡觉时空调24度",
                  memory_type="preference", memory_key=SLEEP_KEY, importance=0.9)
    print("  再改口：喜欢睡觉时空调25度")
    service.write(FATHER, "喜欢睡觉时空调25度",
                  memory_type="preference", memory_key=SLEEP_KEY, importance=0.85)

    sleep_memories = [m for m in long_term.list_all() if m.memory_key == SLEEP_KEY]
    show_memories(sleep_memories, "温度偏好这条记忆（注意版本号）")
    for item in sleep_memories:
        history = long_term.history(item.memory_key)
        print(f"    [{item.memory_key}] 变更历史：")
        for h in history:
            print(f"        {h['old_value']}  →  {h['new_value']}")

    # ---------- 4. 权限隔离 ----------
    hr("4. 权限隔离：儿童读不到家长隐私记忆")
    print("\n  写入一条家长日程记忆（可见角色 L4，仅管理员可见）：")
    service.write(FATHER, "下周三下午三点开家长会",
                  memory_type="schedule_fact", visible_role="L4")
    print("  写入一条全家可见的公共记忆（可见角色 L1，最低权限也能看）：")
    service.write(FATHER, "家里 WiFi 密码是 home2026",
                  memory_type="family_public", visible_role="L1")

    print("\n  [管理员 L4 视角] 召回「家长会」")
    admin_view = service.recall("家长会", member_id=FATHER, viewer_role="L4", top_k=5)
    for item in admin_view:
        print(f"    - {item['memory_value']}（可见角色 {item['visible_role']}）")

    print("\n  [儿童 L2 视角] 召回「家长会」")
    child_view = service.recall("家长会", member_id=FATHER, viewer_role="L2", top_k=5)
    if child_view:
        for item in child_view:
            print(f"    - {item['memory_value']}")
    else:
        print("    （无结果 —— 敏感记忆已在召回阶段被拦截，不会进入模型上下文）")

    print("\n  [儿童 L2 视角] 召回「WiFi 密码」")
    child_wifi = service.recall("WiFi 密码", member_id=FATHER, viewer_role="L2", top_k=5)
    for item in child_wifi:
        print(f"    - {item['memory_value']}（可见角色 {item['visible_role']}）")

    # ---------- 5. 主动遗忘 ----------
    hr("5. 主动遗忘：用户说「忘掉聚会的事」")
    print("\n  遗忘前，召回「聚会」：")
    before = service.recall("聚会", member_id=FATHER, viewer_role="L4", top_k=3)
    for item in before:
        print(f"    - {item['memory_value']}")

    removed = service.forget_by_keyword(FATHER, "聚会")
    print(f"\n  已硬删除 {removed} 条（结构化库 + 语义库同步清除）")

    print("\n  遗忘后，召回「聚会」：")
    after = service.recall("聚会", member_id=FATHER, viewer_role="L4", top_k=3)
    if after:
        for item in after:
            print(f"    - {item['memory_value']}")
    else:
        print("    （检索不到 —— 硬删除生效，不可恢复）")

    # ---------- 6. 自然遗忘 ----------
    hr("6. 自然遗忘：冷记忆衰减")
    print("\n  模拟：把所有记忆的最后访问时间推到 400 天前，再跑衰减")
    import sqlite3
    import time
    old = time.time() - 400 * 86400
    with sqlite3.connect(long_term.db_path) as conn:
        conn.execute("UPDATE memory_struct SET last_access_at=?, importance=0.3", (old,))
    cleaned = service.decay()
    print(f"  衰减清理了 {cleaned} 条冷记忆（重要度低于阈值即清除）")
    print(f"  剩余记忆：{len(long_term.list_all())} 条")

    hr("演示结束")


if __name__ == "__main__":
    main()
