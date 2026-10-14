"""设备控制 MCP Server（Mock 实现，对齐 FR-TOOL-002）。

说明：
- 本模块实现了符合 MCP 约定的工具定义（name / description / inputSchema），
  真实部署时把 handler 里的 Mock 设备层换成设备厂商 SDK 即可，
  Schema 和注册方式完全不用改——这就是"工具标准化"的价值。
- 门锁、燃气阀标记为 high_risk，执行前必须二次确认（FR-SEC-001）。
"""

from __future__ import annotations

from typing import Any

from ..core.models import DeviceState
from ..core.trace import get_logger
from .registry import ToolSpec

logger = get_logger("tools.device")

# ---------------- Mock 设备层 ----------------
# 真实环境这里应替换为设备厂商网关 / IoT 平台的调用
_DEVICES: dict[str, DeviceState] = {
    "living_room_light": DeviceState("living_room_light", "客厅灯", "客厅", "light"),
    "living_room_ac": DeviceState("living_room_ac", "客厅空调", "客厅", "ac", value=26.0),
    "living_room_curtain": DeviceState("living_room_curtain", "客厅窗帘", "客厅", "curtain"),
    "bedroom_light": DeviceState("bedroom_light", "主卧灯", "主卧", "light"),
    "front_door_lock": DeviceState("front_door_lock", "大门门锁", "玄关", "lock"),
}

# 高危设备：动作执行前强制确认
_HIGH_RISK_TYPES = {"lock", "gas_valve"}

# 初始设备快照：评测（以及任何需要可复现结果的场景）靠它把状态打回原点。
# Mock 状态是模块级全局的——第 1 条用例把灯关了，第 20 条查到的就是"关"，
# 不重置的话评测结果根本不可复现。
_INITIAL_DEVICES: dict[str, DeviceState] = {
    key: DeviceState(device_id=dev.device_id, name=dev.name, room=dev.room,
                     device_type=dev.device_type, power=dev.power, value=dev.value)
    for key, dev in _DEVICES.items()
}


def reset_devices() -> None:
    """把 Mock 设备状态重置为初始值（评测与测试前置调用）。"""
    for key, snapshot in _INITIAL_DEVICES.items():
        _DEVICES[key] = DeviceState(
            device_id=snapshot.device_id, name=snapshot.name, room=snapshot.room,
            device_type=snapshot.device_type, power=snapshot.power, value=snapshot.value,
        )


def _find_device(device_name: str | None, room: str | None = None) -> DeviceState | None:
    """按名称模糊匹配设备，可带房间限定。

    匹配优先级：完全匹配名称 > 名称子串 > 房间 + 类型子串
    """
    if not device_name:
        if room:
            for dev in _DEVICES.values():
                if dev.room == room:
                    return dev
        return None

    # 完全匹配
    for dev in _DEVICES.values():
        if dev.name == device_name:
            return dev

    # 子串匹配（"灯" 命中 "客厅灯"，配合 room 可消歧）
    candidates = [d for d in _DEVICES.values() if device_name in d.name]
    if room:
        candidates = [d for d in candidates if d.room == room]
    return candidates[0] if candidates else None


async def _handle_device_control(args: dict[str, Any]) -> dict[str, Any]:
    """设备控制：开关、调值、查询。"""
    action: str = args.get("action", "")
    device_name: str | None = args.get("device_name")
    room: str | None = args.get("room")
    value: float | None = args.get("value")

    device = _find_device(device_name, room)
    if device is None:
        return {
            "success": False,
            "message": f"没有找到设备：{device_name or room or '未指定'}",
        }

    if action == "turn_on":
        device.power = True
        message = f"已打开{device.name}"
    elif action == "turn_off":
        device.power = False
        message = f"已关闭{device.name}"
    elif action == "set_value":
        if value is None:
            return {"success": False, "message": "调值动作缺少 value 参数"}
        device.value = float(value)
        device.power = True
        unit = "度" if device.device_type == "ac" else ""
        message = f"已将{device.name}设置为{device.value}{unit}"
    elif action == "query":
        message = f"{device.name}当前状态：{'开' if device.power else '关'}"
        if device.value is not None:
            message += f"，设定值 {device.value}"
    else:
        return {"success": False, "message": f"不支持的动作：{action}"}

    logger.info("device control: %s -> %s", device.device_id, action,
                extra={"extra_fields": {"event": "device.control",
                                        "device": device.device_id, "action": action}})
    return {"success": True, "message": message, "device": device.to_dict()}


async def _handle_device_list(args: dict[str, Any]) -> dict[str, Any]:
    """列出全部设备状态。"""
    room: str | None = args.get("room")
    devices = [d for d in _DEVICES.values() if room is None or d.room == room]
    return {
        "success": True,
        "devices": [d.to_dict() for d in devices],
        "count": len(devices),
    }


def build_device_tools() -> list[ToolSpec]:
    """构建设备相关的 MCP 工具列表。"""
    return [
        ToolSpec(
            name="device_control",
            description="控制家居设备：开灯、关灯、调节空调温度、开关窗帘、开关门锁等",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["turn_on", "turn_off", "set_value", "query"],
                        "description": "动作类型",
                    },
                    "device_name": {"type": "string", "description": "设备名，如 客厅灯、空调"},
                    "room": {"type": "string", "description": "房间名，用于消歧，如 客厅、主卧"},
                    "value": {"type": "number", "description": "目标数值，调节温度或亮度时使用"},
                },
                "required": ["action"],
            },
            handler=_handle_device_control,
            high_risk=False,
            mutating=True,
        ),
        ToolSpec(
            name="device_list",
            description="查询家中设备清单及当前状态",
            parameters={
                "type": "object",
                "properties": {"room": {"type": "string", "description": "按房间过滤，可省略"}},
            },
            handler=_handle_device_list,
            mutating=False,
        ),
        ToolSpec(
            name="lock_control",
            description="控制门锁开关。高危操作，执行前必须向用户二次确认",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["lock", "unlock"], "description": "上锁或开锁"},
                    "device_name": {"type": "string", "description": "门锁名称"},
                },
                "required": ["action"],
            },
            handler=_handle_lock_control,
            high_risk=True,
            mutating=True,
        ),
    ]


async def _handle_lock_control(args: dict[str, Any]) -> dict[str, Any]:
    """门锁控制（高危）。"""
    action: str = args.get("action", "")
    device = _find_device(args.get("device_name") or "门锁")
    if device is None:
        return {"success": False, "message": "没有找到门锁设备"}
    device.power = action == "unlock"
    message = f"已{'开锁' if action == 'unlock' else '上锁'}：{device.name}"
    logger.warning("lock control: %s -> %s", device.device_id, action,
                   extra={"extra_fields": {"event": "device.lock", "action": action}})
    return {"success": True, "message": message, "device": device.to_dict()}


def reset_devices() -> None:
    """重置全部设备到初始态（测试用）。"""
    for dev in _DEVICES.values():
        dev.power = False
    _DEVICES["living_room_ac"].value = 26.0


def get_device_states() -> dict[str, DeviceState]:
    """获取设备态（演示界面用）。"""
    return _DEVICES
