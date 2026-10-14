"""登录认证回归测试（方案 S1）。

每条都对应一条安全契约，不是凑数：
    test_login_returns_family_scoped_identity   登录带出家庭/成员/角色/城市
    test_wrong_password_rejected                错误密码 401
    test_unknown_user_same_error_message        账号不存在与密码错误返回同一句（防账号枚举）
    test_protected_route_requires_token         无 token 访问控制台 → 401
    test_tampered_token_rejected                篡改签名 → 401
    test_expired_token_rejected                 过期 token → 401
    test_password_not_stored_in_plaintext       库里没有明文密码，但能校验
    test_identity_comes_from_token_not_body     请求体伪造 member_id 无效（最要紧的一条）
    test_rate_limit_after_repeated_failures     连续失败触发限流
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.passwords import verify_password  # noqa: E402
from homemind.auth.store import DEMO_PASSWORD, AccountStore  # noqa: E402
from homemind.auth.tokens import AuthUser, decode_token, issue_token  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402

from homemind.api.console import reset_tasks  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """真实认证链路（不做依赖旁路），账号库落在临时目录，不碰真实数据。"""
    reset_devices()
    reset_schedules()
    reset_tasks()
    set_account_store(AccountStore(db_path=str(tmp_path / "accounts.db")))
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_account_store(None)
    reset_devices()
    reset_schedules()
    reset_tasks()


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_returns_family_scoped_identity(client: TestClient) -> None:
    res = login(client, "wo", DEMO_PASSWORD)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tokenType"] == "Bearer"
    assert body["expiresIn"] > 0
    assert body["user"]["family"]["family_id"] == "family_sz"
    assert body["user"]["role"] == "L4"
    assert body["user"]["city"] == "深圳"

    # token 里必须真的带着家庭与成员，业务层才可能做隔离
    user = decode_token(body["token"])
    assert user.family_id == "family_sz"
    assert user.member_id == "member_001"


def test_qingdao_family_is_a_different_tenant(client: TestClient) -> None:
    res = login(client, "xiaoming", DEMO_PASSWORD)
    assert res.status_code == 200
    user = decode_token(res.json()["token"])
    assert user.family_id == "family_qd"
    assert user.city == "青岛"


def test_wrong_password_rejected(client: TestClient) -> None:
    res = login(client, "wo", "wrong-password")
    assert res.status_code == 401


def test_unknown_user_same_error_message(client: TestClient) -> None:
    """账号不存在与密码错误必须返回同一句话，否则等于帮人枚举账号。"""
    a = login(client, "wo", "wrong-password")
    b = login(client, "nobody_here", "wrong-password")
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"] == "用户名或密码错误"


def test_protected_route_requires_token(client: TestClient) -> None:
    assert client.get("/api/console/state").status_code == 401


def test_tampered_token_rejected(client: TestClient) -> None:
    token = login(client, "wo", DEMO_PASSWORD).json()["token"]
    tampered = token[:-4] + "abcd"
    res = client.get("/api/console/state", headers={"Authorization": f"Bearer {tampered}"})
    assert res.status_code == 401
    assert res.json()["detail"] == "登录态无效或已过期"


def test_expired_token_rejected(client: TestClient) -> None:
    user = AuthUser("family_sz", "member_001", "L4", "我", "深圳")
    expired, _ = issue_token(user, ttl_hours=-1)   # 签发一个已经过期的 token
    res = client.get("/api/console/state", headers={"Authorization": f"Bearer {expired}"})
    assert res.status_code == 401


def test_password_not_stored_in_plaintext(tmp_path, client: TestClient) -> None:
    from homemind.api.auth import get_account_store

    stored = get_account_store().get_password_hash("wo")
    assert stored is not None
    assert DEMO_PASSWORD not in stored          # 库里绝不能出现明文
    assert verify_password(DEMO_PASSWORD, stored)   # 但能正常校验
    assert not verify_password("wrong", stored)


def test_identity_comes_from_token_not_body(client: TestClient) -> None:
    """最要紧的一条：请求体里伪造 member_id 必须无效。

    以前 member_id 是调用方自己填的，等于谁都能伪造成别人去操作门锁。
    现在一律以 token 为准。
    """
    token = login(client, "baba", DEMO_PASSWORD).json()["token"]   # 爸爸 = member_003
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post("/api/console/tasks/t-pet/complete",
                      json={"member_id": "member_001"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["task"]["completedBy"] == "member_003", "完成人必须是登录者本人"


def test_rate_limit_after_repeated_failures(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "mama", "nope").status_code == 401
    # 第 6 次即使密码正确也被限流挡住（防爆破）
    assert login(client, "mama", DEMO_PASSWORD).status_code == 401


def test_logout_and_me(client: TestClient) -> None:
    token = login(client, "wo", DEMO_PASSWORD).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user"]["member_id"] == "member_001"
    assert client.post("/api/auth/logout", headers=headers).json()["ok"] is True
