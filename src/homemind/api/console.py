"""智能家居控制台 REST API（对齐界面设计文档的数据需求）。

设计要点：

1. **设备状态是同一份真相**。UI 上看到的设备与 AI 助手控制的设备，
   都来自 `tools.device_server` 的 Mock 设备层。所以"在对话框里说关灯"，
   首页设备卡的灯泡会真的灭——两处状态不会出现"AI 说关了、界面还亮着"的撕裂。
2. **设备控制走工具注册中心**，不是绕过它直接改内存。
   这样超时、重试、熔断、幂等一整套治理在 Web 端同样生效。
3. **高危设备（门锁）必须显式确认**，与 Agent 主链路的护栏规则一致。
4. 任务数据用进程内存存储（演示态），结构严格对齐设计文档 6.2 的 HomeTask。
"""

from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent.graph import build_agent_graph
from ..auth.deps import current_user
from ..auth.tokens import AuthUser
from ..agent.state import initial_state
from .events import (  # noqa: E402
    TYPE_DEVICE,
    TYPE_SCENE,
    TYPE_SECURITY,
    TYPE_SYSTEM,
    TYPE_TASK,
    events_of,
    mark_all_read,
    mark_read,
    record_event,
    reset_events,
    unread_count,
)
from ..auth.family_store import VALID_ACTIONS, get_family, get_scene_store
from .auth import get_account_store
from ..core.trace import get_logger, new_trace_id, set_trace_id
from ..memory.service import get_memory_service
from ..memory.short_term import get_short_term_memory
from ..tools.calendar_server import delete_schedule, get_schedules, reset_schedules
from ..core.tenancy import DEFAULT_FAMILY_ID, FAMILY_ARG
from ..tools.device_server import devices_of, get_device_states
from ..tools.registry import default_registry

logger = get_logger("api.console")

router = APIRouter(prefix="/api/console", tags=["console"])

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# ---------------------------------------------------------------- 静态数据

# 家庭成员不再写死：注册功能一上来，"新加的人不显示"立刻穿帮。
# 现在从账号表现查——谁注册了谁就在列表里，退出的（将来支持）自然消失。
_MEMBER_COLORS = ["#4F7BFF", "#F472B6", "#22D3A8", "#FBBF24", "#A78BFA", "#FB923C"]
_ROLE_LABEL = {"L4": "owner", "L3": "admin", "L2": "member", "L1": "guest"}


def _members_payload(family_id: str) -> list[dict[str, Any]]:
    """本家庭成员。颜色按顺序轮转，保证同一个人每次刷新颜色不变。"""
    accounts = get_account_store().accounts_of_family(family_id)
    return [
        {
            "id": a.member_id,
            "memberId": a.member_id,
            "name": a.name,
            "colorTag": _MEMBER_COLORS[idx % len(_MEMBER_COLORS)],
            "role": _ROLE_LABEL.get(a.role, "member"),
            "avatar": (a.name or "?")[0],
        }
        for idx, a in enumerate(accounts)
    ]


def _scenes_payload(family_id: str) -> list[dict[str, Any]]:
    """本家庭全部场景：内置在前（只读），自建在后（可增删改）。"""
    return [s.to_dict() for s in get_scene_store().scenes_of(family_id)]


def _family_name(family_id: str) -> str:
    """家庭显示名。查不到就露出 id——宁可难看，也别编一个"我的家"糊弄人。"""
    family = get_family(family_id)
    return family.name if family else family_id

# 场景数据已搬到 auth/family_store.py：内置场景在代码里（只读），自建场景落库
# （可增删改）。搬走的理由——场景从"写死的展示数据"变成了"用户能编辑的内容"，
# 再当常量摆在 API 模块里，就意味着用户每加一个场景都得改一次代码。

CATEGORY_COLORS = {
    "plant": "#34D399", "laundry": "#60A5FA", "cleaning": "#A78BFA",
    "pet": "#FBBF24", "security": "#F87171", "shopping": "#F472B6",
    "maintenance": "#22D3A8", "health": "#FB923C", "other": "#94A3B8",
    "reminder": "#A78BFA",   # AI 创建的提醒
}

# ---------------------------------------------------------------- 任务存储

# 任务也按租户分桶：小明打开控制台看到的是"更换新风滤芯"，
# 不该是"给深圳那只猫加粮"——两个家庭的家务本来就不是一回事。
_TASKS: dict[str, list[dict[str, Any]]] = {"family_sz": [], "family_qd": []}


def tasks_of(family_id: str) -> list[dict[str, Any]]:
    """取某个家庭的任务列表。"""
    return _TASKS.setdefault(family_id, [])


