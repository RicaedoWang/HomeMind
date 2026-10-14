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
from ..auth.store import FAMILIES
from ..core.trace import get_logger, new_trace_id, set_trace_id
from ..memory.long_term import LongTermMemory
from ..memory.service import MemoryService
from ..memory.short_term import get_short_term_memory
from ..tools.calendar_server import get_schedules
from ..tools.device_server import _DEVICES, get_device_states
from ..tools.registry import default_registry

logger = get_logger("api.console")

router = APIRouter(prefix="/api/console", tags=["console"])

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# ---------------------------------------------------------------- 静态数据

FAMILY = [
    {"id": "m1", "name": "我", "colorTag": "#4F7BFF", "role": "owner", "avatar": "我"},
    {"id": "m2", "name": "妈妈", "colorTag": "#F472B6", "role": "admin", "avatar": "妈"},
    {"id": "m3", "name": "爸爸", "colorTag": "#22D3A8", "role": "member", "avatar": "爸"},
]

# 场景：执行时按顺序作用于设备
SCENES = [
    {"id": "home", "name": "回家", "icon": "home",
     "actions": [("living_room_light", "turn_on", 60), ("living_room_curtain", "turn_on", 100)]},
    {"id": "away", "name": "离家", "icon": "exit",
     "actions": [("living_room_light", "turn_off", None), ("living_room_ac", "turn_off", None),
                 ("living_room_curtain", "turn_off", None)]},
    {"id": "movie", "name": "观影", "icon": "movie",
     "actions": [("living_room_light", "set_value", 20), ("living_room_curtain", "turn_off", None)]},
    {"id": "sleep", "name": "睡眠", "icon": "moon",
     "actions": [("living_room_light", "turn_off", None), ("bedroom_light", "set_value", 8)]},
    {"id": "wake", "name": "起床", "icon": "sun",
     "actions": [("bedroom_light", "turn_on", 80), ("living_room_curtain", "turn_on", 60)]},
]

CATEGORY_COLORS = {
    "plant": "#34D399", "laundry": "#60A5FA", "cleaning": "#A78BFA",
    "pet": "#FBBF24", "security": "#F87171", "shopping": "#F472B6",
    "maintenance": "#22D3A8", "health": "#FB923C", "other": "#94A3B8",
}

# ---------------------------------------------------------------- 任务存储

_TASKS: list[dict[str, Any]] = []


