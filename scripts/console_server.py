"""Web 控制台启动脚本（唯一推荐入口）。

为什么有这个文件：`PYTHONPATH=src python -m uvicorn ...` 是 bash 语法，
cmd.exe 会把 PYTHONPATH 当程序名报错，PowerShell 也要用 $env: 前缀。
干脆把 sys.path 处理掉，三种终端 + 双击 bat 都能跑：

    python scripts/console_server.py            # 默认 127.0.0.1:8000
    python scripts/console_server.py 8001       # 换端口
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把 src 挂进 sys.path，等价于 PYTHONPATH=src，但不挑终端
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import uvicorn  # noqa: E402

from homemind.config import get_settings  # noqa: E402


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    settings = get_settings()
    print("=" * 60)
    print("HomeMind Web 控制台启动中")
    print(f"  地址：http://127.0.0.1:{port}")
    print(f"  模型：{'真模型（DeepSeek）' if not settings.llm.use_mock else 'Mock（规则模式，不烧 token）'}")
    print("  退出：Ctrl+C")
    print("=" * 60)
    uvicorn.run("homemind.api.main:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