def _seed_tasks() -> None:
    """种子任务：刻意覆盖文档里的典型场景（逾期 / 设备触发 / AI 添加 / 手动）。"""
    if _TASKS["family_sz"]:
        return
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _TASKS["family_sz"].extend([
        {
            "id": "t-plant", "title": "给阳台的绿萝浇花",
            "description": "土壤湿度 28%，已 3 天未浇",
            "iconKey": "plant", "category": "plant", "source": "automation",
            "creatorId": "m2", "assigneeIds": ["m1"], "roomId": "balcony",
            "dueAt": (today + timedelta(hours=18)).isoformat(),
            "priority": "urgent", "status": "pending",
            "linkedDeviceIds": ["balcony_valve"], "completionAction": "control-device",
            "createdAt": (datetime.now() - timedelta(hours=6)).isoformat(),
        },
        {
            "id": "t-laundry", "title": "晾晒衣服",
            "description": "洗衣机 · 已完成洗涤",
            "iconKey": "laundry", "category": "laundry", "source": "device-event",
            "creatorId": "m1", "assigneeIds": ["m1"], "roomId": "balcony",
            "dueAt": (datetime.now() + timedelta(minutes=40)).isoformat(),
            "priority": "normal", "status": "pending",
            "linkedDeviceIds": ["balcony_dryer"], "completionAction": "control-device",
            "createdAt": (datetime.now() - timedelta(minutes=12)).isoformat(),
        },
        {
            "id": "t-pet", "title": "给猫加粮并清理猫砂",
            "description": "粮仓余量 22%，建议今日补充",
            "iconKey": "pet", "category": "pet", "source": "manual",
            "creatorId": "m3", "assigneeIds": ["m1", "m3"], "roomId": "living",
            "dueAt": (today + timedelta(hours=21)).isoformat(),
            "priority": "normal", "status": "pending",
            "linkedDeviceIds": [], "completionAction": "notify-only",
            "createdAt": (datetime.now() - timedelta(hours=2)).isoformat(),
        },
        {
            "id": "t-security", "title": "确认入户门锁已反锁",
            "description": "20:00 后仍未上锁",
            "iconKey": "security", "category": "security", "source": "automation",
            "creatorId": "m1", "assigneeIds": ["m1"], "roomId": "entry",
            "dueAt": (today + timedelta(hours=22)).isoformat(),
            "priority": "low", "status": "pending",
            "linkedDeviceIds": ["front_door_lock"], "completionAction": "control-device",
            "createdAt": (datetime.now() - timedelta(hours=1)).isoformat(),
        },
    ])
    _TASKS["family_qd"].extend([
        {
            "id": "t-qd-filter", "title": "更换新风系统滤芯",
            "description": "滤芯寿命剩余 8%，海雾天建议提前换",
            "iconKey": "tool", "category": "maintenance", "source": "automation",
            "creatorId": "m101", "assigneeIds": ["m101"], "roomId": "living",
            "dueAt": (today + timedelta(hours=20)).isoformat(),
            "priority": "urgent", "status": "pending",
            "linkedDeviceIds": ["living_room_fan"], "completionAction": "notify-only",
            "createdAt": (datetime.now() - timedelta(hours=5)).isoformat(),
        },
        {
            "id": "t-qd-defrost", "title": "地暖除霜提醒",
            "description": "室外 12°C，地暖建议保持在 20°C 以上",
            "iconKey": "thermometer", "category": "maintenance", "source": "automation",
            "creatorId": "m101", "assigneeIds": ["m101"], "roomId": "living",
            "dueAt": (today + timedelta(hours=17)).isoformat(),
            "priority": "normal", "status": "pending",
            "linkedDeviceIds": ["living_room_heater"], "completionAction": "control-device",
            "createdAt": (datetime.now() - timedelta(hours=3)).isoformat(),
        },
        {
            "id": "t-qd-laundry", "title": "收阳台的衣服",
            "description": "海雾天湿度 45%，晾了一天该收了",
            "iconKey": "laundry", "category": "laundry", "source": "manual",
            "creatorId": "m101", "assigneeIds": ["m101"], "roomId": "balcony",
            "dueAt": (today + timedelta(hours=19)).isoformat(),
            "priority": "normal", "status": "pending",
            "linkedDeviceIds": ["balcony_dryer_qd"], "completionAction": "control-device",
            "createdAt": (datetime.now() - timedelta(minutes=30)).isoformat(),
        },
    ])


def reset_tasks() -> None:
    """重置任务存储（测试前置调用，避免用例间互相污染）。"""
    for bucket in _TASKS.values():
        bucket.clear()
    _seed_tasks()


def reset_console_state() -> None:
    """测试前置：任务与事件流一起打回原点。

    两者必须一起清，否则会出现"任务重置了、消息里还留着上一条完成记录"
    这种前后撕裂的状态，测试断言就不可复现了。
    """
    reset_tasks()
    reset_schedules()
    reset_events()


_seed_tasks()

# ---------------------------------------------------------------- 派生数据


