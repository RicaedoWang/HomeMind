"""评测报告：渲染成人类可读的表格 + 机器可读的 JSON + 基线对比。

基线对比是防"改一个坏一片"的关键：
本次跑完与上次基线比一遍，通过率掉了、或者有哪条从 pass 变 fail，
当场就能指出来——而不是等用户投诉才发现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import EvalReport

_WIDTH = 72


def _pct(value: float | None) -> str:
    if value is None:
        return "  n/a "
    return f"{value * 100:5.1f}%"


def render_report(report: EvalReport) -> str:
    """渲染终端可读的评测报告。"""
    lines: list[str] = []
    lines.append("=" * _WIDTH)
    lines.append(f"HomeMind 评测报告    模式：{report.mode}    耗时：{report.elapsed_ms}ms")
    lines.append("=" * _WIDTH)

    m = report.metrics
    lines.append("")
    lines.append("【总体】")
    lines.append(f"  用例总数 {m['overall']['total']}    通过 {m['overall']['passed']}"
                 f"    失败 {m['overall']['total'] - m['overall']['passed']}"
                 f"    通过率 {_pct(m['overall']['rate'])}")

    lines.append("")
    lines.append("【分项指标】")
    rows = [
        ("意图识别准确率", m["intent_accuracy"]),
        ("工具选择准确率", m["tool_accuracy"]),
        ("Agent 分派准确率", m["agent_dispatch_accuracy"]),
    ]
    for name, item in rows:
        lines.append(f"  {name:<16} {item['passed']}/{item['total']}   {_pct(item['rate'])}")

    rl = m["redline"]
    flag = "✅ 全通过" if rl["all_passed"] else "❌ 有红线用例失败"
    lines.append(f"  {'安全红线':<16} {rl['passed']}/{rl['total']}    "
                 f"{'零误操作' if rl['all_passed'] else '不合格'}  {flag}")

    lines.append("")
    lines.append("【分组通过率】")
    groups: dict[str, list[bool]] = {}
    for r in report.effective_results:  # 与总口径一致：排除已知缺口
        groups.setdefault(r.case.group, []).append(r.passed)
    for group, flags in sorted(groups.items()):
        ok = sum(1 for f in flags if f)
        rate = ok / len(flags)
        mark = "  " if ok == len(flags) else "⚠ "
        lines.append(f"  {mark}{group:<12} {ok}/{len(flags)}   {_pct(rate)}")

    failures = [r for r in report.effective_results if not r.passed]
    if failures:
        lines.append("")
        lines.append(f"【失败明细】共 {len(failures)} 条")
        for r in failures:
            tag = "🔴 红线" if r.case.redline else "   普通"
            lines.append(f"  {tag} {r.case.id}  「{r.case.input}」")
            for reason in r.failures:
                lines.append(f"        ↳ {reason}")
            if r.case.note:
                lines.append(f"        备注：{r.case.note}")
    else:
        lines.append("")
        lines.append("【失败明细】无——全部有效用例通过")

    # 已知缺口单列：既不让它污染通过率，也不让它悄悄消失
    if report.gap_results:
        lines.append("")
        lines.append(f"【已知能力缺口】共 {len(report.gap_results)} 条（不计入通过率）")
        for r in report.gap_results:
            mark = "✅ 已修复" if r.passed else "⬜ 待办"
            lines.append(f"  {mark}  {r.case.id}「{r.case.input}」")
            lines.append(f"         {r.case.note}")
        if report.gap_fixed:
            lines.append(f"  提示：{len(report.gap_fixed)} 条缺口已被补上，"
                         f"建议从 known_gap 名单移除：{[r.case.id for r in report.gap_fixed]}")

    lines.append("")
    lines.append("=" * _WIDTH)
    verdict = "评测通过" if report.redline_passed else "评测不通过（安全红线未守住）"
    lines.append(f"结论：{verdict}")
    lines.append("=" * _WIDTH)
    return "\n".join(lines)


def save_report(report: EvalReport, path: str) -> None:
    """把报告存成 JSON（机器可读，供 CI 消费）。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load_report(path: str) -> dict[str, Any] | None:
    """读取历史报告（基线）。"""
    file = Path(path)
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def compare_with_baseline(report: EvalReport, baseline_path: str) -> str:
    """与基线对比，返回可读的对比文本。"""
    baseline = load_report(baseline_path)
    if baseline is None:
        return "（无基线，本次结果可作为基线：`--save-baseline`）"

    old_metrics = baseline.get("metrics", {})
    old_map = {c["id"]: c["passed"] for c in baseline.get("cases", [])}

    lines: list[str] = []
    lines.append("【与基线对比】")
    new_overall = report.metrics["overall"]["rate"]
    old_overall = (old_metrics.get("overall") or {}).get("rate")
    if old_overall is not None:
        delta = new_overall - old_overall
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(f"  总通过率  {_pct(old_overall)} → {_pct(new_overall)}  {arrow}")

    # 回退：从通过变成失败——这才是真正要看的东西
    regressed = [r for r in report.results
                 if not r.passed and old_map.get(r.case.id) is True]
    if regressed:
        lines.append(f"  ⚠ 发现 {len(regressed)} 条回退（基线通过、本次失败）：")
        for r in regressed:
            lines.append(f"      {r.case.id} 「{r.case.input}」")
            for reason in r.failures:
                lines.append(f"          ↳ {reason}")
    else:
        lines.append("  ✅ 无回退用例")
    return "\n".join(lines)
