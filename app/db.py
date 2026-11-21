"""SQLite 存储层：连接、模式、事务助手。

- 数据落在 SQLite 文件中（DB_PATH 可配），重启后仍可查。
- WAL 模式：补录写入期间查询不被阻塞。
- 读事务（read_txn）保证一次查询内看到的实发与据此算出的区间来自同一次提交；
  写事务（write_txn）用 BEGIN IMMEDIATE，校验或写入失败整体回滚，保留原实发。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB_PATH = os.path.join(os.getcwd(), "data", "app.db")


def db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def horizon() -> int:
    """运营日分钟数上界（不含）。计划/实发分钟须落在 [0, horizon)。"""
    return int(os.environ.get("DAY_HORIZON_MINUTES", "2880"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS trips (
    day      TEXT NOT NULL REFERENCES days(day) ON DELETE CASCADE,
    trip_id  TEXT NOT NULL,
    planned  INTEGER NOT NULL,          -- 计划发车分钟
    topo     INTEGER NOT NULL,          -- 拓扑序（导入时计算，校验传播用）
    actual   INTEGER,                   -- 实发分钟，NULL 表示未补录/已撤销
    PRIMARY KEY (day, trip_id)
);
CREATE TABLE IF NOT EXISTS connections (
    day          TEXT NOT NULL REFERENCES days(day) ON DELETE CASCADE,
    pred         TEXT NOT NULL,         -- 前序班次
    succ         TEXT NOT NULL,         -- 本班
    min_interval INTEGER NOT NULL,      -- 最短接续间隔（分钟）
    PRIMARY KEY (day, pred, succ)
);
CREATE INDEX IF NOT EXISTS idx_connections_succ ON connections(day, succ);
CREATE INDEX IF NOT EXISTS idx_trips_day_actual ON trips(day, actual);
"""


def connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def read_txn(conn: sqlite3.Connection):
    """读事务：事务内所有 SELECT 看到同一快照（同一次提交）。"""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """写事务：立即取写锁；任何异常整体回滚，保留原数据。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
