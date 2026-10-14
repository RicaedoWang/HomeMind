"""API 冒烟测试：不起服务器，直接用 TestClient 打一遍核心接口。

运行：python scripts/smoke_api.py
用途：改完代码快速确认服务还能起来、接口没被打断。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from fastapi.testclient import TestClient  # noqa: E402

from homemind.api.main import app  # noqa: E402

CASES = [
    ("把客厅灯打开", False),
    ("提醒我明天早上九点交房租", False),
    ("提醒我明天早上九点交房租", True),
    ("把大门门锁打开", False),
]


def main() -> None:
    with TestClient(app) as client:
        health = client.get("/health").json()
        print("=== /health ===")
        print(json.dumps(health, ensure_ascii=False, indent=2))

        print("\n=== /api/v1/tools ===")
        tools = client.get("/api/v1/tools").json()
        print(f"共 {tools['count']} 个 MCP 工具：{[t['name'] for t in tools['tools']]}")

        print("\n=== /api/v1/chat ===")
        for text, confirmed in CASES:
            resp = client.post("/api/v1/chat", json={"text": text, "confirmed": confirmed})
            data = resp.json()
            print(f"\n[输入] {text}" + ("  <<已确认>>" if confirmed else ""))
            print(f"  trace_id        : {data['trace_id']}")
            print(f"  意图            : {[i['type'] for i in data['intents']]}")
            print(f"  工具            : {[t.get('tool_name') for t in data['tasks']]}")
            print(f"  需确认          : {data['requires_confirm']}")
            print(f"  降级            : {data['degraded']}")
            print(f"  应答            : {data['answer']}")
            assert resp.status_code == 200, f"HTTP {resp.status_code}"

        print("\n=== /api/v1/devices ===")
        for dev in client.get("/api/v1/devices").json()["devices"]:
            print(f"  {dev['name']}({dev['room']})  电源={'开' if dev['power'] else '关'}  值={dev['value']}")

        print("\n=== /api/v1/schedule ===")
        for sch in client.get("/api/v1/schedule").json()["schedules"]:
            print(f"  {sch['schedule_id']}  {sch['scheduled_at']}  {sch['content']}")

    print("\n冒烟测试通过 ✓")


if __name__ == "__main__":
    main()
