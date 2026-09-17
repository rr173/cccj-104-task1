"""SQLite 持久层。

设计要点：
- isolation_level=None（自动提交），所有多步写入都包在 tx() 的
  BEGIN IMMEDIATE ... COMMIT 里，SQLite 单写者保证串行，名额占位、
  熔断判定等并发敏感操作因此天然原子。
- 幂等由唯一约束兜底：leases(campaign_id, device_id) 防止重复上线
  重复占名额；receipts(receipt_id) 防止重复回执重复计数。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,
    model           TEXT NOT NULL,
    hw_batch        TEXT NOT NULL,
    bootloader      TEXT NOT NULL,
    current_version TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS firmwares (
    firmware_id    TEXT PRIMARY KEY,
    model          TEXT NOT NULL,
    target_version TEXT NOT NULL,
    min_bootloader TEXT NOT NULL,
    allowed_from   TEXT NOT NULL,          -- JSON 数组；空数组 = 任意旧版本
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    chunk_size     INTEGER NOT NULL,
    chunks         TEXT NOT NULL,          -- JSON: [{index, size, sha256}]
    storage_path   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id     TEXT PRIMARY KEY,
    firmware_id     TEXT NOT NULL REFERENCES firmwares,
    name            TEXT NOT NULL,
    fail_threshold  REAL NOT NULL,         -- 失败率阈值，如 0.05
    min_samples     INTEGER NOT NULL,      -- 熔断生效的最小样本数
    state           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stages (
    stage_id    TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns,
    seq         INTEGER NOT NULL,
    hw_batches  TEXT NOT NULL,             -- JSON 数组：本批覆盖的硬件批次
    quota       INTEGER NOT NULL,          -- 本批名额
    granted     INTEGER NOT NULL DEFAULT 0,
    succeeded   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stages_campaign ON stages(campaign_id, seq);

CREATE TABLE IF NOT EXISTS leases (
    lease_id    TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns,
    stage_id    TEXT NOT NULL REFERENCES stages,
    device_id   TEXT NOT NULL REFERENCES devices,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (campaign_id, device_id)        -- 幂等：重复领取不重复占名额
);
CREATE INDEX IF NOT EXISTS idx_leases_device ON leases(device_id);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id  TEXT PRIMARY KEY,          -- 客户端生成的幂等键
    lease_id    TEXT NOT NULL REFERENCES leases,
    outcome     TEXT NOT NULL,
    reason_code TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    # check_same_thread=False：FastAPI 的依赖注入与路由可能跑在线程池的不同线程；
    # 每条连接仍只服务一个请求，安全性由请求级连接 + WAL + busy_timeout 保证
    db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def init_db(db_path: str) -> None:
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
    finally:
        db.close()


@contextmanager
def tx(db: sqlite3.Connection):
    """多步写入的唯一入口：BEGIN IMMEDIATE 保证读写事务串行化。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")
