"""意图识别与任务规划（对齐 FR-INT-001 ~ FR-INT-004）。

双模设计：
- **规则模式（默认）**：零依赖、零延迟、可离线跑，适合开发自测与回归评测。
- **大模型模式**：配置 API Key 后启用，处理规则覆盖不到的复杂表达。

两条模式产出完全相同的 Intent 结构，上层无感知——
这样你既能随时本地跑通，又能一键切到真模型验证效果差异。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..config import Settings, get_settings
from ..core.llm import get_llm_client
from ..core.models import Intent, IntentType
from ..core.trace import get_logger, span
from .time_parser import parse_time_expr

logger = get_logger("agent.intent")

# ---------------- 词表 ----------------
ROOMS = ("客厅", "主卧", "次卧", "卧室", "厨房", "书房", "玄关", "卫生间", "阳台")
DEVICES = ("灯", "空调", "窗帘", "电视", "风扇", "门锁", "地暖", "热水器", "音箱")
DEVICE_ALIAS = {"灯": "灯", "电灯": "灯", "空调": "空调", "冷气": "空调",
                "窗帘": "窗帘", "电视": "电视", "风扇": "风扇", "门锁": "门锁",
                "大门": "门锁", "地暖": "地暖"}

ON_WORDS = ("打开", "开启", "开一下", "开开", "启动", "开", "拉开", "拉起来")
OFF_WORDS = ("关闭", "关掉", "关上", "熄掉", "熄火", "关", "拉上")
SET_WORDS = ("调到", "调至", "设置为", "设成", "调成", "调到")
DELTA_UP = ("调高", "升高", "提高", "升")
DELTA_DOWN = ("调低", "降低", "降")
QUERY_WORDS = ("状态", "开着吗", "关了吗", "多少度", "查询", "看看")

SCHEDULE_WORDS = ("提醒", "记得", "日程", "安排", "闹钟", "提示我", "别忘了",
                  "叫我", "叫醒", "到点")

# 任务创建表达："新加今日任务，晚上八点喂猫""加个任务：明天买猫粮"。
# 刻意**不**把裸的"任务"塞进 SCHEDULE_WORDS——"看看今天的任务""任务进度如何"
# 这类查询句会被误伤成"创建日程"。只匹配"动词 + 任务"的创建句式。
TASK_ADD_PATTERN = re.compile(
    r"(新加|添加|新增|创建|新建|记个|加个|加一下|帮我加)[^，。；！？]{0,10}(任务|待办|事项)")
KNOWLEDGE_WORDS = ("怎么", "如何", "为什么", "什么意思", "说明书", "怎么用",
                   "多久", "几次", "清洗", "保养", "故障", "坏了")
# 注意：不要往这里加"多少"——"空调多少度"是查设备状态，属于设备控制而非知识问答。

# 偏好/习惯表达：这是陈述句不是祈使句，绝不能当成控制指令执行。
# 例："我习惯空调开24度" 里的"开"不是让你现在开空调，是在描述习惯。
# 判错一次，用户家的空调就会莫名其妙自己动——这类误操作比漏执行严重得多。
MEMORY_WORDS = ("我习惯", "我喜欢", "我爱", "我偏好", "我一般", "我通常",
                "我总是", "记住", "以后", "我睡觉时")

# 记忆查询：用户想看 Agent 记住了什么。
# 必须排在偏好判定**之前**——"你现在记录了我的哪些习惯"含"记录了"，
# 如果先查"记住"类词表会撞车。查询短语更具体，优先匹配。
QUERY_MEMORY_WORDS = ("记录了", "看你记录", "我的习惯", "记住了什么",
                      "查看记忆", "我的记忆", "你记得", "看看你记", "列出记忆")

# 多意图切分连接词
SPLIT_WORDS = ("顺便", "然后", "接着", "还有", "另外", "同时")

CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _to_int(raw: str) -> int | None:
    """中文或阿拉伯数字转整数。"""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw in CN_NUM:
        return CN_NUM[raw]
    if raw.startswith("十") and len(raw) == 2:  # 十五 → 15
        return 10 + CN_NUM.get(raw[1], 0)
    return None


def split_multi_intent(text: str) -> list[str]:
    """按连接词切分多意图（对齐 FR-INT-002）。

    只切显式连接词（顺便/然后/还有…），不切逗号——
    "把灯打开，谢谢" 这种礼貌用语不该被拆成两个任务。
    """
    pattern = "|".join(SPLIT_WORDS)
    parts = re.split(f"({pattern})", text)
    segments: list[str] = []
    for part in parts:
        part = part.strip()
        if not part or part in SPLIT_WORDS:
            continue
        segments.append(part)
    return segments or [text]


def _extract_entities(text: str) -> dict[str, Any]:
    """从单句文本抽取实体：房间、设备、动作、数值、时间。"""
    entities: dict[str, Any] = {}

    room = next((r for r in ROOMS if r in text), None)
    if room:
        entities["room"] = room

    device = None
    for alias, canonical in DEVICE_ALIAS.items():
        if alias in text:
            device = canonical
            break
    if device is None:
        device = next((d for d in DEVICES if d in text), None)
    if device:
        entities["device_name"] = device

    # 动作
    if any(w in text for w in SET_WORDS):
        entities["action"] = "set_value"
    elif any(w in text for w in DELTA_UP):
        entities["action"] = "delta_up"
    elif any(w in text for w in DELTA_DOWN):
        entities["action"] = "delta_down"
    elif any(w in text for w in ON_WORDS):
        entities["action"] = "turn_on"
    elif any(w in text for w in OFF_WORDS):
        entities["action"] = "turn_off"
    elif any(w in text for w in QUERY_WORDS):
        entities["action"] = "query"

    # 绝对数值："调到26度" / "设置成 24 度"
    match = re.search(r"(?:调到|调至|设置为|设成|调成)?\s*(\d+|[一两二三四五六七八九十]+)\s*度", text)
    if match:
        value = _to_int(match.group(1))
        if value is not None:
            entities["value"] = float(value)

    # 增量数值："调低两度" / "升高3度"
    if entities.get("action") in ("delta_up", "delta_down"):
        match = re.search(r"(?:调高|调低|升高|降低|提高|升|降)\s*(\d+|[一两二三四五六七八九十]+)\s*度", text)
        if match:
            delta = _to_int(match.group(1))
            if delta is not None:
                entities["delta"] = float(delta)

    return entities


def _classify(text: str) -> tuple[IntentType, float]:
    """规则分类：判断单句意图类型与置信度。"""
    # 记忆查询优先判定（短语更具体，且不能被"记住"类词表抢走）
    if any(w in text for w in QUERY_MEMORY_WORDS):
        return IntentType.MEMORY_QUERY, 0.9

    # 偏好表达优先判定：陈述句不当祈使句用，宁可漏执行也不能误执行
    if any(w in text for w in MEMORY_WORDS):
        return IntentType.MEMORY_WRITE, 0.9

    has_device = any(d in text for d in DEVICES) or any(a in text for a in DEVICE_ALIAS)
    # 查询词也要算作"有动作"，否则"空调多少度"这类纯查询会被判成低置信度而误触发确认
    has_action = any(
        w in text
        for w in ON_WORDS + OFF_WORDS + SET_WORDS + DELTA_UP + DELTA_DOWN + QUERY_WORDS
    )
    # "加任务"是创建日程的另一种说法（对齐需求"给 AI 加任务要进今日任务框"），
    # 也要压过设备判定——同"提醒我九点关灯"一个道理。
    has_schedule = (any(w in text for w in SCHEDULE_WORDS)
                    or bool(TASK_ADD_PATTERN.search(text)))
    has_knowledge = any(w in text for w in KNOWLEDGE_WORDS)

    # 提醒判定必须压过设备判定（顺序改过一次，见下）：
    # "提醒我晚上九点关灯" 里既有"灯"（设备）又有"关"（动作），
    # 原来设备分支排在前面，于是这句话被当成**立刻执行**的关灯指令——
    # 用户想加个提醒，灯却当场灭了，提醒还没建出来。
    # 这是"误执行"，比"漏执行"严重得多：设备动了是看得见的错误，
    # 漏了最多是没反应。所以只要出现提醒触发词，一律先按日程处理。
    # 没有时间的情况（"别忘了关灯"）会走 needs_clarify 反问几点，
    # 宁可多问一句，也不能替用户把设备动了。
    if has_schedule:
        return IntentType.SCHEDULE, 0.85

    # 疑问词必须压过设备动作：问"空调怎么打开"是在求学，不是在下指令。
    # 这个顺序写反了的代价很实在——疑问句被当祈使句，Agent 会真的去动设备。
    if has_device and has_action and not has_knowledge:
        return IntentType.DEVICE_CONTROL, 0.9
    if has_knowledge:
        return IntentType.KNOWLEDGE, 0.8
    if has_device:
        return IntentType.DEVICE_CONTROL, 0.6  # 只有设备没动作，置信度打折
    return IntentType.CHAT, 0.5


def _extract_schedule_fields(text: str) -> dict[str, Any]:
    """抽取日程类意图的字段：内容与时间。"""
    # 先剥掉"加任务"句式，再剥触发词——否则提醒内容里会带着"新加今日任务"这种操作残留
    content = TASK_ADD_PATTERN.sub("", text)
    for word in SCHEDULE_WORDS:
        content = content.replace(word, "")
    content = re.sub(r"[，,。！!？?、：:\s]+", " ", content).strip(" 的我")
    # 去掉时间片段，避免内容里混入时间
    parsed, confidence = parse_time_expr(text)
    fields: dict[str, Any] = {"content": content or "提醒"}
    if parsed is not None:
        fields["scheduled_at"] = parsed.isoformat()
        fields["time_confidence"] = confidence
    else:
        fields["needs_clarify"] = True
    return fields


class IntentParser:
    """意图解析器：规则模式 / 大模型模式。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def parse(self, text: str) -> list[Intent]:
        """解析用户输入，返回意图列表（多意图时返回多个）。"""
        if not self.settings.llm.use_mock:
            parsed = await self._parse_by_llm(text)
            if parsed:
                return parsed
            logger.warning("llm parse failed, fallback to rule-based parsing")

        return self._parse_by_rule(text)

    def _parse_by_rule(self, text: str) -> list[Intent]:
        """规则解析（默认路径）。"""
        with span("intent.parse.rule", logger, text=text):
            intents: list[Intent] = []
            for segment in split_multi_intent(text):
                intent_type, confidence = _classify(segment)
                entities = _extract_entities(segment)
                if intent_type == IntentType.SCHEDULE:
                    # 清洗设备类噪声实体：'提醒我明天开会' 里的 '开' 会被误判成 turn_on
                    for noise in ("action", "device_name", "room", "value", "delta"):
                        entities.pop(noise, None)
                    entities.update(_extract_schedule_fields(segment))
                    # 时间解析不出来要降置信度，触发澄清
                    if entities.get("needs_clarify"):
                        confidence = min(confidence, 0.5)
                intents.append(Intent(type=intent_type, confidence=confidence,
                                      raw_text=segment, entities=entities))
            logger.info("rule parsed %s intent(s)", len(intents),
                        extra={"extra_fields": {"event": "intent.parse",
                                                "types": [i.type.value for i in intents]}})
            return intents

    async def _parse_by_llm(self, text: str) -> list[Intent] | None:
        """大模型解析。失败返回 None，由调用方回退规则模式（依赖可降级）。

        仅在关闭 mock 且配置了 api_key 时启用。
        """
        client = get_llm_client(self.settings)
        if not client.enabled:
            logger.warning("llm not enabled (mock=%s, has_key=%s), fallback to rule parsing",
                           self.settings.llm.use_mock, bool(self.settings.llm.api_key))
            return None

        system = "你是智能家居助手的意图解析器。只输出 JSON，不要任何解释文字。"
        prompt = f"""解析下面的用户输入，输出一个 JSON 数组，每个元素包含：
- type: device_control / schedule / knowledge / memory_query / memory_write / chat
- confidence: 0~1 之间的浮点数
- entities: 对象，可包含这些字段
    device_name: 灯 / 空调 / 窗帘 / 门锁 / 电视 / 风扇
    room: 客厅 / 主卧 / 厨房 等
    action: turn_on / turn_off / set_value / query
    value: 数字（调温度时用）
    content: 提醒内容（type=schedule 时必填）
    scheduled_at: ISO8601 时间字符串（如 2026-10-14T09:00:00）

判断要点：
- 用户想查看 **Agent 记住的习惯与偏好**（"我的习惯""记录了什么""看你记录"）→ type 用 memory_query
- 用户想查看 **已安排的提醒/日程/待办**（"查看我的日程""今天有什么安排""我的提醒"）
  → type 用 schedule，**不要**判成 memory_query。两者都带"我的"，但一个问记忆、一个问日程，
  判错的后果是：用户问日程，助手却答"你习惯早睡"，答非所问。
- 用户在描述习惯或偏好（"我喜欢""我习惯""记住"）→ type 用 memory_write，不要当成控制指令
- 用户要求提醒或安排 → type 用 schedule
- 确实在下指令控制设备 → type 用 device_control
- **两者都像时一律判 schedule**："提醒我晚上九点关灯"虽然有设备（灯）和动作（关），
  但它是把动作安排到未来，不是现在执行。判成 device_control 会让灯当场灭掉，
  属于误执行——宁可判成提醒再反问时间，也不要替用户动设备。

用户输入：{text}"""

        raw_list = await client.chat_json(prompt, system=system)
        if raw_list is None:
            return None
        if not isinstance(raw_list, list):
            raw_list = [raw_list]

        intents: list[Intent] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            try:
                intents.append(Intent(
                    type=IntentType(item.get("type", "chat")),
                    confidence=float(item.get("confidence", 0.5)),
                    raw_text=item.get("raw_text", text),
                    entities=item.get("entities", {}) or {},
                ))
            except (ValueError, TypeError):
                # 模型给了不认识的 type，跳过这条，别拖垮整轮解析
                continue
        return intents or None
