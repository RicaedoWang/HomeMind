"""时间语义解析（对齐 FR-INT-004）。

为什么单独做规则解析，而不是全交给大模型：
大模型解析"下周三下午三点"这类相对时间时，错误率足以让用户抓狂——
这是 Agent 产品最容易被投诉的点（误创建日程、提醒时间错）。
所以本模块采用 **规则解析为主 + 大模型兜底** 的双保险策略：

1. 规则能解析的，直接用规则结果（确定性、零成本、零延迟）；
2. 规则解析不出来的，交给大模型；
3. 两者都解析不出来，返回 None，由上层**强制向用户确认**，绝不静默猜测。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# 星期中文映射
WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}

# 时段默认小时（用户只说"早上/下午"没说几点时的兜底）
PERIOD_HOUR = {
    "凌晨": 2, "早上": 8, "上午": 10, "中午": 12,
    "下午": 15, "傍晚": 18, "晚上": 20, "夜里": 22, "半夜": 23,
}


def _match_relative_minutes(text: str) -> int | None:
    """匹配相对时间：'半小时后' / '20分钟后' / '2小时后'。"""
    if "半小时" in text:
        return 30
    match = re.search(r"(\d+)\s*分钟[之后后]", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*(?:个小?时|小时)[之后后]", text)
    if match:
        return int(match.group(1)) * 60
    return None


def _match_day_offset(text: str) -> int | None:
    """匹配日期偏移：今天 0 / 明天 1 / 后天 2 / 大后天 3。"""
    if "大后天" in text:
        return 3
    if "后天" in text:
        return 2
    if "明天" in text or "明日" in text or "明早" in text:
        return 1
    if "今天" in text or "今日" in text or "今晚" in text:
        return 0
    return None


def _match_weekday(text: str) -> int | None:
    """匹配星期：'下周三' / '周一' / '星期三'。"""
    match = re.search(r"(?:下?周|星期|礼拜)\s*([一二三四五六日天1-7])", text)
    if match:
        return WEEKDAY_MAP.get(match.group(1))
    return None


def _match_clock(text: str) -> tuple[int, int] | None:
    """匹配具体钟点：'九点' / '15:30' / '下午三点半' / '3点20'。

    返回 (hour, minute)，识别不出分钟则 minute=0。
    """
    # 24 小时制 HH:MM
    match = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    # 中文数字或阿拉伯数字 + 点（可带半/分）
    cn_num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    match = re.search(r"(\d{1,2}|[一二三四五六七八九十]+)\s*点(?:钟)?\s*(\d{1,2}|半)?", text)
    if match:
        raw_hour = match.group(1)
        hour = int(raw_hour) if raw_hour.isdigit() else cn_num.get(raw_hour, 0)
        # 下午/晚上 且小时 <= 12 → 转 24 小时制
        if hour <= 12 and any(p in text for p in ("下午", "晚上", "傍晚", "夜里", "半夜")):
            hour += 12
        minute_raw = match.group(2)
        if minute_raw == "半":
            minute = 30
        elif minute_raw and minute_raw.isdigit():
            minute = int(minute_raw)
        else:
            minute = 0
        if 0 <= hour <= 23:
            return hour, minute
    return None


def parse_time_expr(text: str, base: datetime | None = None) -> tuple[datetime | None, float]:
    """解析时间表达式。

    返回：(解析结果 datetime 或 None, 置信度 0~1)
    解析失败返回 (None, 0.0)，上层必须向用户确认，禁止猜测。
    """
    now = base or datetime.now()

    # 1. 相对时间：优先级最高，"20分钟后" 是明确的
    rel_minutes = _match_relative_minutes(text)
    if rel_minutes is not None:
        return now + timedelta(minutes=rel_minutes), 0.95

    # 2. 确定日期
    weekday = _match_weekday(text)
    if weekday is not None:
        days_ahead = weekday - now.weekday()
        if days_ahead <= 0:  # 本周已过，顺延到下周
            days_ahead += 7
        if "下周" in text and days_ahead < 7:
            days_ahead += 7
        target_date = (now + timedelta(days=days_ahead)).date()
        day_confidence = 0.9
    else:
        offset = _match_day_offset(text)
        if offset is None:
            # 没有任何日期线索，但可能有钟点（如"三点提醒我"→ 默认今天）
            if _match_clock(text) is None:
                return None, 0.0
            target_date = now.date()
            day_confidence = 0.6
        else:
            target_date = (now + timedelta(days=offset)).date()
            day_confidence = 0.95

    # 3. 确定时刻
    clock = _match_clock(text)
    if clock is not None:
        hour, minute = clock
        time_confidence = 0.9
    else:
        # 只有时段没有钟点，用时段的默认小时
        hour = None
        for period, default_hour in PERIOD_HOUR.items():
            if period in text:
                hour = default_hour
                break
        if hour is None:
            # 没有时刻信息，默认 9:00，但置信度降到 0.5（上层会触发确认）
            hour = 9
            time_confidence = 0.5
        else:
            time_confidence = 0.7
        minute = 0

    result = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return result, round(min(day_confidence, time_confidence), 2)


def format_human(dt: datetime) -> str:
    """转成人类可读时间，用于回显确认。"""
    return dt.strftime("%Y-%m-%d %H:%M")