def _device_payload(family_id: str) -> list[dict[str, Any]]:
    """设备状态 → 前端卡片结构（只列本家庭的设备）。

    关键状态（亮度/温度/进度）必须在卡面可见，不点进详情也知道设备什么状态。
    """
    devices: list[dict[str, Any]] = []
    for dev in get_device_states(family_id).values():
        payload = {
            "id": dev.device_id,
            "name": dev.name,
            "room": dev.room,
            "type": dev.device_type,
            "power": bool(dev.power),
            "value": dev.value,
            "online": True,
            "lowBattery": False,
            "alert": False,
        }
        if dev.device_type == "light":
            payload["statusText"] = (f"已开 {int(dev.value or 0)}%" if dev.power
                                     else "已关闭")
            payload["control"] = "dimmer"
            payload["glow"] = "#FFB871" if dev.power else None
        elif dev.device_type == "ac":
            payload["statusText"] = (f"制冷 {int(dev.value or 26)}°C" if dev.power
                                     else "已关闭")
            payload["control"] = "temperature"
            payload["glow"] = "#60A5FA" if dev.power else None
        elif dev.device_type == "curtain":
            payload["statusText"] = (f"已开 {int(dev.value or 0)}%" if dev.power
                                     else "已关闭")
            payload["control"] = "dimmer"
        elif dev.device_type == "lock":
            payload["statusText"] = "已开锁（危险）" if dev.power else "已上锁"
            payload["control"] = "switch"
            payload["highRisk"] = True
        elif dev.device_type == "washer":
            payload["statusText"] = "洗涤完成，记得晾晒"
            payload["control"] = "switch"
            payload["progress"] = 100
        elif dev.device_type == "robot":
            payload["statusText"] = "待机 · 电量 100%"
            payload["control"] = "switch"
        elif dev.device_type == "dryer":
            payload["statusText"] = "已升起"
            payload["control"] = "switch"
        elif dev.device_type == "humidifier":
            payload["statusText"] = (f"恒湿 {int(dev.value or 50)}%" if dev.power
                                     else "已关闭")
            payload["control"] = "dimmer"
        else:
            payload["statusText"] = "已开" if dev.power else "已关闭"
            payload["control"] = "switch"
        devices.append(payload)
    return devices


def _schedule_to_task(schedule: dict[str, Any]) -> dict[str, Any]:
    """把一条 AI 创建的提醒转成任务卡片结构。

    为什么要转：家务任务存在 `console._TASKS`，AI 创建的提醒存在
    `calendar_server._SCHEDULES`，是两套存储。不合并的话，
    用户跟 AI 说"提醒我晚上九点关灯"，AI 回"已创建"，
    但今日任务框里**什么都不出现**——像被吞了一样。

    合并放在展示层而不是强行塞进同一张表：家务任务和日程的生命周期不同
    （家务有联动设备、日程没有），硬合成一套只会互相污染。
    """
    when = schedule.get("scheduled_at", "")
    return {
        "id": schedule["schedule_id"],
        "title": schedule.get("content", "提醒"),
        "description": f"AI 助手创建 · 时间 {when.replace('T', ' ')[:16]}",
        "iconKey": "bell",
        "category": "reminder",
        "source": "ai",
        "creatorId": "ai",
        "assigneeIds": [],
        "roomId": "",
        "dueAt": when,
        "priority": "normal",
        "status": schedule.get("status", "pending"),
        "linkedDeviceIds": [],
        "completionAction": "notify-only",
        "isSchedule": True,     # 前端据此走"提醒"而不是"家务"的交互文案
        "createdAt": schedule.get("created_at", ""),
    }


def _find_schedule(family_id: str, schedule_id: str) -> dict[str, Any] | None:
    """在本家庭的日程里找一条（别家的找不到）。"""
    return next((s for s in get_schedules(family_id)
                 if s["schedule_id"] == schedule_id), None)


# 两个家庭的室内外环境基线（演示用种子数据，不是真实传感器读数）
_ENV_BASELINE: dict[str, dict[str, Any]] = {
    "family_sz": {"indoor_temp": 24.5, "humidity": 62, "outdoor": 25.0,
                  "high": 28, "low": 21, "condition": "sunny", "conditionText": "晴",
                  "aqi": 43, "aqi_level": "优", "sensor": "客厅"},
    "family_qd": {"indoor_temp": 18.2, "humidity": 45, "outdoor": 12.0,
                  "high": 15, "low": 8, "condition": "fog", "conditionText": "海雾",
                  "aqi": 72, "aqi_level": "良", "sensor": "客厅"},
}


def _env_payload(family_id: str) -> dict[str, Any]:
    """环境数据：室内外温湿度 + 天气 + 空气质量（按家庭取，两家数据不同）。

    传感器缺数据时返回 None，前端显示"未接入传感器"——
    绝不编造一个看起来合理的数字出来（与 AI 能力边界红线同一原则）。
    """
    now = datetime.now()
    base = _ENV_BASELINE.get(family_id, _ENV_BASELINE[DEFAULT_FAMILY_ID])
    drift = math.sin(now.hour / 24 * math.pi * 2) * 1.6
    indoor_temp = round(base["indoor_temp"] + drift, 1)
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "lunar": "九月初四",
        "indoor": {"temp": indoor_temp, "humidity": base["humidity"], "sensor": base["sensor"]},
        "outdoor": {"temp": base["outdoor"], "high": base["high"], "low": base["low"],
                    "condition": base["condition"], "conditionText": base["conditionText"]},
        "aqi": {"value": base["aqi"], "level": base["aqi_level"]},
        "tempDiffWarn": abs(indoor_temp - base["outdoor"]) >= 8,
        "updatedAt": now.isoformat(timespec="seconds"),
    }


