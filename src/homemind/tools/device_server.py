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
from .device_store import get_device_store
from .registry import ToolSpec

logger = get_logger("tools.device")

# ---------------- Mock 设备层 ----------------
# 真实环境这里应替换为设备厂商网关 / IoT 平台的调用

# 内置设备模板。
#
# **这不是设备的真相，只是首次播种用的模板。** 真相在 `DeviceStore`（SQLite）。
# 之所以要搬到库里：V1.3 起用户可以自己添加电器，并且内置设备也能删——
# 代码常量当真相的话，用户删掉洗衣机，进程重启又长回来，删了个寂寞。
#
# 可调范围（min/max/unit）是这一版补的：用户自建的"咖啡机 3 档"需要知道自己
# 能调到哪里，否则 set_value 收多少都照收，界面也不知道该画什么刻度。
_BUILTIN_TEMPLATES: dict[str, list[DeviceState]] = {
    # 深圳的家：亚热带，靠空调和加湿器
    "family_sz": [
        # 可调档设备必须给 value：以前没设 value，turn_on 之后卡面显示"已开 0%"——
        # 灯开着但亮度为零，等于没开，看着像坏了。
        DeviceState("living_room_light", "客厅灯", "客厅", "light",
                    value=80.0, min_value=0.0, max_value=100.0, unit="%"),
        DeviceState("living_room_ac", "客厅空调", "客厅", "ac",
                    value=26.0, min_value=16.0, max_value=30.0, unit="°C"),
        DeviceState("living_room_curtain", "客厅窗帘", "客厅", "curtain",
                    value=100.0, min_value=0.0, max_value=100.0, unit="%"),
        DeviceState("bedroom_light", "主卧灯", "主卧", "light",
                    value=60.0, min_value=0.0, max_value=100.0, unit="%"),
        DeviceState("front_door_lock", "大门门锁", "玄关", "lock"),
        # 以下为控制台界面补齐的设备（洗衣机/晾衣机/扫地机/加湿器/浇花器），
        # 命名刻意避开"灯""空调""窗帘""门锁"等已有子串，不影响既有模糊匹配。
        DeviceState("balcony_washer", "洗衣机", "阳台", "washer"),
        DeviceState("balcony_dryer", "智能晾衣机", "阳台", "dryer"),
        DeviceState("living_room_robot", "扫地机器人", "客厅", "robot"),
        DeviceState("bedroom_humidifier", "加湿器", "主卧", "humidifier",
                    value=50.0, min_value=0.0, max_value=100.0, unit="%"),
        DeviceState("balcony_valve", "智能浇花器", "阳台", "valve"),
    ],
    # 青岛的家：海边湿冷，靠地暖和新风——设备清单刻意与深圳不同，
    # 这样"两个家庭数据不同"一眼就能看出来，不是同一个列表换个名字。
    "family_qd": [
        DeviceState("living_room_light", "客厅灯", "客厅", "light",
                    value=80.0, min_value=0.0, max_value=100.0, unit="%"),
        DeviceState("living_room_heater", "客厅地暖", "客厅", "heater",
                    value=20.0, min_value=5.0, max_value=35.0, unit="°C"),
        DeviceState("living_room_fan", "新风系统", "客厅", "fan"),
        DeviceState("kitchen_hood", "厨房油烟机", "厨房", "hood"),
        DeviceState("balcony_dryer_qd", "阳台晾衣机", "阳台", "dryer"),
        DeviceState("entry_lock", "入户门锁", "玄关", "lock"),
    ],
}

# 新注册家庭用的模板：取深圳那套（设备最全的 10 台）。
# 与 `family_store.DEFAULT_SCENE_TEMPLATE` 必须指向同一套 device_id，
# 否则新家的默认场景全是空转（场景里的 device_id 找不到设备）。
NEW_FAMILY_TEMPLATE_KEY = "family_sz"

# 高危设备类型：动作执行前强制确认
_HIGH_RISK_TYPES = {"lock", "gas_valve"}


def devices_of(family_id: str | None = None) -> dict[str, DeviceState]:
    """取某个家庭的设备桶。未知家庭返回空桶（不自动建，避免脏数据）。

    内置家庭在这里做**懒播种**：库是空的、且没播过种，就把模板灌进去。
    为什么不在模块加载时播种：那需要一个已经就绪的数据库连接，
    而测试会先把 store 换成临时库（`set_device_store`），模块加载时换不了。
    懒播种天然跟着"当前用的是哪个库"走，测试和线上不会打架。

    播种幂等靠 `device_seed_log`：用户把设备删光之后，这里读到空桶也不会复活设备。
    """
    fid = family_id or DEFAULT_FAMILY_ID
    store = get_device_store()
    bucket = store.devices_of(fid)
    if bucket:
        return bucket
    template = _BUILTIN_TEMPLATES.get(fid)
    if template and store.seed_family(fid, template):
        return store.devices_of(fid)
    return bucket


