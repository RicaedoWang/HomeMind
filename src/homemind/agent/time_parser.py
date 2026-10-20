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


# 时间词表：只收录"明确在说时间"的词，用于从提醒内容里剔除。
# 刻意不收"一次""点菜"里的"点"这类歧义片段——剥错字比不剥更让人困惑。
_TIME_TOKEN_RE = re.compile(
    r"(?:"
    r"(?:今天|今日|明天|明日|后天|大后天|昨天|前天)"
    r"|(?:下周[一二三四五六日天]|这周[一二三四五六日天]|周[一二三四五六日天]"
    r"|下星期[一二三四五六日天]|星期[一二三四五六日天])"
    r"|(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)"
    r"|(?:[零一二两三四五六七八九十百]{1,3}点半|[零一二两三四五六七八九十百]{1,3}点(?:\d{1,2}分)?)"
    r"|(?:\d{1,2}点(?:\d{1,2}分)?)"
    r"|(?:半小时[之以]?[后内]|\d{1,3}\s*(?:分钟|个?小时)[之以]?[后内])"
    r")"
)


def strip_time_tokens(text: str) -> str:
    """把时间片段从提醒内容里剥掉。

    为什么需要：`parse_time_expr` 只返回解析出的 datetime，不告诉你**它吃掉了哪几个字**。
    于是"晚上七点去遛狗"会被存成 content="晚上七点去遛狗" 且 scheduled_at=19:00，
    回显时就成了「即将创建：晚上七点去遛狗（2026-10-20T19:00:00）」——同一句话里时间出现两遍，
    看着就像两个不同的时间。
    """
    if not text:
        return ""
    cleaned = _TIME_TOKEN_RE.sub(" ", text)
    # 剥完会留下多余空格和句中残留的连接词，收一下再交给上层
    cleaned = re.sub(r"[\s，,。！!？?、：:；;]+", " ", cleaned).strip(" 的了我吧呀啊")
    return cleaned


def describe_now(now: datetime | None = None) -> str:
    """把"现在"说成人话，供 prompt 注入。

    为什么必须有这个函数：**大模型没有系统时钟**。
    不告诉它今天几号，它只能拿训练语料里的日期瞎猜。
    实测踩过一次很蠢的坑：意图解析的 prompt 里把示例写死成
    "如 2026-10-14T09:00:00"，用户说"创建今日任务"，模型嘴上说"今日"、
    手上照着示例抄了个 10-14 —— 任务刚建出来就"逾期 5 天 18 小时"，
    而规则解析路径（用 datetime.now()）完全正常，所以评测 52 条全绿也没抓到。

    这个函数同时给意图解析和应答生成两处用，保证两条 prompt 的时间基准一致。
    """
    now = now or datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    return f"现在是 {now.year}年{now.month}月{now.day}日 星期{weekday} {now:%H:%M}"


# 「现在几点」这类纯问时间的说法。刻意收得很窄：只认明确问时间/日期的短语，
# 不认"今天天气怎么样""今天任务"——那些该走别的分支。
# 规则模式没有 LLM 兜底，认错一个词用户就得到一句驴唇不对马嘴的模板话。
_TIME_QUESTION_PATTERNS = (
    "现在几点", "现在几点了", "现在什么时间", "现在是什么时间", "现在什么时候",
    "现在是什么时候", "当前时间", "现在的时间", "几点了", "现在日期",
    "今天几号", "今天几号了", "今天是什么日子", "今天星期几", "今天周几",
    "星期几了", "周几了", "什么日期", "今天的日期", "几月几号",
)


def is_time_question(text: str) -> bool:
    """判断用户是不是在问"现在几点 / 今天几号"。

    规则模式（HM_USE_MOCK_LLM=true）下模型不参与应答，全靠模板拼话，
    所以必须在模板里准备一句真实时间，否则用户问时间会得到
    "我在听。可以说「我喜欢…」"这种答非所问的鬼话。
    """
    t = (text or "").strip()
    if not t:
        return False
    return any(p in t for p in _TIME_QUESTION_PATTERNS)
