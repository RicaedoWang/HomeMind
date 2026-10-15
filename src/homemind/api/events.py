"""家庭事件流（消息中心唯一的数据源）。

为什么要有这个模块：
消息中心原来是**写死的种子文案**——"洗衣机洗涤完成""入户门锁 18:20 开启"，
看着挺像回事，其实跟用户做过什么毫无关系。用户点开一看就穿帮：
"我刚把灯关了，你这消息怎么还在说洗衣机？"

所以改成事件驱动：**每一次真实操作都落一条事件**，消息中心只负责展示这个流。
设备开关、场景执行、任务完成/取消/恢复、AI 创建提醒、登录，全部埋点。

设计取舍：
- 按家庭隔离：事件里绝不能出现别家做的事（与 S2 同一原则）
- 只留最近 N 条：消息中心是"最近发生了什么"，不是审计日志（审计是 S4 的 operation_log）
- 内存存储：演示态够用；真上生产换成 Redis Stream / 表，接口不用改
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.trace import get_logger

logger = get_logger("api.events")

# 每个家庭最多保留的事件条数。消息中心展示"最近发生了什么"，
# 超出就丢最旧的——完整审计留给 S4 的 operation_log，不在这里堆。
_MAX_PER_FAMILY = 50

_EVENTS: dict[str, list[dict[str, Any]]] = {}
_SEQ = {"value": 0}

# 事件类型 → 前端图标键（与 app.js 的 msgIcon 对应）
TYPE_DEVICE = "device"      # 设备操作
TYPE_SCENE = "scene"        # 场景执行
TYPE_TASK = "task"          # 任务完成 / 取消 / 恢复
TYPE_SECURITY = "security"  # 门锁等安全相关
TYPE_SYSTEM = "system"      # 系统/AI 创建提醒


def _next_id() -> str:
    _SEQ["value"] += 1
    return f"evt_{_SEQ['value']:04d}"


def record_event(
    family_id: str,
    kind: str,
    title: str,
    body: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """记录一条真实事件，插到该家庭事件流的最前面。

    kind 决定图标：device / scene / task / security / system
    actor 是操作人名字（取自 token，不是请求体），用于"由谁做的"这类文案。
    """
    event = {
        "id": _next_id(),
        "type": kind,
        "title": title,
        "body": body,
        "actor": actor,
        "time": datetime.now().isoformat(timespec="seconds"),
        "unread": True,
    }
    bucket = _EVENTS.setdefault(family_id, [])
    bucket.insert(0, event)
    if len(bucket) > _MAX_PER_FAMILY:
        del bucket[_MAX_PER_FAMILY:]

    logger.info("event recorded: %s", title,
                extra={"extra_fields": {"event": "event.record", "kind": kind,
                                        "family_id": family_id}})
    return event


def events_of(family_id: str) -> list[dict[str, Any]]:
    """取某个家庭的事件流（新的在前）。别家的事件取不到。"""
    return _EVENTS.get(family_id, [])


def mark_read(family_id: str, event_id: str) -> bool:
    """把单条事件标为已读。"""
    for event in _EVENTS.get(family_id, []):
        if event["id"] == event_id:
            event["unread"] = False
            return True
    return False


def mark_all_read(family_id: str) -> int:
    """全部标为已读，返回本次被标记的条数。"""
    count = 0
    for event in _EVENTS.get(family_id, []):
        if event["unread"]:
            event["unread"] = False
            count += 1
    return count


def unread_count(family_id: str) -> int:
    return sum(1 for e in _EVENTS.get(family_id, []) if e["unread"])


def reset_events() -> None:
    """清空事件流（测试前置调用，避免用例间互相污染）。"""
    _EVENTS.clear()
    _SEQ["value"] = 0