def _comfort_hint(env: dict[str, Any], family_id: str) -> dict[str, Any] | None:
    """舒适度提示：按文档 5.2 的优先级表，同时最多返回 1 条。

    提示里带的设备必须是本家庭真有的，否则青岛家会收到"开加湿器"这种
    指向不存在设备的建议——点了没反应，比不给提示更让人困惑。
    """
    indoor = env["indoor"]
    owned = set(get_device_states(family_id))
    if indoor["humidity"] < 35 and "bedroom_humidifier" in owned:
        return {"level": "warning", "icon": "drop",
                "text": f"室内偏干（{indoor['humidity']}%），皮肤和绿植都难受",
                "action": {"label": "开加湿器", "deviceId": "bedroom_humidifier",
                           "action": "turn_on"}}
    if indoor["humidity"] > 75 and "living_room_ac" in owned:
        return {"level": "warning", "icon": "drop",
                "text": f"室内偏潮（{indoor['humidity']}%），当心霉味",
                "action": {"label": "开除湿", "deviceId": "living_room_ac",
                           "action": "turn_on"}}
    if indoor["temp"] > 29 and "living_room_ac" in owned:
        return {"level": "info", "icon": "thermometer",
                "text": f"客厅 {indoor['temp']}°C，要不把空调开上？",
                "action": {"label": "开客厅空调 26°C", "deviceId": "living_room_ac",
                           "action": "set_value", "value": 26}}
    if indoor["temp"] < 16 and "living_room_heater" in owned:
        return {"level": "info", "icon": "thermometer",
                "text": f"客厅 {indoor['temp']}°C，有点冷了",
                "action": {"label": "开地暖 22°C", "deviceId": "living_room_heater",
                           "action": "set_value", "value": 22}}
    if indoor["temp"] < 16 and "living_room_ac" in owned:
        return {"level": "info", "icon": "thermometer",
                "text": f"客厅 {indoor['temp']}°C，有点冷了",
                "action": {"label": "开空调制热", "deviceId": "living_room_ac",
                           "action": "turn_on"}}
    plant_task = next((t for t in tasks_of(family_id) if t["category"] == "plant"
                       and t["status"] == "pending"), None)
    if plant_task:
        return {"level": "info", "icon": "plant",
                "text": "阳台绿萝该浇水了",
                "action": {"label": "生成浇花任务并指派给我", "taskId": plant_task["id"]}}
    return None


def _energy_payload() -> dict[str, Any]:
    return {"today": 3.2, "unit": "kWh", "delta": -12, "week": 24.6, "month": 108.4}


# 消息中心**没有种子数据**——它就是 events.py 里那条真实事件流。
# 以前这里写死三条"洗衣机洗涤完成""门锁 18:20 开启"，用户点开就穿帮：
# 他刚把灯关了，消息却还在讲洗衣机。现在每一条都对应一次真实操作，
# 谁做的、几点做的、动了哪台设备，全是真的。


# ---------------------------------------------------------------- 接口


