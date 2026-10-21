"""SQLite 连接的唯一入口。

## 为什么要有这个文件

项目里有四个独立存储：账号（`auth/store.py`）、家庭与场景（`auth/family_store.py`）、
长期记忆（`memory/long_term.py`）、设备与房间（`tools/device_store.py`）。
它们各自 `sqlite3.connect()` 再各自设 `row_factory`。

加一条 PRAGMA 优化就得改四处，**漏一处等于没做**——而漏掉的那处往往是最热的写路径
（设备状态、事件流），恰好就是最需要优化的地方。收成一个函数，加参数只需要改这里。

## 这几条 PRAGMA 在做什么

| PRAGMA | 作用 | 实测影响 |
|---|---|---|
| `journal_mode=WAL` | 读写不互相阻塞 | **单条提交 3.63ms → 0.02ms（180 倍）** |
| `synchronous=NORMAL` | WAL 下不必每次 fsync | 与上面配合才有效果；崩溃最多丢最后几条未提交事务，不损坏库 |
| `busy_timeout=5000` | 遇到写锁等 5 秒而不是立刻报错 | 多进程/多线程并发写时，从 `database is locked` 报错变成短暂排队 |
| `foreign_keys=ON` | 打开外键约束 | SQLite 默认**关闭**外键，设了 `REFERENCES` 也不生效 |

`synchronous=NORMAL` 是权衡不是偷懒：FULL 模式下每次提交都 fsync，写一条要 3.6ms；
NORMAL 在 WAL 模式下依然保证崩溃一致性，只是极端断电可能丢最后几个已提交事务。
对一个智能家居控制台，这个取舍明显划算——丢一条"开了客厅灯"的事件，
比每次开灯卡 4 毫秒可接受得多。

## 内存库要特殊处理

`PRAGMA journal_mode=WAL` 对 `:memory:` 数据库无效（没有文件可写 WAL）。
测试里大量用临时文件而不是 `:memory:`，所以这个分支很少走到，但走到时不能报错。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# 并发写等待时间：SQLite 拿不到写锁时默认立刻抛 database is locked，
# 5 秒的等待窗口足够扛住一次慢查询造成的锁竞争。
BUSY_TIMEOUT_MS = 5000


def connect(db_path: str | Path, *, row_factory: bool = True) -> sqlite3.Connection:
    """打开一个配置正确的 SQLite 连接。

    `row_factory=True` 时返回的行支持 `row["column"]` 取值。
    四个存储模块全都需要这个行为（都用过 `row["xxx"]` 写法），
    保留参数只是为了测试时能关掉。
    """
    path_str = str(db_path)
    conn = sqlite3.connect(path_str, timeout=BUSY_TIMEOUT_MS / 1000)

    if row_factory:
        conn.row_factory = sqlite3.Row

    # 内存库没有文件，WAL 无处可写，设了会静默无效（某些版本直接报错）
    if path_str != ":memory:" and not path_str.startswith("file::memory:"):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # SQLite 默认不校验外键，建表时写了 REFERENCES 也拦不住脏数据
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def journal_mode(db_path: str | Path) -> str:
    """读回当前库实际生效的 journal 模式（验收用）。

    WAL 是**持久化在库文件里**的设置，设过一次就一直有效；
    但新库第一次连接前读到的还是 `delete`。验收脚本用它证明优化真的生效了，
    而不是"我以为我设了"。
    """
    with connect(db_path) as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    return row[0] if row else "unknown"
