"""领域状态机。

设备侧一次升级的生命周期（lease 状态）：

    OFFERED ──首次拉块──▶ DOWNLOADING ──全部块校验通过──▶ DOWNLOADED
                                                            │ install/begin（闸门）
                                                            ▼
                                                       INSTALL_PREP
                                                            │ install/critical（闸门，写关键区前最后检查点）
                                                            ▼
                                                     INSTALL_CRITICAL ──result──▶ COMMITTED
                                                            │                   （安全收尾，不受暂停影响）
                                                            └──result──────────▶ ROLLED_BACK

    任意非安装阶段 + 活动停止 ──▶ ABORTED

闸门规则：活动被暂停/熔断后，OFFERED/DOWNLOADING/DOWNLOADED/INSTALL_PREP
一律不得前进；INSTALL_CRITICAL 的设备已经写入关键区，必须允许其上报
committed / rolled_back 完成安全收尾。
"""
from __future__ import annotations

from enum import Enum


class LeaseState(str, Enum):
    OFFERED = "OFFERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    INSTALL_PREP = "INSTALL_PREP"
    INSTALL_CRITICAL = "INSTALL_CRITICAL"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    ABORTED = "ABORTED"


TERMINAL_STATES = {LeaseState.COMMITTED, LeaseState.ROLLED_BACK, LeaseState.ABORTED}

# 允许的状态迁移表：{当前状态: {后继状态, ...}}
TRANSITIONS: dict[LeaseState, set[LeaseState]] = {
    LeaseState.OFFERED: {LeaseState.DOWNLOADING, LeaseState.ABORTED},
    LeaseState.DOWNLOADING: {LeaseState.DOWNLOADED, LeaseState.ABORTED},
    LeaseState.DOWNLOADED: {LeaseState.INSTALL_PREP, LeaseState.ABORTED},
    LeaseState.INSTALL_PREP: {
        LeaseState.INSTALL_CRITICAL,
        LeaseState.COMMITTED,      # 未写关键区即失败也算一次收尾
        LeaseState.ROLLED_BACK,
        LeaseState.ABORTED,
    },
    LeaseState.INSTALL_CRITICAL: {LeaseState.COMMITTED, LeaseState.ROLLED_BACK},
    LeaseState.COMMITTED: set(),
    LeaseState.ROLLED_BACK: set(),
    LeaseState.ABORTED: set(),
}

# 会被“暂停/熔断闸门”拦住的状态：这些状态下的设备不得继续前进
GATED_STATES = {
    LeaseState.OFFERED,
    LeaseState.DOWNLOADING,
    LeaseState.DOWNLOADED,
    LeaseState.INSTALL_PREP,
}

# 已写入关键区、暂停后仍必须安全收尾的状态
CRITICAL_STATES = {LeaseState.INSTALL_CRITICAL}


class CampaignState(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"      # 人工暂停或熔断自动暂停
    STOPPED = "stopped"    # 人工终止，不可恢复
    DONE = "done"


class StageState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"    # 熔断或人工终止
    DONE = "done"


class Outcome(str, Enum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
