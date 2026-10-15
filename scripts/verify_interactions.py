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

    def delete(self, path: str) -> httpx.Response:
        return self._c.delete(path)

    def state(self) -> dict:
        return self.get("/api/console/state").json()


def titles(messages: list[dict]) -> list[str]:
    return [m["title"] for m in messages]


def main() -> int:
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
    finally:
        server.should_exit = True

    print("-" * 72)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"端到端验收通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
