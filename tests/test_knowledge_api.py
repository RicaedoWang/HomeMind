"""知识库 API 端到端（真实 JWT 认证链路，不做依赖旁路）。

验证：
- 导入（按空行切块、去重、返回 added/skipped）
- 列表
- 删除
- 租户隔离（A 家导入的，B 家查不到、也删不掉）
- 空内容 / 全重复被拒

知识库落在 conftest 隔离的内容库（`_isolated_content_store` 的 autouse fixture），
账号库落在本文件 client fixture 的临时目录——都不碰真实数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.auth import set_account_store  # noqa: E402
from homemind.api.console import reset_tasks  # noqa: E402
from homemind.api.main import app  # noqa: E402
from homemind.auth.store import DEMO_PASSWORD, AccountStore  # noqa: E402
from homemind.tools.calendar_server import reset_schedules  # noqa: E402
from homemind.tools.device_server import reset_devices  # noqa: E402


@pytest.fixture()
def client(tmp_path):
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


def _login(client: TestClient, username: str) -> dict[str, str]:
    res = client.post("/api/auth/login",
                      json={"username": username, "password": DEMO_PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_knowledge_import_list_delete(client):
    h = _login(client, "wo")

    # 初始为空
    assert client.get("/api/console/knowledge", headers=h).json()["count"] == 0

    # 导入两段（空行切块）
    r = client.post("/api/console/knowledge",
                    json={"text": "空调滤网清洗\n\n空调省电温度"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2

    # 列表
    assert client.get("/api/console/knowledge", headers=h).json()["count"] == 2

    # 删除一条
    doc_id = client.get("/api/console/knowledge", headers=h).json()["docs"][0]["id"]
    r = client.delete(f"/api/console/knowledge/{doc_id}", headers=h)
    assert r.status_code == 200, r.text
    assert client.get("/api/console/knowledge", headers=h).json()["count"] == 1


def test_knowledge_duplicate_import_rejected(client):
    h = _login(client, "wo")
    client.post("/api/console/knowledge", json={"text": "空调滤网清洗"}, headers=h)
    # 全重复 → 400（不是静默成功），前端据此提示"可能全是重复的"
    r = client.post("/api/console/knowledge", json={"text": "空调滤网清洗"}, headers=h)
    assert r.status_code == 400


def test_knowledge_blank_import_rejected(client):
    h = _login(client, "wo")
    r = client.post("/api/console/knowledge", json={"text": "   \n\n  "}, headers=h)
    assert r.status_code == 400


def test_knowledge_tenant_isolation(client):
    h_wo = _login(client, "wo")
    h_xm = _login(client, "xiaoming")

    client.post("/api/console/knowledge", json={"text": "空调滤网清洗"}, headers=h_wo)

    # 小明（青岛）看不到深圳家的知识库
    assert client.get("/api/console/knowledge", headers=h_xm).json()["count"] == 0

    # 且拿着深圳家的 doc_id 也删不掉 → 404
    wo_id = client.get("/api/console/knowledge", headers=h_wo).json()["docs"][0]["id"]
    r = client.delete(f"/api/console/knowledge/{wo_id}", headers=h_xm)
    assert r.status_code == 404
