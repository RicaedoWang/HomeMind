"""设备匹配的歧义处理与房间别名回归（V1.4）。

## 病灶一：不说房间时**静默替用户挑一台**

    _find_device('灯', None, fid) → 客厅灯（候选三台，取第一个，零提示）

用户说"打开灯"，客厅灯亮了。灯确实亮了，但不是他要的那盏，
而且系统没有任何日志痕迹说明它凭什么挑的客厅灯。
这种"猜对了没人夸、猜错了没人报"的行为最难查——用户只会觉得系统抽风。

修法：`_resolve_devices` 返回**所有**候选，多个时明确反问：
「客厅灯、主卧灯都对得上，你说的是哪一台？」
代价是多一句话，换来用户能立刻纠正。

## 病灶二：房间名两套词

    _find_device('灯', '主卧', fid) → 主卧灯 ✓
    _find_device('灯', '卧室', fid) → 找不到

用户说"卧室"，设备库里那间房叫"主卧"。这不是用户的问题，是两套词。
别名在**抽出 room 之后**归一化，而不是在匹配函数里补救——
room 归成"主卧"以后，下游按房间精确匹配立刻就对，一行都不用改。

## 一个刻意的"不做"

不做模糊相似度匹配。"卧室灯"和"主卧灯"字符重合 2/3，
但一旦按重合度配，就会开始乱配（"客厅灯"和"客厅窗帘"也有重合）。
**宁可说听不懂，也不要替用户按下开关。**
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.agent.intent import ROOM_ALIAS, IntentParser  # noqa: E402
from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_pending, reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.deps import current_user  # noqa: E402
from homemind.auth.store import AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import (  # noqa: E402
    _handle_device_control, _handle_lock_control, _resolve_devices, reset_devices,
)


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


def _entities(text: str, family: str = "family_sz") -> dict:
    return asyncio.run(IntentParser().parse(text, family))[0].entities


def _names(candidates: list) -> list[str]:
    return [c.name for c in candidates]


# ---------------------------------------------------------------- 房间别名

def test_bedroom_alias_resolves_to_primary_bedroom():
    """「卧室灯」必须落到主卧灯，不能回一句"没有找到设备"。"""
    entities = _entities("打开卧室灯")
    assert entities.get("room") == "主卧", (
        f"「卧室」没归一到「主卧」，抽到的是 {entities.get('room')!r}"
    )
    matched = _names(_resolve_devices(entities.get("device_name"),
                                      entities.get("room"), "family_sz"))
    assert matched == ["主卧灯"], f"匹配结果不对：{matched}"


def test_alias_applies_to_multiple_phrasings():
    for text in ("关掉卧室的灯", "把睡房的灯打开", "卧室灯调到 30"):
        entities = _entities(text)
        assert entities.get("room") == "主卧", f"{text!r} 抽到 room={entities.get('room')!r}"


def test_user_defined_room_wins_over_alias(client: TestClient):
    """用户真建了一间叫「卧室」的房，就必须按「卧室」走。

    别名是为了弥合"用户口语"和"系统房间名"的差距，
    不是用来劫持用户自建房间的。这个边界一旦搞错，
    用户会发现自己建的房间永远点不到。
    """
    client.post("/api/console/rooms", json={"name": "卧室"})
    client.post("/api/console/devices", json={
        "name": "卧室吊灯", "room": "卧室", "device_type": "light",
        "min_value": 0, "max_value": 100, "unit": "%",
    })
    entities = _entities("打开卧室吊灯")
    assert entities.get("room") == "卧室", (
        f"用户自建的「卧室」被别名抢走了，抽到 {entities.get('room')!r}"
    )


def test_alias_table_covers_alias_targets():
    """别名表里的目标房间必须是真实存在的内置房间名，不能拼错。"""
    from homemind.agent.intent import ROOMS
    for alias, target in ROOM_ALIAS.items():
        assert target in ROOMS, f"别名 {alias}→{target} 指向了不存在的房间"
        assert alias != target


# ---------------------------------------------------------------- 歧义反问

def test_ambiguous_device_asks_instead_of_guessing():
    """不说房间就选一台 → 反问。

    这条守的是原来 `return candidates[0]` 那行。改回去就会变成
    默默开客厅灯，而这里断言的是 success=False + 反问文案。
    """
    result = asyncio.run(_handle_device_control(
        {"action": "turn_on", "device_name": "灯", "family_id": "family_sz"}
    ))
    assert result.get("success") is False, f"多候选仍然自作主张执行了：{result}"
    assert result.get("ambiguous") is True
    message = result.get("message", "")
    assert "哪一台" in message, f"反问文案不对：{message!r}"
    # 候选必须报全，只报一台等于换个方式猜
    assert "客厅灯" in message and "主卧灯" in message, f"候选没报全：{message!r}"


def test_ambiguous_does_not_touch_any_device():
    """反问的同时不能顺手把某台设备动一下。"""
    from homemind.tools.device_server import devices_of
    before = {d.name: d.power for d in devices_of("family_sz").values()}
    asyncio.run(_handle_device_control(
        {"action": "turn_on", "device_name": "灯", "family_id": "family_sz"}
    ))
    after = {d.name: d.power for d in devices_of("family_sz").values()}
    assert before == after, f"反问过程中动了设备：{before} → {after}"


def test_unique_candidate_executes_normally():
    """对照：能确定唯一一台时照常执行，别把闸门关死。"""
    result = asyncio.run(_handle_device_control(
        {"action": "turn_on", "device_name": "主卧灯", "family_id": "family_sz"}
    ))
    assert result.get("success") is True
    from homemind.tools.device_server import devices_of
    assert devices_of("family_sz")["bedroom_light"].power is True


def test_room_qualifier_disambiguates():
    """带上房间就不再是歧义：候选被房间过滤到只剩一台。"""
    candidates = _resolve_devices("灯", "客厅", "family_sz")
    assert _names(candidates) == ["客厅灯"]


def test_exact_name_beats_substring():
    """精确匹配优先：说"主卧灯"就只返回主卧灯，不要再把别的灯捞进来。"""
    candidates = _resolve_devices("主卧灯", None, "family_sz")
    assert _names(candidates) == ["主卧灯"]


def test_no_match_returns_empty():
    candidates = _resolve_devices("灯", "阳台", "family_sz")
    assert candidates == [], f"阳台上没有灯，却匹配到了 {_names(candidates)}"


def test_ambiguous_lock_also_asks(client: TestClient):
    """高危设备更不许猜：家里两把锁时，反问而不是挑一把开。"""
    client.post("/api/console/devices", json={
        "name": "后门门锁", "room": "玄关", "device_type": "lock",
    })
    result = asyncio.run(_handle_lock_control(
        {"action": "unlock", "device_name": "门锁", "family_id": "family_sz"}
    ))
    assert result.get("success") is False, f"两把锁时竟然直接开了一把：{result}"
    assert "哪一把" in result.get("message", ""), result.get("message")


# ---------------------------------------------------------------- 走完整链路

def test_ai_asks_user_which_lamp(client: TestClient):
    """端到端：用户对 AI 说「打开灯」，AI 要反问而不是闷头开一盏。"""
    answer = client.post(
        "/api/console/assistant", json={"text": "打开灯", "confirmed": False}
    ).json().get("answer", "")
    assert "哪一台" in answer, f"AI 没有反问，它直接动手了或答非所问：{answer!r}"
    from homemind.tools.device_server import devices_of
    assert devices_of("family_sz")["living_room_light"].power is False, (
        "AI 在反问的同时把客厅灯打开了"
    )


def test_ai_handles_bedroom_lamp(client: TestClient):
    """端到端：「打开卧室灯」要开到主卧那盏。"""
    client.post(
        "/api/console/assistant", json={"text": "打开卧室灯", "confirmed": False}
    )
    from homemind.tools.device_server import devices_of
    devices = devices_of("family_sz")
    assert devices["bedroom_light"].power is True, "卧室灯没打开"
    assert devices["living_room_light"].power is False, "开错了屋——客厅灯被打开了"
