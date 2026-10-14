"""日程 / 提醒 MCP Server（Mock 存储，对齐 FR-TOOL-002、NFR-REL-004）。

设计要点：
- **幂等是硬要求**：创建类工具必须带幂等键，避免模型重试导致重复日程。
- **时间必须回显确认**：时间解析错误是 Agent 最常被投诉的问题（误创建日程），
  所以创建前由上层触发确认，本层只负责存储与校验。
- V0.1 用内存 list，V0.5 换成 MySQL，handler 内部实现替换即可，Schema 不变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.trace import get_logger
from .registry import ToolSpec

logger = get_logger("tools.calendar")

# 内存存储：真实环境替换为 MySQL（表结构见 SRS 5.1 Schedule 实体）
# 注意：这是模块级全局状态，评测跑批时必须先 reset，否则用例之间互相污染。
_SCHEDULES: list[dict[str, Any]] = []
_SEQ = {"value": 0}


def reset_schedules() -> None:
    """清空 Mock 日程并重置自增序号（评测与测试前置调用）。"""
    _SCHEDULES.clear()
    _SEQ["value"] = 0


def _next_id() -> str:
    _SEQ["value"] += 1
    return f"sch_{_SEQ['value']:04d}"


async def _handle_create_reminder(args: dict[str, Any]) -> dict[str, Any]:
    """创建提醒 / 日程。

    参数：
        content: 提醒内容
        scheduled_at: ISO 8601 时间字符串（由意图层解析）
        idempotency_key: 幂等键（注册中心会在写操作前查缓存）
    """
    content: str = args.get("content", "").strip()
    scheduled_at: str | None = args.get("scheduled_at")

    if not content:
        return {"success": False, "message": "提醒内容不能为空"}

    # 时间缺失：拒绝猜测，要求澄清（对齐 FR-INT-004 验收标准）
    if not scheduled_at:
        return {
            "success": False,
            "message": "缺少具体时间，需要向用户确认后再创建",
            "needs_clarify": True,
        }

    # 时间格式校验
    try:
        parsed = datetime.fromisoformat(scheduled_at)
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": f"时间格式无法解析：{scheduled_at}，需向用户确认",
            "needs_clarify": True,
        }

    # 重复检测：同内容同时间视为重复，直接返回已有条目（幂等的业务层保险）
    for item in _SCHEDULES:
        if item["content"] == content and item["scheduled_at"] == scheduled_at:
            logger.info("duplicate schedule detected: %s", content)
            return {"success": True, "message": "该提醒已存在，未重复创建",
                    "schedule": item, "duplicate": True}

    schedule = {
        "schedule_id": _next_id(),
        "content": content,
        "scheduled_at": parsed.isoformat(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _SCHEDULES.append(schedule)
    logger.info("schedule created: %s @ %s", content, schedule["scheduled_at"],
                extra={"extra_fields": {"event": "schedule.create", "content": content}})
    return {"success": True, "message": f"已创建提醒：{content}（{schedule['scheduled_at']}）",
            "schedule": schedule}


async def _handle_list_schedule(args: dict[str, Any]) -> dict[str, Any]:
    """查询日程列表。"""
    keyword: str | None = args.get("keyword")
    items = _SCHEDULES
    if keyword:
        items = [s for s in items if keyword in s["content"]]
    return {"success": True, "schedules": items, "count": len(items)}


def build_calendar_tools() -> list[ToolSpec]:
    """构建日程相关的 MCP 工具列表。"""
    return [
        ToolSpec(
            name="create_reminder",
            description="创建提醒或日程。需要提供明确时间，创建前应向用户确认",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "提醒内容"},
                    "scheduled_at": {
                        "type": "string",
                        "description": "ISO 8601 格式时间，如 2026-10-14T09:00:00",
                    },
                },
                "required": ["content", "scheduled_at"],
            },
            handler=_handle_create_reminder,
            mutating=True,
        ),
        ToolSpec(
            name="list_schedule",
            description="查询已有的提醒和日程",
            parameters={
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "按关键词过滤，可省略"}},
            },
            handler=_handle_list_schedule,
            mutating=False,
        ),
    ]


def reset_schedules() -> None:
    """清空日程（测试用）。"""
    _SCHEDULES.clear()
    _SEQ["value"] = 0


def get_schedules() -> list[dict[str, Any]]:
    """获取日程列表（演示界面用）。"""
    return _SCHEDULES
