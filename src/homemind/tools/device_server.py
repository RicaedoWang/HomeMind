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
from ..core.tenancy import DEFAULT_FAMILY_ID, FAMILY_ARG
from ..core.trace import get_logger
from .registry import ToolSpec

logger = get_logger("tools.device")

# ---------------- Mock 设备层 ----------------
# 真实环境这里应替换为设备厂商网关 / IoT 平台的调用

# 设备按家庭分桶：Dict[family_id, Dict[device_id, DeviceState]]。
# 两个家庭可以有同名甚至同 id 的设备（都有"客厅灯"），
# 它们是两个家庭的**不同实体**，绝不能共享一份状态。
_DEVICES: dict[str, dict[str, DeviceState]] = {
    # 深圳的家：亚热带，靠空调和加湿器
    "family_sz": {
        # 可调档设备（灯 / 窗帘）必须给初值：以前没设 value，turn_on 之后
        # 卡面显示"已开 0%"——灯开着但亮度为零，等于没开，看着像坏了。
        "living_room_light": DeviceState("living_room_light", "客厅灯", "客厅", "light",
                                         value=80.0),
        "living_room_ac": DeviceState("living_room_ac", "客厅空调", "客厅", "ac", value=26.0),
        "living_room_curtain": DeviceState("living_room_curtain", "客厅窗帘", "客厅", "curtain",
                                           value=100.0),
        "bedroom_light": DeviceState("bedroom_light", "主卧灯", "主卧", "light", value=60.0),
        "front_door_lock": DeviceState("front_door_lock", "大门门锁", "玄关", "lock"),
        # 以下为控制台界面补齐的设备（洗衣机/晾衣机/扫地机/加湿器/浇花器），
        # 命名刻意避开"灯""空调""窗帘""门锁"等已有子串，不影响既有模糊匹配。
        "balcony_washer": DeviceState("balcony_washer", "洗衣机", "阳台", "washer"),
        "balcony_dryer": DeviceState("balcony_dryer", "智能晾衣机", "阳台", "dryer"),
        "living_room_robot": DeviceState("living_room_robot", "扫地机器人", "客厅", "robot"),
        "bedroom_humidifier": DeviceState("bedroom_humidifier", "加湿器", "主卧", "humidifier",
                                          value=50.0),
        "balcony_valve": DeviceState("balcony_valve", "智能浇花器", "阳台", "valve"),
    },
    # 青岛的家：海边湿冷，靠地暖和新风——设备清单刻意与深圳不同，
    # 这样"两个家庭数据不同"一眼就能看出来，不是同一个列表换个名字。
    "family_qd": {
        "living_room_light": DeviceState("living_room_light", "客厅灯", "客厅", "light",
                                         value=80.0),
        "living_room_heater": DeviceState("living_room_heater", "客厅地暖", "客厅", "heater",
                                          value=20.0),
        "living_room_fan": DeviceState("living_room_fan", "新风系统", "客厅", "fan"),
        "kitchen_hood": DeviceState("kitchen_hood", "厨房油烟机", "厨房", "hood"),
        "balcony_dryer_qd": DeviceState("balcony_dryer_qd", "阳台晾衣机", "阳台", "dryer"),
        "entry_lock": DeviceState("entry_lock", "入户门锁", "玄关", "lock"),
    },
}

# 高危设备：动作执行前强制确认
_HIGH_RISK_TYPES = {"lock", "gas_valve"}

# 初始设备快照：评测（以及任何需要可复现结果的场景）靠它把状态打回原点。
# Mock 状态是模块级全局的——第 1 条用例把灯关了，第 20 条查到的就是"关"，
# 不重置的话评测结果根本不可复现。
_INITIAL_DEVICES: dict[str, dict[str, DeviceState]] = {
    family_id: {
        key: DeviceState(device_id=dev.device_id, name=dev.name, room=dev.room,
                         device_type=dev.device_type, power=dev.power, value=dev.value)
        for key, dev in bucket.items()
    }
    for family_id, bucket in _DEVICES.items()
}


