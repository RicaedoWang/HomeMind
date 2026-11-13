# -*- coding: utf-8 -*-
"""用 Pillow 直接绘制两张架构图（PNG，可保存）。v2：修复层叠布局。

图 1：技术架构图（五层，层 y 坐标累加，不再重叠）
图 2：代码功能框架图（八个模块 + 文件路径映射，块高随文件数自适应）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

OUT = Path(__file__).resolve().parents[1] / "docs"
OUT.mkdir(exist_ok=True)

BG = (11, 17, 32)
PANEL = (17, 28, 51)
PANEL_EDGE = (30, 43, 71)
CARD = (22, 35, 63)
CARD2 = (31, 39, 64)
TXT = (230, 237, 247)
MUTED = (139, 160, 192)
CYAN = (56, 189, 248)
PINK = (251, 113, 133)
GREEN = (52, 211, 153)
AMBER = (251, 191, 36)
VIOLET = (167, 139, 250)


def _font(size, bold=False):
    for name in ("msyhbd.ttc" if bold else "msyh.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except Exception:
            continue
    return ImageFont.load_default()


def _d(d, xy, text, size=14, color=TXT, bold=False, anchor="mm"):
    d.text(xy, text, font=_font(size, bold), fill=color, anchor=anchor)


def _dl(d, xy, text, size=14, color=TXT, bold=False):
    d.text(xy, text, font=_font(size, bold), fill=color, anchor="lm")


def _rbox(d, xy, color, edge, radius=8, width=2):
    d.rounded_rectangle(xy, radius=radius, fill=color, outline=edge, width=width)


def _arrow(d, x, y1, y2, color=MUTED, width=2):
    d.line([(x, y1), (x, y2 - 7)], fill=color, width=width)
    d.polygon([(x - 6, y2 - 7), (x + 6, y2 - 7), (x, y2)], fill=color)


# ============================================================
# 图 1：技术架构图
# ============================================================
def build_tech_arch():
    # 层高 + 层间距先算好，y 累加
    L1_H, L2_H, L3_H, L4_H, L5_H = 100, 128, 180, 140, 172
    GAP = 34
    y0 = 100
    y2 = y0 + L1_H + GAP
    y3 = y2 + L2_H + GAP
    y4 = y3 + L3_H + GAP
    y5 = y4 + L4_H + GAP
    H = y5 + L5_H + 60

    W = 1240
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    _d(d, (W // 2, 42), "HomeMind 技术架构", 26, TXT, True)
    _d(d, (W // 2, 72), "智能家居多智能体中枢 · LangGraph + DeepSeek + 三层记忆 + 多租户隔离", 13, MUTED)

    LX = 36
    BW = W - LX * 2

    def layer(y, h, num, title, sub, color):
        _rbox(d, (LX, y, LX + BW, y + h), PANEL, PANEL_EDGE, 10, 2)
        d.ellipse((LX + 16, y + 20, LX + 42, y + 46), outline=color, width=2)
        _d(d, (LX + 29, y + 33), num, 13, color, True)
        _dl(d, (LX + 52, y + 22), title, 16, TXT, True)
        _dl(d, (LX + 52, y + 44), sub, 11, MUTED)

    # ---- L1 接入层 ----
    y = y0
    layer(y, L1_H, "1", "接入层", "用户 · 浏览器 · 命令行", CYAN)
    c1 = [
        (LX + 250, 320, "Web 控制台", "web/index.html + app.js"),
        (LX + 620, 320, "对话 API", "POST /api/v1/chat · JWT"),
        (LX + 990, 210, "CLI / 演示", "scripts/chat_cli.py"),
    ]
    for cx, cw, t, sub in c1:
        _rbox(d, (cx - cw // 2, y + 54, cx + cw // 2, y + 90), CARD, PANEL_EDGE, 6, 1)
        _d(d, (cx, y + 66), t, 13, TXT, True)
        _d(d, (cx, y + 81), sub, 9.5, CYAN)
    _arrow(d, LX + BW // 2, y + L1_H, y + L1_H + GAP)

    # ---- L2 API 层 ----
    y = y2
    layer(y, L2_H, "2", "API / 服务层", "FastAPI + uvicorn · src/homemind/api/", GREEN)
    api = [
        ("main.py", "服务入口·/chat·/health"),
        ("auth.py", "登录鉴权·JWT"),
        ("console.py", "控制台 REST"),
        ("events.py", "消息中心数据源"),
    ]
    for i, (f, desc) in enumerate(api):
        bx = LX + 20 + i * 290
        _rbox(d, (bx, y + 58, bx + 274, y + 108), CARD, PANEL_EDGE, 6, 1)
        _d(d, (bx + 137, y + 74), f, 12, CYAN, True)
        _d(d, (bx + 137, y + 94), desc, 10, MUTED)
    _arrow(d, LX + BW // 2, y + L2_H, y + L2_H + GAP)

    # ---- L3 编排层 ----
    y = y3
    layer(y, L3_H, "3", "编排层（Agent 核心）", "LangGraph 状态图 + 多智能体 · agent/ + agents/", PINK)
    nodes = ["记忆召回", "意图解析", "安全护栏", "工具执行", "应答生成"]
    for i, n in enumerate(nodes):
        bx = LX + 20 + i * 226
        _rbox(d, (bx, y + 58, bx + 202, y + 100), CARD2, PINK, 21, 2)
        _d(d, (bx + 101, y + 79), n, 13, TXT, True)
        if i < 4:
            d.line([(bx + 202, y + 79), (bx + 220, y + 79)], fill=PINK, width=2)
            d.polygon([(bx + 220, y + 74), (bx + 220, y + 84), (bx + 226, y + 79)], fill=PINK)
    _dl(d, (LX + 20, y + 118), "agent/graph.py 状态图：", 11, MUTED)
    _dl(d, (LX + 20, y + 138),
        "START → recall_memory → parse_intent → safety_gate → execute_tools → respond → write_memory → END",
        10, CYAN)
    _dl(d, (LX + 20, y + 158),
        "子 Agent（职责隔离+工具白名单）：DeviceAgent / ScheduleAgent / KnowledgeAgent ＋ Supervisor 分派·冲突检测",
        10, VIOLET)
    _arrow(d, LX + BW // 2, y + L3_H, y + L3_H + GAP)

    # ---- L4 领域层 ----
    y = y4
    layer(y, L4_H, "4", "领域 / 能力层", "意图 · 记忆 · 工具 · 安全", VIOLET)
    dom = [
        ("意图解析", "agent/intent.py", "规则+LLM 双模"),
        ("三层记忆", "memory/short·long·semantic", "短期/长期/语义"),
        ("工具治理", "tools/registry.py", "熔断·幂等·Schema"),
        ("安全护栏", "graph.py safety_gate", "高危二次确认"),
    ]
    for i, (t, f, desc) in enumerate(dom):
        bx = LX + 20 + i * 290
        _rbox(d, (bx, y + 58, bx + 274, y + 120), CARD, PANEL_EDGE, 6, 1)
        _d(d, (bx + 137, y + 74), t, 13, TXT, True)
        _d(d, (bx + 137, y + 93), f, 9, VIOLET)
        _d(d, (bx + 137, y + 108), desc, 9, MUTED)
    _arrow(d, LX + BW // 2, y + L4_H, y + L4_H + GAP)

    # ---- L5 基础设施层 ----
    y = y5
    layer(y, L5_H, "5", "基础设施层", "存储 · 模型 · 可观测", AMBER)
    inf = [
        ("SQLite", "core/db.py · WAL", "记忆/设备/知识库"),
        ("Redis 7.4", "短期记忆·可降级", "Docker 真跑"),
        ("DeepSeek", "core/llm.py", "deepseek-flash"),
        ("Embedding", "memory/embedder.py", "bge-small-zh 512维"),
        ("Trace", "core/trace.py", "trace_id 全链路"),
    ]
    for i, (t, f, desc) in enumerate(inf):
        bx = LX + 20 + i * 178
        _rbox(d, (bx, y + 56, bx + 164, y + 126), CARD, PANEL_EDGE, 6, 1)
        _d(d, (bx + 82, y + 72), t, 12.5, TXT, True)
        _d(d, (bx + 82, y + 92), f, 8.5, AMBER)
        _d(d, (bx + 82, y + 108), desc, 8.5, MUTED)
    # 降级说明（贴右边）
    _rbox(d, (LX + BW - 232, y + 56, LX + BW - 16, y + 126), (26, 21, 35), (59, 42, 62), 6, 1)
    _d(d, (LX + BW - 124, y + 72), "降级哲学", 12, PINK, True)
    _d(d, (LX + BW - 124, y + 92), "LLM 挂→规则  Redis 挂→内存", 9, TXT)
    _d(d, (LX + BW - 124, y + 108), "Embedding 挂→Jaccard", 9, TXT)

    _d(d, (W // 2, H - 26), "绿=对外接口 · 粉=Agent 核心 · 紫=领域能力 · 黄=基础设施", 12, MUTED)
    return img


# ============================================================
# 图 2：代码功能框架图（含文件映射）
# ============================================================
def build_code_map():
    modules = [
        ("core/", "核心基建", CYAN, "配置·DB·LLM·租户·日志", [
            ("config.py", "全局配置（env 驱动，全部可降级）"),
            ("db.py", "SQLite 连接工厂 · WAL"),
            ("llm.py", "DeepSeek 客户端（OpenAI 兼容）"),
            ("models.py", "IntentType / ToolResult 等数据模型"),
            ("tenancy.py", "family_id 租户隔离"),
            ("trace.py", "日志 + span + trace_id"),
        ]),
        ("agent/", "主链路编排", PINK, "LangGraph 状态图·意图·安全", [
            ("graph.py", "状态图：召回→解析→护栏→执行→应答（490行）"),
            ("intent.py", "意图识别（规则+LLM 双模）"),
            ("execution.py", "工具执行原语（增量解析/幂等键）"),
            ("state.py", "AgentState 定义"),
            ("time_parser.py", "时间解析 + describe_now"),
        ]),
        ("agents/", "多智能体", VIOLET, "职责隔离·工具白名单", [
            ("supervisor.py", "分派 + 冲突检测 + 并发隔离"),
            ("device_agent.py", "设备控制（白名单工具）"),
            ("schedule_agent.py", "日程/提醒"),
            ("knowledge_agent.py", "说明书问答（语义召回）"),
            ("base.py", "Agent 基类（admits 白名单）"),
        ]),
        ("memory/", "三层记忆", GREEN, "短期/长期/语义 + 知识库", [
            ("short_term.py", "滑动窗口 + 超限摘要（Redis）"),
            ("long_term.py", "SQLite 长期记忆"),
            ("semantic.py", "语义召回（向量/Jaccard）"),
            ("service.py", "统一门面：写入/召回/遗忘"),
            ("embedder.py", "bge-small-zh 本地向量"),
            ("knowledge_store.py", "说明书知识库（RAG 闭环）"),
        ]),
        ("tools/", "工具层", AMBER, "MCP 工具·治理", [
            ("registry.py", "注册中心：超时/重试/熔断/幂等"),
            ("device_server.py", "设备控制工具"),
            ("calendar_server.py", "日程/提醒工具"),
            ("device_store.py", "设备/房间落库"),
        ]),
        ("api/", "对外接口", PINK, "FastAPI 路由", [
            ("main.py", "服务入口 + /chat + /health"),
            ("auth.py", "登录/注册/鉴权"),
            ("console.py", "控制台 REST（1451 行）"),
            ("events.py", "消息中心数据源"),
        ]),
        ("auth/", "认证授权", GREEN, "登录·JWT·家庭", [
            ("store.py", "账号存储 + demo 账号"),
            ("family_store.py", "家庭/成员/场景落库"),
            ("tokens.py", "JWT 签发/校验"),
            ("passwords.py", "密码哈希"),
            ("deps.py", "current_user 依赖"),
        ]),
        ("eval/", "评测体系", VIOLET, "端到端评测·红线", [
            ("cases.py", "54 条评测用例"),
            ("runner.py", "评测执行器"),
            ("report.py", "评测报告"),
        ]),
    ]

    # 布局：每模块块高 = max(90, 文件数*24 + 24)
    LX, LW = 36, 360
    FX = 440        # 文件名列 x
    DX = 680        # 说明列 x
    y = 100
    positions = []
    for dirn, name, color, sub, files in modules:
        h = max(92, len(files) * 24 + 26)
        positions.append((y, h))
        y += h + 14
    H = y + 50
    W = 1300

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _d(d, (W // 2, 40), "HomeMind 代码功能框架图", 26, TXT, True)
    _d(d, (W // 2, 70), "src/homemind/ 八个模块 · 功能 → 文件路径 · 找代码：先定位模块，再按文件名检索", 13, MUTED)
    _dl(d, (FX, 100), "文件（src/homemind/ 下）", 12, CYAN, True)
    _dl(d, (DX, 100), "功能说明", 12, MUTED, True)

    for (dirn, name, color, sub, files), (by, bh) in zip(modules, positions):
        _rbox(d, (LX, by, LX + LW, by + bh), PANEL, color, 8, 2)
        _dl(d, (LX + 18, by + 24), dirn, 14, color, True)
        _dl(d, (LX + 18, by + 52), name, 16, TXT, True)
        _dl(d, (LX + 18, by + 74), sub, 10, MUTED)
        for j, (fn, desc) in enumerate(files):
            fy = by + 20 + j * 24
            _dl(d, (FX, fy), fn, 12, CYAN, True)
            _dl(d, (DX, fy), desc, 11, MUTED)

    _d(d, (W // 2, H - 24), "左=模块目录 · 右=文件路径 + 一句话功能 · 全部位于 src/homemind/ 下", 12, MUTED)
    return img


def save(img, name):
    p = OUT / name
    img.save(p, "PNG")
    print(f"✅ 已生成 {p}  ({img.width}x{img.height})")


if __name__ == "__main__":
    save(build_tech_arch(), "HomeMind_技术架构图.png")
    save(build_code_map(), "HomeMind_代码功能框架图.png")
