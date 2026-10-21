"""三项交互契约的端到端验收（真起服务，走 HTTP + JWT）。

和 pytest 用例的区别（两者都要有，不是重复）：
`tests/test_console_events.py` 用 dependency_overrides **旁路认证**，
所以每条用例只测一件事、结果确定。但"登录 → 拿 token → 用 token 操作"
这条完整链路它一次都没真跑过——JWT 中间件、静态站点托管、
跨账号切换都在旁路时被跳过了。这个脚本就是把它们串起来真跑一遍。

跑法：
    python scripts/verify_interactions.py          # mock 模型（默认，确定性）
    python scripts/verify_interactions.py --real   # 真模型（会烧 token）
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 必须在导入 homemind 之前设：config 的 load_dotenv(override=False) 不覆盖已有环境变量
import os  # noqa: E402

if "--real" not in sys.argv:
    os.environ["HM_USE_MOCK_LLM"] = "true"
# 验收要看的是结论，不是服务端的 trace 日志；--verbose 时才打开
if "--verbose" not in sys.argv:
    os.environ["HM_LOG_LEVEL"] = "ERROR"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from homemind.api.main import app  # noqa: E402

MODE = "真模型" if "--real" in sys.argv else "mock 模型"

# 验收会**注册真实账号、建真实家庭**，这些都会落 SQLite。不隔离的话每跑一次
# 就往用户的账号表里塞两个测试用户，跑十次就是二十个——和"复现时污染记忆库"
# 是同一类事故，只是这次脏的是账号表。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="hm-verify-"))


def isolate_stores() -> None:
    """把账号库、家庭库、场景库、设备库都换成临时文件（必须在起服务之前调用）。

    这里有个踩过的坑：V1.3 新加了 device store，`tests/conftest.py` 那边
    同步加了隔离 fixture，但这个脚本的名单忘了跟着加——于是每次跑验收，
    测试注册的那个家庭就把"厨房""影音厅"两间自建房写进**用户的真实库**。
    教训：**新增可写存储不只要改 conftest，凡是"换库"的名单都得一起改**，
    落单的那一份就是数据泄漏的口子。
    """
    from homemind.api.auth import set_account_store
    from homemind.auth.family_store import (
        FamilyStore, SceneStore, set_family_store, set_scene_store,
    )
    from homemind.auth.store import AccountStore
    from homemind.tools.device_store import DeviceStore, set_device_store

    set_account_store(AccountStore(db_path=str(_TMP_DIR / "accounts.db")))
    set_family_store(FamilyStore(str(_TMP_DIR / "content.db")))
    set_scene_store(SceneStore(str(_TMP_DIR / "content.db")))
    set_device_store(DeviceStore(str(_TMP_DIR / "content.db")))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class Client:
    """带身份的 HTTP 客户端：token 从真实登录接口拿，不是伪造的。"""

    def __init__(self, base: str) -> None:
        self._c = httpx.Client(base_url=base, timeout=60.0)
        self.token: str | None = None
        self.name = ""

    def login(self, username: str, password: str = "demo1234") -> bool:
        res = self._c.post("/api/auth/login", json={"username": username, "password": password})
        if res.status_code != 200:
            return False
        data = res.json()
        self.token = data["token"]
        self.name = data["user"]["name"]
        self._c.headers["Authorization"] = f"Bearer {self.token}"
        return True

    def get(self, path: str) -> httpx.Response:
        return self._c.get(path)

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self._c.post(path, json=body or {})

    def put(self, path: str, body: dict | None = None) -> httpx.Response:
        return self._c.put(path, json=body or {})

    def delete(self, path: str) -> httpx.Response:
        return self._c.delete(path)

    def state(self) -> dict:
        return self.get("/api/console/state").json()


def titles(messages: list[dict]) -> list[str]:
    return [m["title"] for m in messages]


def main() -> int:
    isolate_stores()      # 必须在任何请求打到服务之前换库
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    print("=" * 72)
    print(f"HomeMind 三项交互契约 · 端到端验收（{MODE}）")
    print(f"服务地址：{base}")
    print("=" * 72)

    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str) -> None:
        checks.append((label, ok, detail))
        print(f"  {'✅' if ok else '❌'} {label:<28} {detail}")

    try:
        # 静态站点必须能被同一条命令托管
        home = httpx.get(base + "/", timeout=10.0)
        check("静态站点托管", home.status_code == 200 and "HomeMind" in home.text,
              f"GET / → {home.status_code}")

        me = Client(base)
        xm = Client(base)

        # ---- 需求 1：消息中心是真实事件流（不再是写死的种子文案）
        assert me.login("wo"), "登录失败"
        assert xm.login("xiaoming"), "小明登录失败"

        st = me.state()
        check("登录本身就是一条真事件",
              any("登录了控制台" in t for t in titles(st["messages"])),
              f"我：{titles(st['messages'])[:1]}")

        me.post("/api/console/devices/living_room_light/control",
                {"action": "turn_on", "value": 60})
        st = me.state()
        check("开灯 → 消息里真的多一条",
              any("打开了客厅灯" in t for t in titles(st["messages"])),
              f"{titles(st['messages'])[0]}")

        me.post("/api/console/scenes/home/activate", {})
        st = me.state()
        check("执行场景 → 写清联动设备",
              any(m["type"] == "scene" and "客厅灯" in m["body"] for m in st["messages"]),
              next((m["title"] for m in st["messages"] if m["type"] == "scene"), "无"))

        me.post("/api/console/tasks/t-plant/complete", {})
        st = me.state()
        check("完成任务 → 留下完成记录",
              any(m["type"] == "task" and "完成了" in m["title"] for m in st["messages"]),
              next((m["title"] for m in st["messages"] if m["type"] == "task"), "无"))

        qd = xm.state()
        check("别家的消息一条都看不到",
              qd["messages"] != [] and all("客厅灯" not in t for t in titles(qd["messages"])),
              f"小明：{titles(qd['messages'])}")

        # ---- 需求 2：跟 AI 加的提醒要进今日任务框
        # 提醒走"回显确认"（FR-SEC 防误创建）：第一轮只回显，第二轮带 confirmed 才真建。
        # 前端对应动作卡上的「确认执行」按钮，所以这里也要走完整两轮，不能只发一次。
        before = {t["id"] for t in me.state()["tasks"]}
        first = me.post("/api/console/assistant", {"text": "提醒我晚上九点关灯"}).json()
        check("提醒先回显、不直接创建",
              first.get("requiresConfirm") is True
              and not any(t["id"] not in before and t.get("source") == "ai"
                          for t in me.state()["tasks"]),
              (first.get("answer") or "")[:36])

        res = me.post("/api/console/assistant",
                      {"text": "提醒我晚上九点关灯", "confirmed": True})
        answer = res.json().get("answer", "")
        st = me.state()
        new_task = next((t for t in st["tasks"]
                         if t["id"] not in before and t.get("source") == "ai"), None)
        check("AI 加的提醒进了今日任务", new_task is not None,
              f"{new_task['title']} · 来源 {new_task.get('source')}" if new_task
              else f"AI 答复：{answer[:40]}")

        if new_task:
            sid = new_task["id"]
            me.post(f"/api/console/tasks/{sid}/complete", {})
            st = me.state()
            check("提醒也能完成并留痕",
                  any("提醒已完成" in t for t in titles(st["messages"])),
                  next((t for t in titles(st["messages"]) if "提醒已完成" in t), "无"))

            r = me.post(f"/api/console/tasks/{sid}/reopen", {})
            check("提醒能恢复（曾经的 404）", r.status_code == 200,
                  f"POST /tasks/{sid}/reopen → {r.status_code}")

            r = me.delete(f"/api/console/schedules/{sid}")
            st = me.state()
            check("提醒能删除", r.status_code == 200
                  and not any(t["id"] == sid for t in st["tasks"]),
                  f"DELETE /schedules/{sid} → {r.status_code}")

        # ---- V1.4：查询与创建必须分开（真链路，不是单测）
        # 单测能验证意图分类，但验证不了"真起服务 + JWT + 完整图"之后，
        # 用户问一句查询到底有没有在库里留下垃圾数据。
        before_count = len([t for t in me.state()["tasks"] if t.get("source") == "ai"])
        ask = me.post("/api/console/assistant", {"text": "看看今天的提醒"}).json()
        after_count = len([t for t in me.state()["tasks"] if t.get("source") == "ai"])
        check("问一句日程查询，不建出任何提醒",
              after_count == before_count,
              f"提醒数 {before_count} → {after_count}；答复：{(ask.get('answer') or '')[:30]}")
        check("查询的答复不是闲聊兜底",
              "我在听" not in (ask.get("answer") or ""),
              (ask.get("answer") or "")[:36])

        # ---- V1.4：说不清是哪台时反问，不许静默挑一台
        ambiguous = me.post("/api/console/assistant",
                            {"text": "打开灯", "confirmed": False}).json()
        check("说不清哪台灯时会反问", "哪一台" in (ambiguous.get("answer") or ""),
              (ambiguous.get("answer") or "")[:40])

        # ---- 需求 3：可点元素的后端支撑
        r = me.post("/api/console/habits/forget", {"memory_key": "preference:不存在的键"})
        check("忘掉不存在的记忆 → 404", r.status_code == 404,
              f"POST /habits/forget → {r.status_code}")

        r = xm.delete("/api/console/schedules/sch_0001")
        check("删别家的提醒 → 404", r.status_code == 404,
              f"小明 DELETE /schedules/sch_0001 → {r.status_code}")

        r = xm.post("/api/console/messages/evt_0001/read", {})
        check("读别家的消息 → 404", r.status_code == 404,
              f"小明 POST /messages/evt_0001/read → {r.status_code}")

        # ---- 注册：建新家 + 邀请码加入 ----
        fresh = Client(base)
        reg = fresh._c.post("/api/auth/register", json={
            "username": "e2e_owner", "password": "pass1234", "name": "端到端户主",
            "mode": "create", "family_name": "端到端的家", "city": "成都",
        })
        check("注册即建家（L4 户主）",
              reg.status_code == 200 and reg.json()["user"]["role"] == "L4",
              f"POST /auth/register → {reg.status_code}")
        fresh.token = reg.json()["token"]
        fresh._c.headers["Authorization"] = f"Bearer {fresh.token}"
        fresh_family = reg.json()["user"]["family"]["family_id"]

        st = fresh.state()
        check("新家有设备（不是空控制台）",
              len(st["devices"]) > 0, f"{len(st['devices'])} 台设备")
        check("新家成员列表只有自己",
              [m["name"] for m in st["family"]] == ["端到端户主"],
              str([m["name"] for m in st["family"]]))

        inv = fresh.get("/api/auth/family/invite")
        check("户主能看邀请码", inv.status_code == 200,
              f"邀请码 {inv.json().get('inviteCode', '无')}")
        invite_code = inv.json().get("inviteCode", "")

        mate = Client(base)
        mate_reg = mate._c.post("/api/auth/register", json={
            "username": "e2e_mate", "password": "pass1234", "name": "端到端家人",
            "mode": "join", "invite_code": invite_code,
        })
        mate_ok = mate_reg.status_code == 200
        if mate_ok:
            mate.token = mate_reg.json()["token"]
            mate._c.headers["Authorization"] = f"Bearer {mate.token}"
        check("邀请码加入同一个家（L3 成员）",
              mate_ok and mate_reg.json()["user"]["family"]["family_id"] == fresh_family
              and mate_reg.json()["user"]["role"] == "L3",
              f"member {mate_reg.json()['user']['member_id']}" if mate_ok else "注册失败")

        r = mate.get("/api/auth/family/invite")
        check("成员看不到邀请码 → 403", r.status_code == 403,
              f"POST /auth/family/invite → {r.status_code}")

        st = fresh.state()
        check("新成员出现在家庭列表里",
              "端到端家人" in [m["name"] for m in st["family"]],
              str([m["name"] for m in st["family"]]))

        # ---- 场景自定义：增删改 + 真执行 ----
        created = fresh.post("/api/console/scenes", {
            "name": "端到端夜读", "icon": "moon",
            "actions": [{"device_id": "bedroom_light", "action": "set_value", "value": 12}],
        })
        scene_ok = created.status_code == 200
        scene = created.json()["scene"] if scene_ok else {}
        check("新建场景", scene_ok and scene.get("builtin") is False,
              f"POST /scenes → {created.status_code}")

        if scene_ok:
            act = fresh.post(f"/api/console/scenes/{scene['id']}/activate")
            light = next((d for d in fresh.state()["devices"]
                          if d["id"] == "bedroom_light"), {})
            check("新场景真能执行（数值落到设备）",
                  act.status_code == 200 and light.get("value") == 12,
                  f"主卧灯 → {light.get('power')} / {light.get('value')}")

            upd = fresh._c.put(f"/api/console/scenes/{scene['id']}", json={
                "name": "端到端改名", "icon": "sun",
                "actions": [{"device_id": "bedroom_light", "action": "turn_off", "value": None}],
            })
            names = [s["name"] for s in fresh.get("/api/console/scenes").json()["scenes"]]
            check("场景改名生效", upd.status_code == 200 and "端到端改名" in names,
                  f"PUT /scenes → {upd.status_code}")

            r = fresh.delete("/api/console/scenes/home")
            check("内置场景删不掉 → 403", r.status_code == 403,
                  f"DELETE /scenes/home → {r.status_code}")

            r = fresh.post("/api/console/scenes", {
                "name": "偷别家设备", "icon": "moon",
                "actions": [{"device_id": "living_room_heater", "action": "turn_on"}],
            })
            check("用别家设备建场景 → 400", r.status_code == 400,
                  f"青岛地暖 → {r.status_code}")

            r = xm.delete(f"/api/console/scenes/{scene['id']}")
            check("删别家的场景 → 404", r.status_code == 404,
                  f"小明 DELETE → {r.status_code}")

            fresh.delete(f"/api/console/scenes/{scene['id']}")
            names = [s["name"] for s in fresh.get("/api/console/scenes").json()["scenes"]]
            check("自建场景能删除", "端到端改名" not in names, f"剩余 {len(names)} 个")

            # ---- 设备与房间管理（V1.3）
            # 这一段必须走真实的 JWT + 真实服务，单测用的是 dependency_overrides
            # 旁路认证，测不出"过了鉴权之后租户还对不对"。
            r = fresh.post("/api/console/devices", {
                "name": "咖啡机", "room": "厨房", "device_type": "extra",
                "min_value": 1, "max_value": 5, "unit": "档", "initial_value": 3,
            })
            added = r.json().get("device", {}) if r.status_code == 200 else {}
            check("能添加自定义电器", bool(added.get("id")),
                  f"POST /devices → {r.status_code} {added.get('name', '')}")

            names = [d["name"] for d in fresh.state()["devices"]]
            check("新设备出现在控制台", "咖啡机" in names, f"共 {len(names)} 台")

            r = fresh.post("/api/console/devices", {"name": "咖啡机", "room": "客厅"})
            check("重名设备被拒绝 → 409", r.status_code == 409,
                  f"重复添加 → {r.status_code}")

            # 关键一条：用户自己加的电器，必须能被自然语言控制
            r = fresh.post("/api/console/assistant", {"text": "把咖啡机打开"})
            coffee = next((d for d in fresh.state()["devices"] if d["name"] == "咖啡机"), {})
            check("自建设备能被 AI 控制", coffee.get("power") is True,
                  f"「把咖啡机打开」→ power={coffee.get('power')}")

            r = fresh.post("/api/console/devices", {
                "name": "落地灯", "room": "影音室", "device_type": "light",
                "min_value": 0, "max_value": 100, "unit": "%",
            })
            lamp = r.json().get("device", {})
            r = fresh.post(f"/api/console/devices/{lamp['id']}/control",
                           {"action": "set_value", "value": 40})
            check("自建房间与调值都生效",
                  r.json().get("device", {}).get("value") == 40,
                  f"影音室落地灯 → {r.json().get('device', {}).get('value')}")

            r = fresh.put(f"/api/console/devices/{lamp['id']}", {"name": "观影灯"})
            check("设备能改名", r.json().get("device", {}).get("name") == "观影灯",
                  f"PUT /devices → {r.status_code}")

            r = fresh.delete("/api/console/rooms/厨房")
            check("有设备的房间删不掉 → 409", r.status_code == 409,
                  f"厨房还有咖啡机 → {r.status_code}")

            fresh.post("/api/console/rooms", {"name": "临时房"})
            r = fresh.delete("/api/console/rooms/临时房")
            check("空房间能删除", r.status_code == 200, f"DELETE /rooms → {r.status_code}")

            r = fresh.put("/api/console/rooms/影音室", {"new_name": "影音厅"})
            rooms = [x["name"] for x in fresh.get("/api/console/rooms").json()["rooms"]]
            check("房间改名生效", "影音厅" in rooms, f"PUT /rooms → {r.status_code}")

            r = xm.delete(f"/api/console/devices/{lamp['id']}")
            check("删别家的设备 → 404", r.status_code == 404,
                  f"小明 DELETE → {r.status_code}")

            r = fresh.delete(f"/api/console/devices/{lamp['id']}")
            check("设备能删除", r.status_code == 200, f"DELETE /devices → {r.status_code}")

            # 删光之后不能再自己长回来（seed_log 守的那条）
            for dev in fresh.state()["devices"]:
                fresh.delete(f"/api/console/devices/{dev['id']}")
            left = len(fresh.state()["devices"])
            check("内置设备删光后不复活", left == 0, f"清空后仍有 {left} 台")
    finally:
        server.should_exit = True

    print("-" * 72)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"端到端验收通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