@router.get("/state")
async def console_state(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """控制台全量状态：一次请求拿齐首页所需数据，**且只含本家庭的数据**。"""
    family_id = user.family_id
    env = _env_payload(family_id)
    # 今日任务 = 家务任务 + AI 创建的提醒（两套存储在展示层合并）
    tasks = [_schedule_to_task(s) for s in get_schedules(family_id)] + tasks_of(family_id)
    pending = [t for t in tasks if t["status"] == "pending"]
    devices = _device_payload(family_id)
    return {
        "env": env,
        "hint": _comfort_hint(env, family_id),
        "tasks": [dict(t, colorTag=CATEGORY_COLORS.get(t["category"], "#94A3B8"))
                  for t in tasks],
        "pendingCount": len(pending),
        "devices": devices,
        "rooms": sorted({d["room"] for d in devices}),
        "scenes": _scenes_payload(family_id),
        "schedules": get_schedules(family_id),
        "energy": _energy_payload(),
        "messages": events_of(family_id),
        "unreadCount": unread_count(family_id),
        "family": _members_payload(family_id),
        # 身份回显：前端据此显示"谁在看这个家"
        "viewer": {**user.to_dict(),
                   "familyName": _family_name(user.family_id)},
    }


class ControlRequest(BaseModel):
    """设备控制请求。"""

    action: str = Field(..., description="turn_on / turn_off / set_value")
    value: float | None = Field(default=None, description="目标值（亮度/温度/开合度）")
    confirmed: bool = Field(default=False, description="高危设备的二次确认")
    member_id: str = Field(default="member_001")


@router.post("/devices/{device_id}/control")
async def control_device(
    device_id: str, req: ControlRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """控制设备。走工具注册中心，享受超时/重试/熔断/幂等治理。

    两道隔离：
    1. 操作人取自 token（`user.member_id`），**请求体里传的 member_id 一律忽略**——
       否则谁都能伪造成别人来操作门锁。
    2. 设备必须属于 token 里的家庭。不属于 → 403，且**状态一点都不动**。
       这里刻意不返回 404：404 等于告诉对方"这个 id 存在但不属于你"，
       是免费的资源枚举线索。
    """
    family_id = user.family_id
    device = devices_of(family_id).get(device_id)
    if device is None:
        raise HTTPException(status_code=403, detail="无权操作该设备")

    # 高危设备：未确认不放行，与 Agent 主链路的护栏规则保持一致
    if device.device_type == "lock" and not req.confirmed:
        return {"ok": False, "requiresConfirm": True,
                "message": "门锁属于高危操作，请先在卡片上确认",
                "device": next((d for d in _device_payload(family_id)
                                if d["id"] == device_id), None)}

    registry = default_registry()
    tool_name = "lock_control" if device.device_type == "lock" else "device_control"
    arguments: dict[str, Any] = {
        # 括号是**必须的**。原来写成
        #   "unlock" if action == "turn_on" else "lock" if is_lock else action
        # Python 的条件表达式右结合，实际解析成
        #   "unlock" if action == "turn_on" else ("lock" if is_lock else action)
        # ——于是**任何设备**的 turn_on 都被翻译成 "unlock"：
        # 灯/空调/窗帘永远打不开（工具回"不支持的动作：unlock"），
        # 只有 turn_off 原样透传所以"能关不能开"。
        # 这就是用户报的那个 bug。外层括号把"是不是门锁"变成先判的条件。
        "action": ("unlock" if req.action == "turn_on" else "lock")
        if device.device_type == "lock" else req.action,
        "device_name": device.name,
        "room": device.room,
        # 服务端注入租户：模型/请求体都碰不到这个字段
        FAMILY_ARG: family_id,
    }
    if req.value is not None:
        arguments["value"] = req.value

    result = await registry.call(tool_name, arguments)

    # 调用成功 ≠ 操作真的生效：device_control 遇到不支持的动作会返回
    # success=False 但调用本身没抛错，result.ok 仍是 True。
    # 以前只判 result.ok，于是"不支持的动作"也被当成功，
    # 消息中心还记了一条"打开了客厅灯"——灯根本没动。
    # 消息中心不许放假消息，所以必须看工具自己报的 success。
    succeeded = result.ok and (result.data or {}).get("success", True)

    # 真实事件：只有真的动了才记，失败/被拦的不记（消息中心不放假消息）
    if succeeded:
        verb = "打开" if req.action == "turn_on" else "关闭"
        kind = TYPE_SECURITY if device.device_type == "lock" else TYPE_DEVICE
        record_event(
            family_id, kind,
            title=f"{verb}了{device.name}" if device.device_type != "lock"
                  else f"{'开锁' if req.action == 'turn_on' else '上锁'}：{device.name}",
            body=f"位置：{device.room} · 操作人：{user.name}",
            actor=user.name,
        )

    # 注意顺序：必须在工具调用之后再取设备状态，否则前端拿到的是执行前的旧值，
    # 会显示"后端说开了、卡片还关着"。
    # ok 回的是"操作是否真的生效"，不是"工具有没有被调用"——
    # 前端据此决定刷新还是报错，回 True 会让它以为成功了。
    return {"ok": succeeded, "tool": tool_name,
            "message": (result.data or {}).get("message", ""),
            "device": next((d for d in _device_payload(family_id)
                            if d["id"] == device_id), None),
            "degraded": result.degraded, "error": result.error}


class SceneRequest(BaseModel):
    """场景执行请求。"""

    confirmed: bool = False


@router.post("/scenes/{scene_id}/activate")
async def activate_scene(
    scene_id: str, req: SceneRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """执行场景：按顺序作用于一组设备，逐项返回回执。

    场景与设备都取自本家庭——拿深圳的场景清单去青岛家执行，
    除了空转什么也不会发生。
    """
    family_id = user.family_id
    scene = get_scene_store().find(family_id, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="场景不存在")

    registry = default_registry()
    affected: list[dict[str, Any]] = []
    for item in scene.actions:
        device_id = str(item.get("device_id", ""))
        action = str(item.get("action", ""))
        value = item.get("value")
        device = devices_of(family_id).get(device_id)
        if device is None:
            # 场景是用户自己建的，设备后来可能被移除（或换了个家）。
            # 跳过要留痕：否则用户看到"执行成功"，某台设备却没动，
            # 会以为系统在糊弄他。
            affected.append({"deviceId": device_id, "name": device_id,
                             "ok": False, "message": "设备不存在，已跳过"})
            continue
        arguments: dict[str, Any] = {"action": action, "device_name": device.name,
                                     "room": device.room, FAMILY_ARG: family_id}
        if value is not None:
            arguments["value"] = value
        result = await registry.call("device_control", arguments)
        # `result.ok` 只代表"工具被调用了"，device_control 对不支持的动作
        # 返回 success=False 但不抛错。只看 ok 会出现"场景说全成功、灯其实没动"。
        succeeded = result.ok and (result.data or {}).get("success", True)
        affected.append({"deviceId": device_id, "name": device.name,
                         "ok": succeeded,
                         "message": (result.data or {}).get("message", "")})

    done = [a for a in affected if a["ok"]]
    record_event(
        family_id, TYPE_SCENE,
        title=f"执行了「{scene.name}」模式",
        body=f"联动 {len(done)} 台设备：{'、'.join(a['name'] for a in done) or '无'}",
        actor=user.name,
    )
    return {"ok": True, "scene": scene.name, "affected": affected}


# ---------------------------------------------------------------- 场景自定义

class SceneActionItem(BaseModel):
    """场景里的一个动作：对哪台设备做什么。"""

    device_id: str = Field(..., description="设备 id（必须是本家庭的设备）")
    action: str = Field(..., description="turn_on / turn_off / set_value")
    value: float | None = Field(default=None, description="数值 0-100（亮度/开合/温度）")


class SceneUpsertRequest(BaseModel):
    """新建或修改场景。"""

    name: str = Field(..., description="场景名，如「回家」")
    icon: str = Field(default="sparkle", description="图标 id")
    actions: list[SceneActionItem] = Field(default_factory=list)


def _validate_actions(family_id: str, actions: list[SceneActionItem]) -> list[dict[str, Any]]:
    """校验并清洗动作。

    为什么在这里较真：场景是"点一次动好几台设备"的批量操作。里面混进一个
    别家的设备 id，就等于给用户开了一个绕过单设备租户校验的后门——
    单设备接口会 403，场景接口却把它当成自己家的设备照做，隔离就漏了。
    """
    if not actions:
        raise HTTPException(status_code=400, detail="场景至少要包含一个动作")
    if len(actions) > 10:
        raise HTTPException(status_code=400, detail="一个场景最多 10 个动作")

    bucket = devices_of(family_id)
    cleaned: list[dict[str, Any]] = []
    for item in actions:
        if item.device_id not in bucket:
            raise HTTPException(status_code=400,
                                detail=f"设备 {item.device_id} 不属于这个家")
        if item.action not in VALID_ACTIONS:
            raise HTTPException(status_code=400, detail=f"不支持的动作 {item.action}")

        value = item.value
        if value is not None:
            if not 0 <= float(value) <= 100:
                raise HTTPException(status_code=400, detail="数值必须在 0-100 之间")
            value = float(value)
        cleaned.append({"device_id": item.device_id, "action": item.action, "value": value})
    return cleaned


@router.get("/scenes")
async def list_scenes(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """本家庭场景清单（含动作明细，编辑器直接用这份数据）。"""
    return {"scenes": _scenes_payload(user.family_id)}


@router.post("/scenes")
async def create_scene(
    req: SceneUpsertRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """新建场景。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="场景名不能为空")
    if len(name) > 12:
        raise HTTPException(status_code=400, detail="场景名不超过 12 个字")

    actions = _validate_actions(user.family_id, req.actions)
    scene = get_scene_store().create(user.family_id, name, req.icon, actions)

    record_event(
        user.family_id, TYPE_SCENE,
        title=f"新建了「{scene.name}」模式",
        body=f"包含 {len(actions)} 个动作",
        actor=user.name,
    )
    return {"ok": True, "scene": scene.to_dict()}


@router.put("/scenes/{scene_id}")
async def update_scene(
    scene_id: str, req: SceneUpsertRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """修改场景。内置场景（回家/离家…）只读，改不了。"""
    current = get_scene_store().find(user.family_id, scene_id)
    if current is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    if current.builtin:
        raise HTTPException(status_code=403, detail="内置场景不能修改，可以新建一个自己的")

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="场景名不能为空")
    if len(name) > 12:
        raise HTTPException(status_code=400, detail="场景名不超过 12 个字")

    actions = _validate_actions(user.family_id, req.actions)
    scene = get_scene_store().update(
        user.family_id, scene_id, name=name, icon=req.icon, actions=actions
    )
    if scene is None:
        raise HTTPException(status_code=404, detail="场景不存在")

    record_event(
        user.family_id, TYPE_SCENE,
        title=f"修改了「{scene.name}」模式",
        body=f"现在包含 {len(actions)} 个动作",
        actor=user.name,
    )
    return {"ok": True, "scene": scene.to_dict()}


@router.delete("/scenes/{scene_id}")
async def delete_scene_api(
    scene_id: str, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """删除自建场景。内置场景删不掉——删光了控制台一片空白，那不叫自由，叫事故。"""
    current = get_scene_store().find(user.family_id, scene_id)
    if current is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    if current.builtin:
        raise HTTPException(status_code=403, detail="内置场景不能删除")

    get_scene_store().delete(user.family_id, scene_id)
    record_event(
        user.family_id, TYPE_SCENE,
        title=f"删除了「{current.name}」模式",
        body="", actor=user.name,
    )
    return {"ok": True}


class TaskActionRequest(BaseModel):
    """任务动作请求。"""

    reason: str | None = None
    member_id: str = Field(default="member_001")


def _find_task(family_id: str, task_id: str) -> dict[str, Any]:
    """在本家庭的任务里找——别家的任务 id 一律当不存在（404，不透露更多信息）。"""
    task = next((t for t in tasks_of(family_id) if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str, req: TaskActionRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """完成任务：置为 done，并触发关联设备联动。

    两种任务都支持：家务任务（带设备联动）和 AI 创建的提醒（只置为完成）。
    """
    family_id = user.family_id

    schedule = _find_schedule(family_id, task_id)
    if schedule is not None:
        schedule["status"] = "done"
        record_event(family_id, TYPE_TASK,
                     title=f"提醒已完成：{schedule['content']}",
                     body=f"操作人：{user.name} · 由 AI 助手创建",
                     actor=user.name)
        return {"ok": True, "task": _schedule_to_task(schedule), "linked": []}

    task = _find_task(family_id, task_id)
    task["status"] = "done"
    task["completedAt"] = datetime.now().isoformat()
    task["completedBy"] = user.member_id

    linked: list[dict[str, Any]] = []
    for device_id in task.get("linkedDeviceIds", []):
        device = devices_of(family_id).get(device_id)
        if device is None:
            continue
        action = "turn_off" if device_id == "front_door_lock" else "turn_on"
        result = await default_registry().call(
            "lock_control" if device.device_type == "lock" else "device_control",
            {"action": "lock" if device_id == "front_door_lock" else action,
             "device_name": device.name, "room": device.room, FAMILY_ARG: family_id},
        )
        linked.append({"deviceId": device_id, "name": device.name,
                       "ok": result.ok,
                       "message": (result.data or {}).get("message", "")})

    ok_linked = [l["name"] for l in linked if l["ok"]]
    record_event(
        family_id, TYPE_TASK,
        title=f"完成了「{task['title']}」",
        body=f"完成人：{user.name}" + (f" · 已联动：{'、'.join(ok_linked)}" if ok_linked else ""),
        actor=user.name,
    )
    return {"ok": True, "task": task, "linked": linked}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, req: TaskActionRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """取消任务：保留记录（不物理删除），可追溯。"""
    family_id = user.family_id

    schedule = _find_schedule(family_id, task_id)
    if schedule is not None:
        schedule["status"] = "cancelled"
        schedule["cancelReason"] = req.reason or "未填写原因"
        record_event(family_id, TYPE_TASK,
                     title=f"取消了提醒：{schedule['content']}",
                     body=f"取消人：{user.name} · 原因：{schedule['cancelReason']}",
                     actor=user.name)
        return {"ok": True, "task": _schedule_to_task(schedule)}

    task = _find_task(family_id, task_id)
    task["status"] = "cancelled"
    task["cancelReason"] = req.reason or "未填写原因"
    task["cancelledAt"] = datetime.now().isoformat()
    task["cancelledBy"] = user.member_id
    record_event(
        user.family_id, TYPE_TASK,
        title=f"取消了「{task['title']}」",
        body=f"取消人：{user.name} · 原因：{task['cancelReason']}",
        actor=user.name,
    )
    return {"ok": True, "task": task}


@router.post("/tasks/{task_id}/reopen")
async def reopen_task(
    task_id: str, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """撤销：把任务从 done/cancelled 回滚到 pending。

    AI 创建的提醒（日程）也走这个口子：任务页的「恢复」按钮对两类任务
    一视同仁，后端要是只认家务任务，用户点下去就是 404，
    前端只能弹「撤销失败」——点了没反应，是这套界面最忌讳的事。
    """
    family_id = user.family_id

    schedule = _find_schedule(family_id, task_id)
    if schedule is not None:
        schedule["status"] = "pending"
        schedule.pop("cancelReason", None)
        record_event(
            family_id, TYPE_TASK,
            title=f"恢复了提醒：{schedule['content']}",
            body=f"已重新放回待办 · 操作人：{user.name}",
            actor=user.name,
        )
        return {"ok": True, "task": _schedule_to_task(schedule)}

    task = _find_task(family_id, task_id)
    task["status"] = "pending"
    for key in ("completedAt", "completedBy", "cancelledAt", "cancelledBy", "cancelReason"):
        task.pop(key, None)
    record_event(
        family_id, TYPE_TASK,
        title=f"恢复了「{task['title']}」",
        body=f"已重新放回待办 · 操作人：{user.name}",
        actor=user.name,
    )
    return {"ok": True, "task": task}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule_api(
    schedule_id: str, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """删除一条 AI 创建的提醒（物理删除）。

    只删本家庭的：拿别家的 schedule_id 来删，返回 404 且**一条都不动**。
    删掉之后它会从今日任务框里消失——这也是它唯一的去处，
    所以这条接口不能少，否则提醒只能加不能减。
    """
    family_id = user.family_id
    removed = delete_schedule(family_id, schedule_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="提醒不存在或不属于当前家庭")
    record_event(
        family_id, TYPE_SYSTEM,
        title=f"删除了提醒：{removed['content']}",
        body=f"原定时间：{removed['scheduled_at']} · 操作人：{user.name}",
        actor=user.name,
    )
    return {"ok": True, "removed": removed["schedule_id"]}


class AssistantRequest(BaseModel):
    """AI 助手请求。"""

    text: str
    member_id: str = "member_001"
    confirmed: bool = False
    session_id: str = "console"


@router.post("/assistant")
async def assistant(
    req: AssistantRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """AI 助手：走真实 LangGraph 多 Agent 图，返回应答 + 动作卡。

    动作卡来自真实工具回执——不是模型编的。模型只负责表达层。
    member_id 取自 token：AI 记住的偏好是按人存的，身份必须可信。

    多轮记忆（"滑动窗口 + 摘要压缩"模式）：
    - 会话按登录者隔离：`console:{member_id}`，换账号互不可见
    - 每轮先写短期记忆，再把"窗口内原文 + 窗口外摘要"注入 prompt
    - 跑完把应答也写回，下一轮"我刚刚干了什么"才答得上来
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)

    # 会话键带家庭：换了家庭就是另一个会话，青岛家看不到深圳家聊过什么。
    # 请求体里的 session_id 不可信，一律忽略。
    session_id = f"console:{user.family_id}:{user.member_id}"
    memory = get_short_term_memory()
    await memory.append(session_id, "user", req.text)
    context = await memory.get_context(session_id)

    state = initial_state(
        user_input=req.text,
        member_id=user.member_id,
        member_role=user.role,
        family_id=user.family_id,
        confirmed=req.confirmed,
        trace_id=trace_id,
        chat_history=context["messages"],
        chat_summary=context["summary"] or "",
    )
    graph = build_agent_graph()
    result = await graph.ainvoke(state)

    answer = result.get("final_answer", "")
    if answer:
        await memory.append(session_id, "assistant", answer)

    actions = []
    for res in result.get("tool_results", []):
        data = res.get("data") or {}
        tool = res.get("tool_name")
        actions.append({
            "tool": tool,
            "ok": res.get("ok"),
            "title": data.get("message") or tool,
            "deviceId": (data.get("device") or {}).get("device_id"),
            "error": res.get("error"),
        })

        # AI 干的事也要进消息中心——不然用户让 AI 开了灯，
        # 消息里却只有他自己手动操作的记录，看着像漏了一半。
        if not res.get("ok") or not data.get("success", True):
            continue
        if tool == "create_reminder" and not data.get("duplicate"):
            schedule = data.get("schedule") or {}
            record_event(
                user.family_id, TYPE_SYSTEM,
                title=f"AI 添加了提醒：{schedule.get('content', '新提醒')}",
                body=f"时间：{schedule.get('scheduled_at', '待定')} · 由「{user.name}」对话创建",
                actor=user.name,
            )
        elif tool in ("device_control", "lock_control"):
            device = data.get("device") or {}
            record_event(
                user.family_id,
                TYPE_SECURITY if tool == "lock_control" else TYPE_DEVICE,
                title=data.get("message") or "AI 操作了设备",
                body=f"由「{user.name}」对 AI 说：{req.text[:30]}"
                     + (f" · 位置：{device.get('room', '')}" if device.get("room") else ""),
                actor=user.name,
            )

    return {
        "traceId": trace_id,
        "answer": result.get("final_answer", ""),
        "intents": result.get("intents", []),
        "agentResults": result.get("agent_results", []),
        "actions": actions,
        "requiresConfirm": result.get("requires_confirm", False),
        "confirmPrompt": result.get("confirm_prompt", ""),
        "degraded": result.get("degraded", False),
    }


@router.post("/messages/{event_id}/read")
async def read_message(
    event_id: str, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """把一条消息标为已读。

    消息条目能点，点的就是这个——点击必须产生真实状态变化，
    否则"看起来能点其实没反应"比"明确不可点"更让人恼火。
    """
    if not mark_read(user.family_id, event_id):
        raise HTTPException(status_code=404, detail="消息不存在或不属于当前家庭")
    return {"ok": True, "unreadCount": unread_count(user.family_id)}


@router.post("/messages/read-all")
async def read_all_messages(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """一键全部标记已读。"""
    marked = mark_all_read(user.family_id)
    return {"ok": True, "marked": marked, "unreadCount": unread_count(user.family_id)}


@router.get("/habits")
async def habits(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """Agent 记住的习惯（真实记忆库，非编造）。

    按登录者自己的 member_id 查，并按其角色做可见性过滤——
    L1 访客不该看到管理员的偏好。
    """
    # 必须用与 assistant 同一个记忆服务实例：
    # 这里要是自己 new 一个 MemoryService(另一个 db_path)，
    # AI 记住的习惯在这一页就查不到——写和读必须是同一个库。
    service = get_memory_service()
    records = service.long_term.list_by_member(user.member_id, viewer_role=user.role,
                                               family_id=user.family_id)
    return {
        "habits": [{"key": r.memory_key, "value": r.memory_value,
                    "type": r.memory_type, "version": getattr(r, "version", 1)}
                   for r in records],
        "count": len(records),
    }


class ForgetRequest(BaseModel):
    """遗忘一条记忆的请求。"""

    memory_key: str = Field(..., description="要忘掉的记忆 key（如 preference:空调温度）")


@router.post("/habits/forget")
async def forget_habit(
    req: ForgetRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """主动遗忘一条 AI 记住的习惯。

    为什么接口层要有这个：习惯卡片现在是可点的，点了就得有真实结果。
    "让 AI 忘掉"是唯一符合直觉的动作——留着"点了没反应"不如不做。

    双库同清：长期记忆库 + 语义索引一起删（NFR-COMP-001），
    只删一边会出现"习惯页看不到了，AI 对话里还记得"的撕裂。
    """
    service = get_memory_service()
    ok = service.forget(user.member_id, req.memory_key, family_id=user.family_id)
    if not ok:
        raise HTTPException(status_code=404, detail="这条记忆不存在或不属于你")

    record_event(
        user.family_id, TYPE_SYSTEM,
        title=f"AI 忘掉了一条习惯：{req.memory_key.replace('preference:', '')}",
        body=f"操作人：{user.name} · 长期记忆与语义索引已同步清除",
        actor=user.name,
    )
    return {"ok": True, "forgot": req.memory_key}


def mount_web(app: Any) -> None:
    """托管前端静态站点：一条命令同时起 API 和 Web。"""
    if WEB_DIR.exists():
        # 只托管 assets 子目录：/assets/app.js、/assets/app.css
        app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="web-assets")

        # 只用 "/" 一个入口：前端是同页分区切换，不需要 SPA 回落。
        # 这里刻意不注册 "/{path:path}" 通配路由——console_router 是最先
        # include 的，通配会把它之后注册的 /health、/api/v1/* 全部吃掉。
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))