def _resolve_family(args: dict[str, Any]) -> str:
    """取服务端注入的租户标识。

    只认 `__family_id`——它不在工具 Schema 里，模型看不见也造不出来。
    刻意**不读**不带下划线的同名参数：那是模型可能幻觉出来的字段，
    一旦采信，等于把"去操作哪个家庭"的决定权交给了模型输出。
    """
    return args.get(FAMILY_ARG) or DEFAULT_FAMILY_ID


def reset_devices() -> None:
    """把设备状态重置为出厂（评测与测试前置调用）。

    语义和以前一致，但现在是"删库再播种"：
    - 内置家庭：清空后按模板重播，等下次 `devices_of` 懒播种时生效；
    - 其它家庭（测试期间注册的 `family_xxxx`）：连播种标记一起清掉，
      这样下一条用例查它们仍然是空桶，不会因为前置用例多出一套设备。
    """
    store = get_device_store()
    for family_id in _BUILTIN_TEMPLATES:
        store.reset_family(family_id)
    for family_id in store.all_family_ids():
        if family_id not in _BUILTIN_TEMPLATES:
            store.reset_family(family_id)


def ensure_family(family_id: str) -> bool:
    """给新注册的家庭初始化一套设备（幂等）。返回是否真的新建了。

    刻意**不**写进 devices_of：那个函数的语义是"读"，读的时候顺手建数据会让
    评测结果不可复现（查一次状态就多一个家庭）。初始化必须是显式动作，
    由注册流程调用，谁建的、什么时候建的，代码里看得见。

    模板取深圳那套（设备最全的 10 台）。新家"装修"就是把这套设备搬进去。
    播种写进库里，每台是独立记录——两个家庭绝不共享一份状态。
    """
    template = _BUILTIN_TEMPLATES[NEW_FAMILY_TEMPLATE_KEY]
    created = get_device_store().seed_family(family_id, template)
    if created:
        logger.info("family devices initialized: %s (%d 台)",
                    family_id, len(template),
                    extra={"extra_fields": {"event": "device.family_init",
                                            "family_id": family_id}})
    return created


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
        # 有范围就夹进去。用户说"空调调到 50 度"以前会照收 50，
        # 界面显示"50 度"像坏了；现在夹到 30 并如实告知上限。
        requested = float(value)
        clamped = device.clamp(requested)
        device.value = clamped
        device.power = True
        unit = device.unit or ("度" if device.device_type == "ac" else "")
        message = f"已将{device.name}设置为{device.value}{unit}"
        if clamped != requested:
            message += f"（超出可调范围，已取最接近的 {clamped}{unit}）"
    elif action == "query":
        message = f"{device.name}当前状态：{'开' if device.power else '关'}"
        if device.value is not None:
            message += f"，设定值 {device.value}"
    else:
        return {"success": False, "message": f"不支持的动作：{action}"}

    # 状态必须写回库：改的只是内存里的这个对象，不落库的话
    # 用户"关灯"之后重启服务，灯又亮着。
    get_device_store().save_state(family_id, device)

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
    # 落库。门锁走的是独立 handler，不经过 _handle_device_control 那段统一写回，
    # 漏了这一步的后果是：开锁成功、消息中心记了一笔，重启后门锁状态还是旧的。
    # 这类"两处实现同一件事、新加的逻辑只补了一处"的问题，只能靠测试兜
    # （test_lock_confirmed_unlocks 就是这条）。
    get_device_store().save_state(family_id, device)
    message = f"已{'开锁' if action == 'unlock' else '上锁'}：{device.name}"
    logger.warning("lock control: %s -> %s", device.device_id, action,
                   extra={"extra_fields": {"event": "device.lock", "action": action}})
    return {"success": True, "message": message, "device": device.to_dict()}


def get_device_states(family_id: str | None = None) -> dict[str, DeviceState]:
    """获取某个家庭的设备态（演示界面 / 控制台用）。

    注意：返回的是**从库里读出来的快照**，不是活引用（V1.3 起设备落库）。
    改状态必须走工具（device_control），不要拿这里的对象直接改——
    改了不落库，刷新就还原，而且绕开了超时/重试/熔断/幂等那一整套治理。
    """
    return devices_of(family_id)


def device_belongs_to(family_id: str, device_id: str) -> bool:
    """判断设备是否属于该家庭——跨家庭访问的断言就靠它。

    接口层必须先过这一关再调工具：拿别家的 device_id 来控制，
    要在**动设备之前**拦下来并返回 403，而不是先执行了再道歉。
    """
    return device_id in devices_of(family_id)