def devices_of(family_id: str | None = None) -> dict[str, DeviceState]:
    """取某个家庭的设备桶。未知家庭返回空桶（不自动建，避免脏数据）。"""
    return _DEVICES.get(family_id or DEFAULT_FAMILY_ID, {})


def _resolve_family(args: dict[str, Any]) -> str:
    """取服务端注入的租户标识。

    只认 `__family_id`——它不在工具 Schema 里，模型看不见也造不出来。
    刻意**不读**不带下划线的同名参数：那是模型可能幻觉出来的字段，
    一旦采信，等于把"去操作哪个家庭"的决定权交给了模型输出。
    """
    return args.get(FAMILY_ARG) or DEFAULT_FAMILY_ID


def reset_devices() -> None:
    """把所有家庭的 Mock 设备状态重置为初始值（评测与测试前置调用）。"""
    for family_id, snapshot in _INITIAL_DEVICES.items():
        bucket = _DEVICES.setdefault(family_id, {})
        for key, dev in snapshot.items():
            bucket[key] = DeviceState(
                device_id=dev.device_id, name=dev.name, room=dev.room,
                device_type=dev.device_type, power=dev.power, value=dev.value,
            )


def _find_device(
    device_name: str | None,
    room: str | None = None,
    family_id: str | None = None,
) -> DeviceState | None:
    """按名称模糊匹配设备，可带房间限定，且**只在指定家庭内找**。

    匹配优先级：完全匹配名称 > 名称子串 > 房间 + 类型子串
    """
    bucket = devices_of(family_id)
    if not device_name:
        if room:
            for dev in bucket.values():
                if dev.room == room:
                    return dev
        return None

    # 完全匹配
    for dev in bucket.values():
        if dev.name == device_name:
            return dev

    # 子串匹配（"灯" 命中 "客厅灯"，配合 room 可消歧）
    candidates = [d for d in bucket.values() if device_name in d.name]
    if room:
        candidates = [d for d in candidates if d.room == room]
    return candidates[0] if candidates else None


async def _handle_device_control(args: dict[str, Any]) -> dict[str, Any]:
    """设备控制：开关、调值、查询。"""
    action: str = args.get("action", "")
    device_name: str | None = args.get("device_name")
    room: str | None = args.get("room")
    value: float | None = args.get("value")
    family_id = _resolve_family(args)

    device = _find_device(device_name, room, family_id)
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
    """列出本家庭的设备状态（绝不会列出别的家庭的设备）。"""
    room: str | None = args.get("room")
    family_id = _resolve_family(args)
    devices = [d for d in devices_of(family_id).values() if room is None or d.room == room]
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
    family_id = _resolve_family(args)
    device = _find_device(args.get("device_name") or "门锁", None, family_id)
    if device is None:
        return {"success": False, "message": "没有找到门锁设备"}
    device.power = action == "unlock"
    message = f"已{'开锁' if action == 'unlock' else '上锁'}：{device.name}"
    logger.warning("lock control: %s -> %s", device.device_id, action,
                   extra={"extra_fields": {"event": "device.lock", "action": action}})
    return {"success": True, "message": message, "device": device.to_dict()}


def get_device_states(family_id: str | None = None) -> dict[str, DeviceState]:
    """获取某个家庭的设备态（演示界面 / 控制台用）。

    注意这里返回的是**桶内引用**，不是拷贝——控制台靠它读实时状态，
    改状态必须走工具（device_control），不要在这里直接改，
    否则就绕开了超时/重试/熔断/幂等那一整套治理。
    """
    return devices_of(family_id)


def device_belongs_to(family_id: str, device_id: str) -> bool:
    """判断设备是否属于该家庭——跨家庭访问的断言就靠它。

    接口层必须先过这一关再调工具：拿别家的 device_id 来控制，
    要在**动设备之前**拦下来并返回 403，而不是先执行了再道歉。
    """
    return device_id in devices_of(family_id)
