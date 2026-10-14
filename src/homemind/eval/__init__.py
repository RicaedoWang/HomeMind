"""评测体系（V0.8）。

对外暴露：
    get_dataset / redline_cases   用例集
    run_eval                      跑一轮评测
    render_report / save_report   渲染与落盘
    compare_with_baseline         与历史基线对比，找回退
"""

from .cases import DATASET, EvalCase, get_dataset, redline_cases
from .report import compare_with_baseline, render_report, save_report
from .runner import CaseResult, EvalReport, run_eval

__all__ = [
    "DATASET",
    "EvalCase",
    "get_dataset",
    "redline_cases",
    "run_eval",
    "render_report",
    "save_report",
    "compare_with_baseline",
    "CaseResult",
    "EvalReport",
]
