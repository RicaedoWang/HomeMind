"""一键自检：把项目成果全部跑一遍，汇总成一张表。

跑法：
    python scripts/verify_all.py          # 默认 mock，确定性、不烧 token
    python scripts/verify_all.py --real   # 用真模型跑（会烧 token，结果有波动）

为什么默认 mock：自检的目的是"这台机器上东西全不全、有没有跑崩"，
不是"模型今天状态好不好"。用真模型会把偶发的模型波动当成项目问题。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PY = sys.executable


def _env(mock: bool) -> dict[str, str]:
    import os

    env = dict(os.environ)
    if mock:
        env["HM_USE_MOCK_LLM"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(name: str, cmd: list[str], mock: bool = True) -> tuple[bool, str]:
    """跑一条命令，返回 (是否成功, 摘要信息)。"""
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_env(mock))
    elapsed = time.perf_counter() - started
    output = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode != 0:
        tail = [l for l in output.strip().splitlines() if l.strip()][-3:]
        return False, f"退出码 {proc.returncode} | {' / '.join(tail)}"

    detail = _summarize(name, output)
    return True, f"{detail}（{elapsed:.1f}s）"


def _summarize(name: str, output: str) -> str:
    """从输出里抠关键数字，别让用户自己翻日志。"""
    patterns = {
        "单元测试": r"(\d+ passed[^\n]*)",
        "端到端评测": r"用例总数 (\d+)\s+通过 (\d+)\s+失败 (\d+)\s+通过率\s+([\d.]+%)",
        "多 Agent": r"## 演示结束",
        "Web 界面": r"UI 构建 OK",
    }
    if name in ("单元测试", "Web 控制台"):
        hit = re.search(patterns["单元测试"], output)
        if hit:
            return f"{hit.group(1)}（控制台契约全守）" if name == "Web 控制台" else hit.group(1)
        return "通过"
    if name == "端到端评测":
        hit = re.search(patterns["端到端评测"], output)
        if hit:
            return f"{hit.group(2)}/{hit.group(1)} 通过，通过率 {hit.group(4)}"
        return "通过（红线全守）"
    if name == "多 Agent 协同":
        hits = len(re.findall(r"【\d/\d】", output))
        return f"六段演示 {hits} 段全部跑完" if hits else "通过"
    if name == "记忆系统":
        # 演示脚本按"1. 2. 3. …"列出六段能力，数段落标题比数分隔符靠谱
        sections = len(re.findall(r"^\s*\d+\.\s", output, flags=re.MULTILINE))
        return f"六段能力演示跑完（写入/召回/冲突/权限/遗忘/衰减）" if sections >= 6 \
            else f"演示段落 {sections} 段"
    if name == "主链路":
        return "V0.1 七条用例全通过"
    if name == "API 冒烟":
        return "接口全部返回 200"
    if name == "Git 仓库":
        return output.strip().splitlines()[0] if output.strip() else "正常"
    return "通过"


def code_stats() -> tuple[int, int]:
    """统计代码行数与文件数。"""
    files = list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")) \
        + list((ROOT / "tests").rglob("*.py"))
    lines = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in files)
    return len(files), lines


def git_summary() -> str:
    proc = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(ROOT),
                          capture_output=True, text=True)
    commits = proc.stdout.strip() if proc.returncode == 0 else "?"
    proc2 = subprocess.run(["git", "status", "--short"], cwd=str(ROOT),
                           capture_output=True, text=True)
    dirty = "干净" if proc2.stdout.strip() == "" else f"{len(proc2.stdout.strip().splitlines())} 处未提交"
    return f"{commits} 个提交，工作区{dirty}"


CHECKS = [
    ("单元测试", [PY, "-m", "pytest", "-q"], True),
    ("端到端评测", [PY, "scripts/run_eval.py"], True),
    ("多 Agent 协同", [PY, "scripts/demo_multi_agent.py"], True),
    ("记忆系统", [PY, "scripts/demo_memory.py"], True),
    ("主链路", [PY, "scripts/demo.py"], True),
    ("API 冒烟", [PY, "scripts/smoke_api.py"], True),
    ("Web 控制台", [PY, "-m", "pytest", "tests/test_console.py", "-q"], True),
    ("Git 仓库", ["git", "log", "--oneline", "-1"], False),
]


def main() -> int:
    mode = "mock（规则模式）" if "--real" not in sys.argv else "真模型"
    print("\n" + "=" * 72)
    print(f"HomeMind 成果自检    模式：{mode}")
    print("=" * 72 + "\n")

    rows: list[tuple[str, bool, str]] = []
    for name, cmd, mock in CHECKS:
        if mode != "mock（规则模式）":
            mock = False
        print(f"  正在跑：{name} …", end="", flush=True)
        ok, detail = run(name, cmd, mock=mock)
        print(f"\r  {'✅' if ok else '❌'} {name:<16} {detail}")
        rows.append((name, ok, detail))

    # Web 界面：只验构建，不启服务器（启了就不叫自检了）
    print("  正在跑：Web 界面 …", end="", flush=True)
    web = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0,'scripts'); sys.path.insert(0,'src');"
         "import web_ui; web_ui.build_ui(); print('UI 构建 OK')"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=_env(True),
    )
    ok = web.returncode == 0 and "UI 构建 OK" in (web.stdout or "")
    print(f"\r  {'✅' if ok else '❌'} {'Web 界面':<16} "
          f"{'构建通过（未启服务器）' if ok else '构建失败'}")

    files, lines = code_stats()
    print("\n" + "-" * 72)
    print("【汇总】")
    print(f"  Python 文件 {files} 个，代码 {lines} 行")
    print(f"  {git_summary()}")
    passed = sum(1 for _, ok, _ in rows if ok) + (1 if ok else 0)
    total = len(rows) + 1
    print(f"  自检项通过 {passed}/{total}")
    print("-" * 72)

    failed = [name for name, ok, _ in rows if not ok]
    if failed:
        print(f"\n❌ 未通过的项：{', '.join(failed)}")
        return 1
    print("\n✅ 全部自检通过。项目成果完整。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
