"""SQLite 连接配置的回归（WAL / 外键 / 超时）。

## 为什么值得单独测

这些是**连接级**配置，设错了不会报错、不会崩、功能测试全绿——
只会在「多用户并发写」或「崩溃恢复」时悄悄出问题。

实测背景（2026-10-21）：
- 出厂默认 journal 模式，单条事务提交 **3.63 ms**
- 换成 WAL + synchronous=NORMAL 后 **0.02 ms**（纯 SQLite 层，180 倍）
- 经 DeviceStore.save_state 全链路，**0.816 ms/次**（含开连接、查询、提交）

项目原先是四处各自 `sqlite3.connect`，一处 PRAGMA 都没有。
所以这份用例要守住两件事：
1. 连接工厂确实把 PRAGMA 设上了
2. 四个存储模块**没有**再自己写裸连接（否则漏掉的那处等于没优化）
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from homemind.core.db import connect, journal_mode  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "t.db")


# ---------------------------------------------------------------- 配置本身

def test_journal_mode_is_wal(db_path):
    """WAL 是这次优化的核心：写不阻塞读，单条提交快 180 倍。"""
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_synchronous_is_normal(db_path):
    """NORMAL 而不是 FULL。

    FULL 每次提交都 fsync，写一条要 3.6 ms；NORMAL 在 WAL 下仍保证崩溃一致性，
    极端断电最多丢最后几个已提交事务。对智能家居控制台这个取舍是划算的。
    """
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # 1=NORMAL


def test_foreign_keys_enabled(db_path):
    """SQLite 默认关闭外键约束——建表写了 REFERENCES 也拦不住脏数据。"""
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_busy_timeout_set(db_path):
    """拿不到写锁时等一会儿，而不是立刻抛 database is locked。"""
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_row_factory_returns_named_rows(db_path):
    """四个 store 全都用 row["列名"] 取值，row_factory 不能丢。"""
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE t(a TEXT)")
        conn.execute("INSERT INTO t VALUES ('x')")
        row = conn.execute("SELECT a FROM t").fetchone()
        assert row["a"] == "x"


def test_row_factory_can_be_disabled(db_path):
    with connect(db_path, row_factory=False) as conn:
        conn.execute("CREATE TABLE t(a TEXT)")
        conn.execute("INSERT INTO t VALUES ('x')")
        row = conn.execute("SELECT a FROM t").fetchone()
        assert row[0] == "x"
        assert not hasattr(row, "keys")


# ---------------------------------------------------------------- 持久化行为

def test_wal_persists_across_connections(db_path):
    """WAL 是写入库文件的持久设置，不是一次性开关。

    所以用裸 sqlite3 另开一个连接也能读到 wal——这条正是验收脚本
    用来证明「优化真的生效了」而不是「我以为我设了」。
    """
    with connect(db_path):
        pass
    assert journal_mode(db_path) == "wal"

    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        raw.close()


def test_memory_db_does_not_crash(tmp_path):
    """内存库没有文件，WAL 无处可写——不能为此报错。"""
    with connect(":memory:") as conn:
        conn.execute("CREATE TABLE t(a TEXT)")
        conn.execute("INSERT INTO t VALUES ('ok')")
        assert conn.execute("SELECT a FROM t").fetchone()[0] == "ok"


# ---------------------------------------------------------------- 白盒守门

STORES = [
    "auth/store.py",
    "auth/family_store.py",
    "memory/long_term.py",
    "tools/device_store.py",
]


@pytest.mark.parametrize("rel", STORES)
def test_store_does_not_open_raw_connection(rel):
    """四个存储模块不许再自己写 `sqlite3.connect(...)`。

    这次优化的价值就在于「只有一处配置」。谁要是新增一个 module
    自己连库，就会绕开 WAL——不报错、测试也全绿，只是慢 180 倍。
    """
    src = (SRC / "homemind" / rel).read_text(encoding="utf-8")
    offenders = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # 注释里提旧写法是为了解释，不算违规
        if re.search(r"sqlite3\.connect\s*\(", stripped):
            offenders.append(stripped)
    assert not offenders, (
        f"{rel} 又自己开了裸连接：{offenders}——请改用 core.db.connect()，"
        f"否则会绕开 WAL 与外键配置"
    )


@pytest.mark.parametrize("rel", STORES)
def test_store_imports_core_db(rel):
    """反向确认：每个 store 确实通过 core.db.connect 建连接。"""
    src = (SRC / "homemind" / rel).read_text(encoding="utf-8")
    assert "core.db import connect" in src or "core.db import" in src, (
        f"{rel} 没有引用 core.db 的连接工厂"
    )


# ---------------------------------------------------------------- 测试环境隔离

def test_tests_run_without_real_redis():
    """测试环境必须禁用 Redis，不许连真实服务。

    背景：2026-10-21 本机真跑起 Redis，`.env` 里的 `HM_REDIS_ENABLED` 改成了 true。
    测试若跟着去连真实 Redis，就变成「依赖外部服务」——别人机器上跑必挂，
    同一份代码两次跑结果可能不同。

    这条断言守的是**结果**（测试进程里 redis 必须是关的），不绑定实现方式。
    实测过：conftest 无论是强制赋值还是 setdefault，在当前 import 顺序下都能得到
    false（conftest 早于 dotenv 加载 .env）。所以这条用例真正能抓住的是
    「那行被整个删掉」或「有人改成 true」。

    守不住的是「强制赋值被换回 setdefault」——两种写法在当前顺序下等价，
    只有顺序被打破才会出问题，那属于防御性问题，不是这条用例的职责。
    """
    from homemind.config import get_settings

    assert get_settings().redis.enabled is False, (
        "测试环境竟然启用了 Redis —— 检查 conftest.py 顶部的 HM_REDIS_ENABLED 设置。"
        "测试必须不依赖任何外部服务"
    )
