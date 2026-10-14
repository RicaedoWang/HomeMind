"""评测 CLI（V0.8 评测体系入口）。

跑法：
    python scripts/run_eval.py                    # 跑全量（默认 mock 模型，确定性）
    python scripts/run_eval.py --save-baseline    # 跑完把结果存为基线
    python scripts/run_eval.py --real             # 用真模型跑（会烧 token，结果不保证稳定）
    python scripts/run_eval.py --json reports/x.json

退出码：
    0  评测通过（红线全通过）
    1  有红线用例失败——安全底线没守住，CI 应直接拦住不让合入
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from homemind.eval import (  # noqa: E402
    compare_with_baseline,
    render_report,
    run_eval,
    save_report,
)

DEFAULT_JSON = "reports/eval-latest.json"
DEFAULT_BASELINE = "reports/eval-baseline.json"


async def main() -> int:
    parser = argparse.ArgumentParser(description="运行 HomeMind 端到端评测")
    parser.add_argument("--real", action="store_true",
                        help="用真模型评测（默认 mock，确定性且不烧 token）")
    parser.add_argument("--save-baseline", action="store_true",
                        help="把本次结果存为基线，供以后对比回退")
    parser.add_argument("--json", default=DEFAULT_JSON, help="报告 JSON 输出路径")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, help="基线文件路径")
    args = parser.parse_args()

    if not args.real:
        # 默认 mock：真模型输出不确定，评测就失去了回归价值。
        # 想看真模型表现用 --real，但别拿它的结果当回归基线。
        os.environ["HM_USE_MOCK_LLM"] = "true"

    mode = "real" if args.real else "mock"
    print(f"\n正在以 {mode} 模式运行评测……\n")

    report = await run_eval(mode=mode)

    print(render_report(report))
    print()
    print(compare_with_baseline(report, args.baseline))

    save_report(report, args.json)
    print(f"\n报告已写入：{args.json}")
    if args.save_baseline:
        save_report(report, args.baseline)
        print(f"已存为基线：{args.baseline}")

    # 红线不通过 → 非零退出码，方便 CI 卡住
    return 0 if report.redline_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
