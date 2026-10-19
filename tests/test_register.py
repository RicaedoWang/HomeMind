"""注册与家庭（租户）创建测试。

覆盖两条真实路径：
    test_register_creates_family        → 注册即建家，注册者是户主 L4
    test_register_initializes_devices   → 新家必须有设备，否则登进去是个空控制台
    test_register_then_login            → 注册完能立刻登录（同一套密码哈希）
    test_join_by_invite                 → 凭邀请码成为 L3 成员，和户主同一个家
    test_invite_is_case_insensitive     → 念给家人手打的码，大小写不该计较
    test_duplicate_username_409         → 重名要 409，前端据此把光标停回用户名框
    test_weak_password_rejected         → 弱密码 400
    test_bad_invite_rejected            → 假邀请码 400，且不泄露"这个码不存在还是过期"
    test_invite_code_only_owner         → 邀请码是钥匙，普通成员看不了（403）
    test_new_family_is_isolated         → 新家的设备状态与深圳家互不影响
    test_new_family_has_default_scenes  → 新家也不是空的，有默认场景可用

最后一条是最容易被忘的：注册完功能"能用"但整个家空荡荡，
这种产品用户第二天就不打开了。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from homemind.api.auth import set_account_store
from homemind.api.console import reset_tasks
from homemind.api.main import app
from homemind.auth.store import AccountStore
from homemind.tools.calendar_server import reset_schedules
from homemind.tools.device_server import devices_of, reset_devices

NEW_FAMILY = {
    "username": "zhangsan",
    "password": "pass1234",
    "name": "张三",
    "mode": "create",
    "family_name": "杭州的家",
    "city": "杭州",
}


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    """状态归零 + 账号库换临时文件。

    账号库必须换：注册是**写**操作，走默认路径就是对
    `data/homemind_accounts.db` 动手，用户真实账号表里会多出一堆测试用户。
    """
    reset_devices()
    reset_schedules()
    reset_tasks()
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    yield
    reset_devices()
    reset_schedules()
    reset_tasks()
    set_account_store(None)


@pytest.fixture()
def client(tmp_path):
    """不旁路认证的客户端——注册返回的 token 就是真 token，直接拿来用。"""
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------- 创建家庭 ----------------

def test_register_creates_family(client):
    """注册即建家，注册者是户主（L4），并直接拿到可用的 token。"""
    resp = client.post("/api/auth/register", json=NEW_FAMILY)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    user = data["user"]
    assert user["role"] == "L4", "建家的人是户主，不是普通成员"
    assert user["member_id"] == "member_001", "新家的第一个成员编号从 001 开始"
    assert user["family"]["name"] == "杭州的家"
    assert user["family"]["builtin"] is False, "注册建的必须是自定义家庭，不是内置的"
    assert data["token"], "注册完应直接登录，不该让用户再输一次密码"


def test_register_initializes_devices(client):
    """新家要有设备。空控制台不叫"新家"，叫"坏了"。"""
    resp = client.post("/api/auth/register", json=NEW_FAMILY)
    family_id = resp.json()["user"]["family"]["family_id"]

    devices = devices_of(family_id)
    assert len(devices) == len(devices_of("family_sz")), "新家按模板初始化整套设备"
    assert devices["living_room_light"].name == "客厅灯"


def test_register_then_login(client):
    """注册写入的就是正常账号，走同一套 PBKDF2 校验能登进来。"""
    client.post("/api/auth/register", json=NEW_FAMILY)

    resp = client.post("/api/auth/login",
                       json={"username": "zhangsan", "password": "pass1234"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["name"] == "张三"


def test_new_family_has_default_scenes(client):
    """新家有默认场景（回家/离家/…），不是一片空白。"""
    resp = client.post("/api/auth/register", json=NEW_FAMILY)
    token = resp.json()["token"]

    state = client.get("/api/console/state", headers=_auth(token)).json()

    names = [s["name"] for s in state["scenes"]]
    assert "回家" in names and "睡眠" in names
    assert all(s["builtin"] for s in state["scenes"]), "默认场景是内置的，只读"


def test_new_family_is_isolated(client):
    """新家的设备是独立实体：开新家的灯，深圳家的灯状态纹丝不动。

    断言方式刻意是"记初值再比对"，而不是写死 True/False——
    设备初始状态属于种子数据，随时可能调（可调档设备必须带 value，
    不然卡面显示"已开 0%"）。测试该守的是"没被别的家改过"，
    不是"初始值必须是某个具体值"。
    """
    sz_before = devices_of("family_sz")["living_room_light"].power

    resp = client.post("/api/auth/register", json=NEW_FAMILY)
    token = resp.json()["token"]
    family_id = resp.json()["user"]["family"]["family_id"]

    client.post("/api/console/devices/living_room_light/control",
                json={"action": "turn_on"}, headers=_auth(token))

    assert devices_of(family_id)["living_room_light"].power is True, "新家的灯没打开"
    assert devices_of("family_sz")["living_room_light"].power is sz_before, \
        "开新家的灯顺手把深圳家的灯也开了——租户隔离漏到了设备对象层"


def test_new_member_shows_in_family_list(client):
    """新注册的成员要出现在家庭成员列表里（以前这份列表是写死的）。"""
    resp = client.post("/api/auth/register", json=NEW_FAMILY)
    token = resp.json()["token"]

    state = client.get("/api/console/state", headers=_auth(token)).json()

    names = [m["name"] for m in state["family"]]
    assert names == ["张三"], "一个人建的家，成员列表就他一个"


# ---------------- 邀请码加入 ----------------

def test_join_by_invite(client):
    """凭邀请码加入：同一个家、L3 身份、成员编号递增。"""
    created = client.post("/api/auth/register", json=NEW_FAMILY).json()
    family_id = created["user"]["family"]["family_id"]

    invite = client.get("/api/auth/family/invite",
                        headers=_auth(created["token"])).json()["inviteCode"]

    resp = client.post("/api/auth/register", json={
        "username": "lisi", "password": "pass1234", "name": "李四",
        "mode": "join", "invite_code": invite,
    })

    assert resp.status_code == 200, resp.text
    user = resp.json()["user"]
    assert user["family"]["family_id"] == family_id, "加入的必须是同一个家"
    assert user["role"] == "L3", "后来者是普通成员，不是第二个户主"
    assert user["member_id"] == "member_002", "成员编号递增，不跟户主撞号"


def test_invite_is_case_insensitive(client):
    """邀请码是念给人听、手打进手机的，大小写不该计较。"""
    created = client.post("/api/auth/register", json=NEW_FAMILY).json()
    invite = client.get("/api/auth/family/invite",
                        headers=_auth(created["token"])).json()["inviteCode"]

    resp = client.post("/api/auth/register", json={
        "username": "lisi", "password": "pass1234", "name": "李四",
        "mode": "join", "invite_code": invite.lower(),
    })

    assert resp.status_code == 200, "小写输入的邀请码被拒了"


def test_invite_code_only_owner(client):
    """邀请码只有户主能看。普通成员随手一瞥就能把邻居拉进来，那权限就是摆设。"""
    created = client.post("/api/auth/register", json=NEW_FAMILY).json()
    invite = client.get("/api/auth/family/invite",
                        headers=_auth(created["token"])).json()["inviteCode"]
    member = client.post("/api/auth/register", json={
        "username": "lisi", "password": "pass1234", "name": "李四",
        "mode": "join", "invite_code": invite,
    }).json()

    resp = client.get("/api/auth/family/invite", headers=_auth(member["token"]))

    assert resp.status_code == 403
    assert "户主" in resp.json()["detail"]


# ---------------- 校验 ----------------

def test_duplicate_username_409(client):
    """重名必须 409（而不是 400）：这是唯一"换一个就能成"的错误，
    前端据状态码把光标停回用户名输入框，而不是弹一句通用失败。"""
    client.post("/api/auth/register", json=NEW_FAMILY)

    resp = client.post("/api/auth/register", json=NEW_FAMILY)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "这个用户名已经被用了"


def test_weak_password_rejected(client):
    resp = client.post("/api/auth/register",
                       json={**NEW_FAMILY, "password": "123"})

    assert resp.status_code == 400
    assert "6 位" in resp.json()["detail"]


def test_bad_invite_rejected(client):
    resp = client.post("/api/auth/register", json={
        "username": "lisi", "password": "pass1234", "name": "李四",
        "mode": "join", "invite_code": "NOSUCH",
    })

    assert resp.status_code == 400
    assert "邀请码" in resp.json()["detail"]


def test_bad_username_rejected(client):
    """用户名限制成字母数字下划线：中文用户名在日志、URL、库里到处是坑。"""
    resp = client.post("/api/auth/register",
                       json={**NEW_FAMILY, "username": "张三"})

    assert resp.status_code == 400
    assert "用户名" in resp.json()["detail"]


def test_duplicate_username_creates_no_orphan_family(client):
    """重名时不能留下一个没有任何成员的"空家"。

    这是个真实的数据卫生问题：先建家再插账号，一旦用户名撞了，
    库里就多一个永远没人住的家，而且没有任何入口能删掉它。
    """
    from homemind.auth.family_store import get_family_store

    before = len(get_family_store().list_all())
    client.post("/api/auth/register", json=NEW_FAMILY)
    client.post("/api/auth/register", json=NEW_FAMILY)   # 第二次必然重名

    after = len(get_family_store().list_all())
    assert after - before == 1, f"只该新增一个家，实际多了 {after - before} 个"
