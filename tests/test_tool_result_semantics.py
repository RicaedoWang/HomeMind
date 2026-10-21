"""工具结果语义的统一回归（V1.4）。

## 病灶：`ok=True` 配失败

`ToolResult.ok` 只表示"注册中心成功拿到了一个返回值"。
设备 handler 遇到"设备不存在""动作不支持"时**不抛异常**，
它老实返回 `{"success": False, "message": "没有找到设备"}`——
调用当然算成功，`ok` 就是 True。实测：

    device_control 不存在的设备 → ok=True  success=False
    device_control 不支持的动作 → ok=True  success=False

只看 `ok` 的下场：界面显示"已打开"，消息中心记一条"打开了客厅灯"，灯一动不动。

这个坑踩过三次（单设备控制、场景批量执行、任务联动设备），
每次都是"只改当前那一处"，换个入口又冒出来。
所以收成 `registry.tool_succeeded()` 一处定义，谁要用谁调。

## 顺藤摸到的更严重问题：门锁的"反向操作"

`complete_task` 里原来用字面量判断是不是门锁：

    action = "turn_off" if device_id == "front_door_lock" else "turn_on"
    ... "action": "lock" if device_id == "front_door_lock" else action

V1.3 允许用户自建门锁之后，自建锁的 id 不叫 `front_door_lock`，
于是落到 else 分支，给 `lock_control` 传 `turn_on`。
而 handler 里是 `device.power = (action == "unlock")`——
**任何不是 unlock 的动作都被当成上锁**，还返回 success=True。

用户完成一个"出门记得锁门"的任务，期望联动开锁、实际把门锁上，
全程零报错。反向操作比"没动"难查得多：设备状态变了，看着像"正常执行"。

修法两条腿：
1. 判断"是不是门锁"看 `device.device_type`，不看 id 字面量——id 是数据，类型才是语义；
2. `lock_control` 对非法动作直接返回失败，不再静默吞掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_pending, reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.core.models import ToolResult  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import devices_of, reset_devices  # noqa: E402
from homemind.tools.device_store import get_device_store  # noqa: E402
from homemind.tools.registry import default_registry, tool_succeeded  # noqa: E402

CONSOLE_SRC = SRC / "homemind" / "api" / "console.py"


@pytest.fixture(autouse=True)
def _clean():
    reset_devices()
    reset_schedules()
    reset_tasks()
    reset_pending()
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()
    reset_pending()


@pytest.fixture()
def client(tmp_path):
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    app.dependency_overrides[current_user] = lambda: AuthUser(
        family_id="family_sz", member_id="member_001", role="L4", name="我", city="深圳",
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)


# ---------------------------------------------------------------- 判定函数本身

def test_tool_succeeded_rejects_business_failure():
    """核心语义：调用成功 ≠ 操作生效。"""
    failed_but_called = ToolResult(
        tool_name="device_control", ok=True,
        data={"success": False, "message": "没有找到设备：咖啡机"},
    )
    assert failed_but_called.ok is True, "前提：注册中心确实认为调用成功"
    assert tool_succeeded(failed_but_called) is False, (
        "tool_succeeded 把业务失败当成了成功——界面会显示'已打开'而设备没动"
    )


def test_tool_succeeded_accepts_real_success():
    assert tool_succeeded(ToolResult(tool_name="x", ok=True, data={"success": True}))


def test_tool_succeeded_handles_transport_failure():
    assert tool_succeeded(ToolResult(tool_name="x", ok=False, error="timeout")) is False
    assert tool_succeeded(None) is False


def test_tool_succeeded_defaults_true_without_flag():
    """没有 success 字段的老工具按成功算，保持向后兼容。

    收紧默认值会让没实现这个字段的工具静默变"失败"——那是另一种坑。
    """
    assert tool_succeeded(ToolResult(tool_name="legacy", ok=True, data={})) is True


# ---------------------------------------------------------------- 调用点

def test_device_control_with_unknown_action_reports_failure(client: TestClient):
    """给设备塞一个不支持的动作，接口必须回 ok:false。

    以前这里回 ok:true，前端据此刷新，用户看到"操作成功"但灯没动。
    """
    body = client.post(
        "/api/console/devices/living_room_light/control",
        json={"action": "turn_sideways"},
    ).json()
    assert body.get("ok") is False, f"不支持的动作被当成成功：{body}"
    assert "不支持" in body.get("message", ""), f"没告诉用户为什么不支持：{body}"


def test_lock_control_rejects_non_lock_action(client: TestClient):
    """门锁只认 lock / unlock，其他动作必须明确失败。

    以前 `power = (action == "unlock")`：任何非 unlock 的动作都被
    静默当成"上锁"并返回成功。上游一旦传错动作，等于悄悄把门锁了。
    """
    lock = devices_of("family_sz")["front_door_lock"]
    before = lock.power
    result = __import__("asyncio").run(
        default_registry().call(
            "lock_control",
            {"action": "turn_on", "device_name": "大门门锁", "family_id": "family_sz"},
        )
    )
    assert result.data.get("success") is False, (
        f"门锁接受了非法动作且报成功：{result.data}"
    )
    assert tool_succeeded(result) is False
    after = devices_of("family_sz")["front_door_lock"].power
    assert after == before, "非法动作居然动了门锁状态"


def test_lock_control_accepts_valid_actions(client: TestClient):
    """对照：合法动作照常工作，别把闸门关死。"""
    result = __import__("asyncio").run(
        default_registry().call(
            "lock_control",
            {"action": "unlock", "device_name": "大门门锁", "family_id": "family_sz"},
        )
    )
    assert tool_succeeded(result) is True
    assert devices_of("family_sz")["front_door_lock"].power is True


def test_custom_lock_linked_task_uses_type_not_id(client: TestClient):
    """自建门锁挂到任务上，联动必须走 lock 路径，不能靠 handler 兜底。

    守的是 `complete_task` 里那两行字面量判断。改成看 `device_type` 之后，
    自建锁和内置锁走同一条 lock 路径——不再依赖 `lock_control` 把
    turn_on 静默解释成"上锁"。

    这条能抓住回归，是因为 `lock_control` 现在会**拒绝** turn_on：
    一旦有人把判断改回 `device_id == "front_door_lock"`，自建锁就会收到
    turn_on → 返回 success=False → 这里的 `ok` 断言立刻红。
    """
    from homemind.api import console as console_mod

    # 自建一把锁：id 不是 front_door_lock，正是原来会漏判的形状。
    # 字段名是 device_type（snake_case），和前端保持一致——传 deviceType 会被
    # pydantic 当成未知字段静默忽略，设备会静默变成 "extra"，
    # 然后这条测试就变成"验了个不是锁的东西"，白测。
    client.post("/api/console/devices", json={
        "name": "后门智能锁", "room": "玄关", "device_type": "lock",
    })
    custom = next(d for d in devices_of("family_sz").values() if d.name == "后门智能锁")
    assert custom.device_id != "front_door_lock", "前提：自建锁的 id 和内置的不同"
    assert custom.device_type == "lock", f"前提没成立：设备类型是 {custom.device_type}"

    # 把"确认入户门锁已反锁"这条种子任务改成联动自建锁
    task = next(t for t in console_mod._TASKS["family_sz"] if t["id"] == "t-security")
    task["linkedDeviceIds"] = [custom.device_id]

    body = client.post("/api/console/tasks/t-security/complete", json={}).json()
    assert body.get("ok") is True
    linked = body.get("linked", [])
    assert len(linked) == 1, f"联动设备数不对：{linked}"
    assert linked[0]["ok"] is True, (
        f"自建门锁的联动被判失败——很可能又按 device_id 字面量分支了：{linked[0]}"
    )
    # 任务联动的语义是"上锁"，自建锁也必须真的被锁上，不能只是报成功
    after = devices_of("family_sz")[custom.device_id]
    assert after.power is False, "报成功但门锁状态没变——这就是 ok 撒谎"


def test_builtin_lock_linked_task_still_works(client: TestClient):
    """对照：内置门锁的联动没被改坏。"""
    body = client.post("/api/console/tasks/t-security/complete", json={}).json()
    linked = body.get("linked", [])
    assert linked and linked[0]["ok"] is True, f"内置门锁联动坏了：{linked}"
    assert devices_of("family_sz")["front_door_lock"].power is False


def test_no_raw_ok_judgement_left_in_console():
    """白盒守门：console.py 里不许再出现手写的 ok 判定。

    这个坑改了三次、每次只改一处，所以用一条用例把入口锁死：
    新增调用点时如果再手写 `"ok": result.ok`，这里立刻红。
    """
    src = Path(CONSOLE_SRC).read_text(encoding="utf-8")
    assert '"ok": result.ok' not in src, (
        "又有人手写 result.ok 当结果了——请改用 registry.tool_succeeded()"
    )
    assert 'result.ok and (result.data or {})' not in src, (
        "手写的成功判定残留，请改用 registry.tool_succeeded()"
    )


def test_no_literal_device_id_as_semantics():
    """白盒守门：不许拿 device_id 字面量当"这算不算门锁"的判据。

    id 是用户数据（可自建、可改名），device_type 才是语义。
    """
    src = Path(CONSOLE_SRC).read_text(encoding="utf-8")
    offenders = []
    for line in src.splitlines():
        stripped = line.strip()
        # 跳过注释：注释里引用旧写法是为了解释为什么不能这么写，
        # 把它当违规抓出来属于"抓错人"，测试也就变成了噪音。
        if stripped.startswith("#"):
            continue
        if 'device_id == "front_door_lock"' in stripped:
            offenders.append(stripped)
    assert not offenders, f"仍在用字面量判断设备类型：{offenders}"