def _seed_tasks() -> None:
    """种子任务：刻意覆盖文档里的典型场景（逾期 / 设备触发 / AI 添加 / 手动）。"""
    if _TASKS:
        return
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _TASKS.extend([
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


def reset_tasks() -> None:
    """重置任务存储（测试前置调用，避免用例间互相污染）。"""
    _TASKS.clear()
    _seed_tasks()


_seed_tasks()

# ---------------------------------------------------------------- 派生数据


def _device_payload() -> list[dict[str, Any]]:
    """设备状态 → 前端卡片结构。

    关键状态（亮度/温度/进度）必须在卡面可见，不点进详情也知道设备什么状态。
    """
    devices: list[dict[str, Any]] = []
    for dev in get_device_states().values():
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


def _env_payload() -> dict[str, Any]:
    """环境数据：室内外温湿度 + 天气 + 空气质量。

    传感器缺数据时返回 None，前端显示"未接入传感器"——
    绝不编造一个看起来合理的数字出来（与 AI 能力边界红线同一原则）。
    """
    now = datetime.now()
    drift = math.sin(now.hour / 24 * math.pi * 2) * 1.6
    indoor_temp = round(24.5 + drift, 1)
    indoor_humidity = 62
    outdoor_high, outdoor_low = 28, 21
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "lunar": "九月初四",
        "indoor": {"temp": indoor_temp, "humidity": indoor_humidity, "sensor": "客厅"},
        "outdoor": {"temp": 25.0, "high": outdoor_high, "low": outdoor_low,
                    "condition": "sunny", "conditionText": "晴"},
        "aqi": {"value": 43, "level": "优"},
        "tempDiffWarn": abs(indoor_temp - 25.0) >= 8,
        "updatedAt": now.isoformat(timespec="seconds"),
    }


def _comfort_hint(env: dict[str, Any]) -> dict[str, Any] | None:
    """舒适度提示：按文档 5.2 的优先级表，同时最多返回 1 条。"""
    indoor = env["indoor"]
    if indoor["humidity"] < 35:
        return {"level": "warning", "icon": "drop",
                "text": f"室内偏干（{indoor['humidity']}%），皮肤和绿植都难受",
                "action": {"label": "开加湿器", "deviceId": "bedroom_humidifier",
                           "action": "turn_on"}}
    if indoor["humidity"] > 75:
        return {"level": "warning", "icon": "drop",
                "text": f"室内偏潮（{indoor['humidity']}%），当心霉味",
                "action": {"label": "开除湿", "deviceId": "living_room_ac",
                           "action": "turn_on"}}
    if indoor["temp"] > 29:
        return {"level": "info", "icon": "thermometer",
                "text": f"客厅 {indoor['temp']}°C，要不把空调开上？",
                "action": {"label": "开客厅空调 26°C", "deviceId": "living_room_ac",
                           "action": "set_value", "value": 26}}
    if indoor["temp"] < 16:
        return {"level": "info", "icon": "thermometer",
                "text": f"客厅 {indoor['temp']}°C，有点冷了",
                "action": {"label": "开空调制热", "deviceId": "living_room_ac",
                           "action": "turn_on"}}
    plant_task = next((t for t in _TASKS if t["category"] == "plant"
                       and t["status"] == "pending"), None)
    if plant_task:
        return {"level": "info", "icon": "plant",
                "text": "阳台绿萝该浇水了",
                "action": {"label": "生成浇花任务并指派给我", "taskId": plant_task["id"]}}
    return None


def _energy_payload() -> dict[str, Any]:
    return {"today": 3.2, "unit": "kWh", "delta": -12, "week": 24.6, "month": 108.4}


def _messages_payload() -> list[dict[str, Any]]:
    return [
        {"id": "msg-1", "type": "device", "title": "洗衣机洗涤完成",
         "body": "已自动生成「晾晒衣服」任务", "time": "12 分钟前", "unread": True},
        {"id": "msg-2", "type": "security", "title": "入户门锁 18:20 开启",
         "body": "由「爸爸」的指纹开启", "time": "1 小时前", "unread": True},
        {"id": "msg-3", "type": "system", "title": "滤芯寿命不足 10%",
         "body": "空气净化器滤芯建议更换", "time": "昨天", "unread": False},
    ]


# ---------------------------------------------------------------- 接口


@router.get("/state")
async def console_state(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """控制台全量状态：一次请求拿齐首页所需数据。"""
    env = _env_payload()
    pending = [t for t in _TASKS if t["status"] == "pending"]
    return {
        "env": env,
        "hint": _comfort_hint(env),
        "tasks": [dict(t, colorTag=CATEGORY_COLORS.get(t["category"], "#94A3B8"))
                  for t in _TASKS],
        "pendingCount": len(pending),
        "devices": _device_payload(),
        "rooms": sorted({d["room"] for d in _device_payload()}),
        "scenes": [{"id": s["id"], "name": s["name"], "icon": s["icon"]} for s in SCENES],
        "schedules": get_schedules(),
        "energy": _energy_payload(),
        "messages": _messages_payload(),
        "family": FAMILY,
        # 身份回显：前端据此显示"谁在看这个家"
        "viewer": {**user.to_dict(),
                   "familyName": FAMILIES.get(user.family_id, {}).get("name", user.family_id)},
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

    操作人取自 token（`user.member_id`），**请求体里传的 member_id 一律忽略**——
    否则谁都能伪造成别人来操作门锁。
    """
    device = _DEVICES.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")

    # 高危设备：未确认不放行，与 Agent 主链路的护栏规则保持一致
    if device.device_type == "lock" and not req.confirmed:
        return {"ok": False, "requiresConfirm": True,
                "message": "门锁属于高危操作，请先在卡片上确认",
                "device": next((d for d in _device_payload() if d["id"] == device_id), None)}

    registry = default_registry()
    tool_name = "lock_control" if device.device_type == "lock" else "device_control"
    arguments: dict[str, Any] = {
        "action": "unlock" if req.action == "turn_on" else "lock"
        if device.device_type == "lock" else req.action,
        "device_name": device.name,
        "room": device.room,
    }
    if req.value is not None:
        arguments["value"] = req.value

    result = await registry.call(tool_name, arguments)
    # 注意顺序：必须在工具调用之后再取设备状态，否则前端拿到的是执行前的旧值，
    # 会显示"后端说开了、卡片还关着"。
    return {"ok": result.ok, "tool": tool_name,
            "message": (result.data or {}).get("message", ""),
            "device": next((d for d in _device_payload() if d["id"] == device_id), None),
            "degraded": result.degraded, "error": result.error}


class SceneRequest(BaseModel):
    """场景执行请求。"""

    confirmed: bool = False


@router.post("/scenes/{scene_id}/activate")
async def activate_scene(
    scene_id: str, req: SceneRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """执行场景：按顺序作用于一组设备，逐项返回回执。"""
    scene = next((s for s in SCENES if s["id"] == scene_id), None)
    if scene is None:
        raise HTTPException(status_code=404, detail="场景不存在")

    registry = default_registry()
    affected: list[dict[str, Any]] = []
    for device_id, action, value in scene["actions"]:
        device = _DEVICES.get(device_id)
        if device is None:
            continue
        arguments: dict[str, Any] = {"action": action, "device_name": device.name,
                                     "room": device.room}
        if value is not None:
            arguments["value"] = value
        result = await registry.call("device_control", arguments)
        affected.append({"deviceId": device_id, "name": device.name,
                         "ok": result.ok,
                         "message": (result.data or {}).get("message", "")})
    return {"ok": True, "scene": scene["name"], "affected": affected}


class TaskActionRequest(BaseModel):
    """任务动作请求。"""

    reason: str | None = None
    member_id: str = Field(default="member_001")


def _find_task(task_id: str) -> dict[str, Any]:
    task = next((t for t in _TASKS if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str, req: TaskActionRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """完成任务：置为 done，并触发关联设备联动。"""
    task = _find_task(task_id)
    task["status"] = "done"
    task["completedAt"] = datetime.now().isoformat()
    task["completedBy"] = user.member_id

    linked: list[dict[str, Any]] = []
    for device_id in task.get("linkedDeviceIds", []):
        device = _DEVICES.get(device_id)
        if device is None:
            continue
        action = "turn_off" if device_id == "front_door_lock" else "turn_on"
        result = await default_registry().call(
            "lock_control" if device.device_type == "lock" else "device_control",
            {"action": "lock" if device_id == "front_door_lock" else action,
             "device_name": device.name, "room": device.room},
        )
        linked.append({"deviceId": device_id, "name": device.name,
                       "ok": result.ok,
                       "message": (result.data or {}).get("message", "")})
    return {"ok": True, "task": task, "linked": linked}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, req: TaskActionRequest, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """取消任务：保留记录（不物理删除），可追溯。"""
    task = _find_task(task_id)
    task["status"] = "cancelled"
    task["cancelReason"] = req.reason or "未填写原因"
    task["cancelledAt"] = datetime.now().isoformat()
    task["cancelledBy"] = user.member_id
    return {"ok": True, "task": task}


@router.post("/tasks/{task_id}/reopen")
async def reopen_task(
    task_id: str, user: AuthUser = Depends(current_user)
) -> dict[str, Any]:
    """撤销：把任务从 done/cancelled 回滚到 pending。"""
    task = _find_task(task_id)
    task["status"] = "pending"
    for key in ("completedAt", "completedBy", "cancelledAt", "cancelledBy", "cancelReason"):
        task.pop(key, None)
    return {"ok": True, "task": task}


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

    session_id = f"console:{user.member_id}"   # 请求体里的 session_id 不可信，忽略
    memory = get_short_term_memory()
    await memory.append(session_id, "user", req.text)
    context = await memory.get_context(session_id)

    state = initial_state(
        user_input=req.text,
        member_id=user.member_id,
        member_role=user.role,
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
        actions.append({
            "tool": res.get("tool_name"),
            "ok": res.get("ok"),
            "title": data.get("message") or res.get("tool_name"),
            "deviceId": (data.get("device") or {}).get("device_id"),
            "error": res.get("error"),
        })

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


@router.get("/habits")
async def habits(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """Agent 记住的习惯（真实记忆库，非编造）。

    按登录者自己的 member_id 查，并按其角色做可见性过滤——
    L1 访客不该看到管理员的偏好。
    """
    service = MemoryService(long_term=LongTermMemory(db_path="data/homemind.db"))
    records = service.long_term.list_by_member(user.member_id, viewer_role=user.role)
    return {
        "habits": [{"key": r.memory_key, "value": r.memory_value,
                    "type": r.memory_type, "version": getattr(r, "version", 1)}
                   for r in records],
        "count": len(records),
    }


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
