"""Web 控制台启动脚本（唯一推荐入口）。

为什么有这个文件：`PYTHONPATH=src python -m uvicorn ...` 是 bash 语法，
Windows 传统终端会把 PYTHONPATH 当程序名报错。这里自己把 src 挂进
sys.path，任何终端（含双击 bat）都能跑：

    python scripts/console_server.py            # 默认 127.0.0.1:8000
    python scripts/console_server.py 8001       # 指定起始端口

两个贴心设计：
1. 端口被占（上一次的服务没退干净 / 10048 错误）→ 自动顺延到下一个空闲端口；
2. 服务就绪后自动打开浏览器，不用手动敲网址。
"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser
from pathlib import Path

# 把 src 挂进 sys.path，等价于 PYTHONPATH=src，但不挑终端
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import uvicorn  # noqa: E402

from homemind.config import get_settings  # noqa: E402


def _port_in_use(port: int) -> bool:
    """探测端口是否已有服务在听（连接成功 = 被占）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    while _port_in_use(port):
        print(f"  端口 {port} 已被占用（多半是上次的服务还挂着），改用 {port + 1}")
        port += 1

    settings = get_settings()
    url = f"http://127.0.0.1:{port}"
    print("=" * 60)
    print("HomeMind Web 控制台启动中")
    print(f"  地址：{url}   （浏览器将自动打开；没开就手动复制这行）")
    print(f"  模型：{'真模型（DeepSeek）' if not settings.llm.use_mock else 'Mock（规则模式，不烧 token）'}")
    print("  退出：Ctrl+C")
    print("=" * 60)

    # uvicorn.run 会阻塞，用定时器在服务就绪后拉起浏览器
    threading.Timer(2.0, webbrowser.open, args=(url,)).start()
    uvicorn.run("homemind.api.main:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
