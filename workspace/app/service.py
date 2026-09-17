"""核心业务逻辑。所有公共方法假定调用方已持有 tx() 事务。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from .domain import (
    GATED_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    CampaignState,
    LeaseState,
    Outcome,
    StageState,
)
from .versioning import version_eq, version_gte


class ApiError(Exception):
    status = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


class BadRequest(ApiError):
    status = 400


class GateClosed(ApiError):
    """批次已暂停/熔断：设备不得继续前进，但可稍后重试。"""

    status = 423  # Locked


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


class OtaService:
    def __init__(self, db: sqlite3.Connection, firmware_dir: str, chunk_size: int):
        self.db = db
        self.firmware_dir = firmware_dir
        self.chunk_size = chunk_size

    # ------------------------------------------------------------------ #
    # 运营端：固件 / 活动 / 放量                                           #
    # ------------------------------------------------------------------ #

    def register_firmware(
        self,
        model: str,
        target_version: str,
        min_bootloader: str,
        allowed_from: list[str],
        data: bytes,
    ) -> dict:
        """接收镜像字节流，切块、逐块算 SHA256 后落盘并登记。"""
        firmware_id = uuid.uuid4().hex
        total_sha = hashlib.sha256(data).hexdigest()
        chunks = []
        store_dir = os.path.join(self.firmware_dir, firmware_id)
        os.makedirs(store_dir, exist_ok=True)
        for index, off in enumerate(range(0, max(len(data), 1), self.chunk_size)):
            blob = data[off : off + self.chunk_size]
            if not blob and data:
                break
            chunks.append(
                {
                    "index": index,
                    "size": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
            with open(os.path.join(store_dir, f"{index:06d}.chunk"), "wb") as f:
                f.write(blob)
            if not data:
                break
        self.db.execute(
            """INSERT INTO firmwares
               (firmware_id, model, target_version, min_bootloader, allowed_from,
                size, sha256, chunk_size, chunks, storage_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                firmware_id,
                model,
                target_version,
                min_bootloader,
                json.dumps(allowed_from),
                len(data),
                total_sha,
                self.chunk_size,
                json.dumps(chunks),
                store_dir,
                now(),
            ),
        )
        return self.get_firmware(firmware_id)

    def get_firmware(self, firmware_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM firmwares WHERE firmware_id=?", (firmware_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"firmware {firmware_id} not found")
        d = _row_to_dict(row)
        d["allowed_from"] = json.loads(d["allowed_from"])
        d["chunks"] = json.loads(d["chunks"])
        return d

    def create_campaign(
        self,
        firmware_id: str,
        name: str,
        fail_threshold: float,
        min_samples: int,
        stages: list[dict],
    ) -> dict:
        self.get_firmware(firmware_id)  # 校验存在
        if not stages:
            raise BadRequest("campaign needs at least one stage")
        if not (0 < fail_threshold < 1):
            raise BadRequest("fail_threshold must be in (0, 1)")
        campaign_id = uuid.uuid4().hex
        self.db.execute(
            """INSERT INTO campaigns
               (campaign_id, firmware_id, name, fail_threshold, min_samples, state, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                campaign_id,
                firmware_id,
                name,
                fail_threshold,
                min_samples,
                CampaignState.DRAFT.value,
                now(),
            ),
        )
        for seq, st in enumerate(stages):
            if st["quota"] <= 0:
                raise BadRequest("stage quota must be positive")
            self.db.execute(
                """INSERT INTO stages
                   (stage_id, campaign_id, seq, hw_batches, quota, state)
                   VALUES (?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    campaign_id,
                    seq,
                    json.dumps(st["hw_batches"]),
                    st["quota"],
                    StageState.PENDING.value,
                ),
            )
        return self.get_campaign(campaign_id)

    def get_campaign(self, campaign_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"campaign {campaign_id} not found")
        c = _row_to_dict(row)
        stages = []
        for s in self.db.execute(
            "SELECT * FROM stages WHERE campaign_id=? ORDER BY seq", (campaign_id,)
        ):
            d = _row_to_dict(s)
            d["hw_batches"] = json.loads(d["hw_batches"])
            total = d["succeeded"] + d["failed"]
            d["failure_rate"] = (d["failed"] / total) if total else 0.0
            stages.append(d)
        c["stages"] = stages
        return c

    def start_campaign(self, campaign_id: str) -> dict:
        c = self.get_campaign(campaign_id)
        if c["state"] != CampaignState.DRAFT.value:
            raise Conflict(f"cannot start campaign in state {c['state']}")
        self.db.execute(
            "UPDATE campaigns SET state=? WHERE campaign_id=?",
            (CampaignState.RUNNING.value, campaign_id),
        )
        # 激活第一级放量
        self.db.execute(
            """UPDATE stages SET state=? WHERE campaign_id=? AND seq=
               (SELECT MIN(seq) FROM stages WHERE campaign_id=?)""",
            (StageState.ACTIVE.value, campaign_id, campaign_id),
        )
        return self.get_campaign(campaign_id)

    def activate_stage(self, campaign_id: str, seq: int) -> dict:
        """开启下一级放量（前一级不必结束，允许交叠灰度）。"""
        c = self.get_campaign(campaign_id)
        if c["state"] != CampaignState.RUNNING.value:
            raise Conflict(f"campaign not running (state={c['state']})")
        cur = self.db.execute(
            "UPDATE stages SET state=? WHERE campaign_id=? AND seq=? AND state=?",
            (StageState.ACTIVE.value, campaign_id, seq, StageState.PENDING.value),
        )
        if cur.rowcount == 0:
            raise Conflict(f"stage {seq} cannot be activated")
        return self.get_campaign(campaign_id)

    def pause_campaign(self, campaign_id: str) -> dict:
        """人工暂停：立即关上闸门，未进入安装阶段的设备全部停住。"""
        cur = self.db.execute(
            "UPDATE campaigns SET state=? WHERE campaign_id=? AND state=?",
            (CampaignState.PAUSED.value, campaign_id, CampaignState.RUNNING.value),
        )
        if cur.rowcount == 0:
            raise Conflict("campaign is not running")
        self.db.execute(
            "UPDATE stages SET state=? WHERE campaign_id=? AND state=?",
            (StageState.PAUSED.value, campaign_id, StageState.ACTIVE.value),
        )
        return self.get_campaign(campaign_id)

    def resume_campaign(self, campaign_id: str) -> dict:
        cur = self.db.execute(
            "UPDATE campaigns SET state=? WHERE campaign_id=? AND state=?",
            (CampaignState.RUNNING.value, campaign_id, CampaignState.PAUSED.value),
        )
        if cur.rowcount == 0:
            raise Conflict("campaign is not paused")
        # 只恢复被人工暂停的批；被熔断 STOPPED 的批保持停止
        self.db.execute(
            "UPDATE stages SET state=? WHERE campaign_id=? AND state=?",
            (StageState.ACTIVE.value, campaign_id, StageState.PAUSED.value),
        )
        return self.get_campaign(campaign_id)

    def stop_campaign(self, campaign_id: str) -> dict:
        """终止活动：未进入关键区的 lease 安全中止；INSTALL_CRITICAL 的保留，等待收尾回执。"""
        c = self.get_campaign(campaign_id)
        if c["state"] in (CampaignState.STOPPED.value, CampaignState.DONE.value):
            raise Conflict(f"campaign already {c['state']}")
        self.db.execute(
            "UPDATE campaigns SET state=? WHERE campaign_id=?",
            (CampaignState.STOPPED.value, campaign_id),
        )
        self.db.execute(
            "UPDATE stages SET state=? WHERE campaign_id=? AND state IN (?,?,?)",
            (
                StageState.STOPPED.value,
                campaign_id,
                StageState.ACTIVE.value,
                StageState.PAUSED.value,
                StageState.PENDING.value,
            ),
        )
        self.db.execute(
            """UPDATE leases SET state=?, updated_at=?
               WHERE campaign_id=? AND state IN (?,?,?,?)""",
            (
                LeaseState.ABORTED.value,
                now(),
                campaign_id,
                LeaseState.OFFERED.value,
                LeaseState.DOWNLOADING.value,
                LeaseState.DOWNLOADED.value,
                LeaseState.INSTALL_PREP.value,
            ),
        )
        return self.get_campaign(campaign_id)

    # ------------------------------------------------------------------ #
    # 终端端：签到 / 领取 / 下载 / 安装 / 回执                              #
    # ------------------------------------------------------------------ #

    def checkin(self, device: dict) -> dict:
        """幂等签到：重复上线只做 upsert，不产生任何名额占用。"""
        self.db.execute(
            """INSERT INTO devices (device_id, model, hw_batch, bootloader, current_version, last_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                   model=excluded.model, hw_batch=excluded.hw_batch,
                   bootloader=excluded.bootloader,
                   current_version=excluded.current_version, last_seen=excluded.last_seen""",
            (
                device["device_id"],
                device["model"],
                device["hw_batch"],
                device["bootloader"],
                device["current_version"],
                now(),
            ),
        )
        return {"device_id": device["device_id"], "registered": True}

    def claim(self, device_id: str) -> dict:
        """领取升级名额。幂等：同一设备在同一活动下永远只有一个 lease。

        名额占位用一条条件 UPDATE 完成（granted < quota 时才 +1），
        配合 leases(campaign_id, device_id) 唯一约束，并发与重试都不会超发。
        """
        dev = self.db.execute(
            "SELECT * FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if not dev:
            raise NotFound("device not registered; checkin first")

        # 幂等重放：已有 lease 直接返回，绝不重新占位
        existing = self.db.execute(
            """SELECT l.*, s.state AS stage_state FROM leases l
               JOIN stages s ON s.stage_id=l.stage_id
               WHERE l.device_id=?""",
            (device_id,),
        ).fetchone()
        if existing:
            return self._lease_payload(existing, reused=True)

        for c in self.db.execute(
            "SELECT * FROM campaigns WHERE state=?", (CampaignState.RUNNING.value,)
        ):
            fw = self.get_firmware(c["firmware_id"])
            if not self._compatible(dev, fw):
                continue
            for st in self.db.execute(
                "SELECT * FROM stages WHERE campaign_id=? AND state=? ORDER BY seq",
                (c["campaign_id"], StageState.ACTIVE.value),
            ):
                if dev["hw_batch"] not in json.loads(st["hw_batches"]):
                    continue
                cur = self.db.execute(
                    """UPDATE stages SET granted=granted+1
                       WHERE stage_id=? AND state=? AND granted < quota""",
                    (st["stage_id"], StageState.ACTIVE.value),
                )
                if cur.rowcount == 0:
                    continue  # 名额已满或刚被暂停，尝试下一级
                lease_id = uuid.uuid4().hex
                try:
                    self.db.execute(
                        """INSERT INTO leases
                           (lease_id, campaign_id, stage_id, device_id, state, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            lease_id,
                            c["campaign_id"],
                            st["stage_id"],
                            device_id,
                            LeaseState.OFFERED.value,
                            now(),
                            now(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    # 极端并发下另一请求先插入了：退回名额并返回已有 lease
                    self.db.execute(
                        "UPDATE stages SET granted=granted-1 WHERE stage_id=?",
                        (st["stage_id"],),
                    )
                    existing = self.db.execute(
                        """SELECT l.*, s.state AS stage_state FROM leases l
                           JOIN stages s ON s.stage_id=l.stage_id
                           WHERE l.device_id=?""",
                        (device_id,),
                    ).fetchone()
                    return self._lease_payload(existing, reused=True)
                row = self.db.execute(
                    """SELECT l.*, s.state AS stage_state FROM leases l
                       JOIN stages s ON s.stage_id=l.stage_id WHERE l.lease_id=?""",
                    (lease_id,),
                ).fetchone()
                return self._lease_payload(row, reused=False)
        raise NotFound("no compatible offer for this device")

    @staticmethod
    def _compatible(dev: sqlite3.Row, fw: dict) -> bool:
        """型号、引导程序、当前版本三重兼容性校验。"""
        if fw["model"] != dev["model"]:
            return False
        if not version_gte(dev["bootloader"], fw["min_bootloader"]):
            return False
        if version_eq(dev["current_version"], fw["target_version"]):
            return False  # 已是目标版本
        allowed = fw["allowed_from"]
        if allowed and dev["current_version"] not in allowed:
            return False  # 不在允许的升级路径上
        return True

    def _lease_payload(self, lease: sqlite3.Row, reused: bool) -> dict:
        c = self.db.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?", (lease["campaign_id"],)
        ).fetchone()
        fw = self.get_firmware(c["firmware_id"])
        return {
            "lease_id": lease["lease_id"],
            "campaign_id": lease["campaign_id"],
            "device_id": lease["device_id"],
            "state": lease["state"],
            "reused": reused,
            "firmware": {
                "firmware_id": fw["firmware_id"],
                "target_version": fw["target_version"],
                "size": fw["size"],
                "sha256": fw["sha256"],
                "chunk_size": fw["chunk_size"],
                "chunk_count": len(fw["chunks"]),
            },
        }

    # ------------------------- 下载（断点续传） ------------------------- #

    def manifest(self, lease_id: str) -> dict:
        lease = self._get_lease(lease_id)
        self._enforce_gate(lease)
        fw = self._lease_firmware(lease)
        return {
            "lease_id": lease_id,
            "firmware_id": fw["firmware_id"],
            "target_version": fw["target_version"],
            "size": fw["size"],
            "sha256": fw["sha256"],
            "chunk_size": fw["chunk_size"],
            "chunks": fw["chunks"],
        }

    def chunk_path(self, lease_id: str, index: int) -> tuple[str, str]:
        """返回分块文件路径与预期哈希；首次拉块时把 lease 推进到 DOWNLOADING。"""
        lease = self._get_lease(lease_id)
        self._enforce_gate(lease)
        fw = self._lease_firmware(lease)
        chunks = fw["chunks"]
        if index < 0 or index >= len(chunks):
            raise NotFound(f"chunk {index} out of range")
        if lease["state"] == LeaseState.OFFERED.value:
            self._transition(lease, LeaseState.DOWNLOADING)
        path = os.path.join(fw["storage_path"], f"{index:06d}.chunk")
        return path, chunks[index]["sha256"]

    def resume(self, lease_id: str, verified: list[int]) -> dict:
        """设备重连后上报本地已校验通过的分块，服务端只下发缺失清单。"""
        lease = self._get_lease(lease_id)
        self._enforce_gate(lease)
        fw = self._lease_firmware(lease)
        have = set(verified)
        missing = [c for c in fw["chunks"] if c["index"] not in have]
        return {
            "lease_id": lease_id,
            "received_chunks": sorted(have),
            "missing": missing,
            "complete": not missing,
        }

    def download_complete(self, lease_id: str, verified: list[int]) -> dict:
        """设备声明全部数据块已在本地校验通过，进入待安装状态。"""
        lease = self._get_lease(lease_id)
        self._enforce_gate(lease)
        fw = self._lease_firmware(lease)
        expected = {c["index"] for c in fw["chunks"]}
        if set(verified) != expected:
            raise BadRequest("verified chunk set does not cover the whole image")
        if lease["state"] == LeaseState.DOWNLOADING.value:
            self._transition(lease, LeaseState.DOWNLOADED)
        elif lease["state"] != LeaseState.DOWNLOADED.value:
            raise Conflict(f"cannot complete download from state {lease['state']}")
        return {"lease_id": lease_id, "state": LeaseState.DOWNLOADED.value}

    # ------------------------- 安装（闸门 + 收尾） ------------------------- #

    def install_begin(self, lease_id: str) -> dict:
        """进入安装准备（尚未写关键区）。闸门：批次暂停/熔断时拒绝。"""
        lease = self._get_lease(lease_id)
        self._enforce_gate(lease)
        self._transition(lease, LeaseState.INSTALL_PREP)
        return {"lease_id": lease_id, "state": LeaseState.INSTALL_PREP.value}

    def install_critical(self, lease_id: str) -> dict:
        """即将写入关键区（A/B 分区切换点）——暂停后此闸门关闭。

        一旦进入 INSTALL_CRITICAL，设备必须走完 committed/rolled_back，
        此后活动暂停、停止、熔断都不再拦截它的回执。
        """
        lease = self._get_lease(lease_id)
        self._enforce_gate(lease)
        self._transition(lease, LeaseState.INSTALL_CRITICAL)
        return {"lease_id": lease_id, "state": LeaseState.INSTALL_CRITICAL.value}

    def report_result(
        self,
        lease_id: str,
        receipt_id: str,
        outcome: str,
        reason_code: str | None,
        detail: str | None,
    ) -> dict:
        """安装回执。幂等：receipt_id 唯一，重复回执返回首次处理结果。

        此端点不做闸门检查——已写入关键区的设备必须能安全收尾。
        """
        lease = self._get_lease(lease_id)
        if outcome == Outcome.ROLLED_BACK.value and not reason_code:
            raise BadRequest("rolled_back requires reason_code")
        if outcome not in (Outcome.COMMITTED.value, Outcome.ROLLED_BACK.value):
            raise BadRequest(f"unknown outcome {outcome}")

        # 幂等：同一 receipt_id 直接重放首次结果
        dup = self.db.execute(
            "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if dup:
            if dup["lease_id"] != lease_id:
                raise Conflict("receipt_id already used by another lease")
            return {
                "lease_id": lease_id,
                "receipt_id": receipt_id,
                "outcome": dup["outcome"],
                "duplicate": True,
            }

        if lease["state"] in (LeaseState.COMMITTED.value, LeaseState.ROLLED_BACK.value):
            raise Conflict(f"lease already finalized as {lease['state']}")
        if lease["state"] not in (
            LeaseState.INSTALL_PREP.value,
            LeaseState.INSTALL_CRITICAL.value,
        ):
            raise Conflict(f"cannot report result from state {lease['state']}")

        final = (
            LeaseState.COMMITTED if outcome == Outcome.COMMITTED.value else LeaseState.ROLLED_BACK
        )
        self._transition(lease, final)
        try:
            self.db.execute(
                """INSERT INTO receipts (receipt_id, lease_id, outcome, reason_code, detail, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (receipt_id, lease_id, outcome, reason_code, detail, now()),
            )
        except sqlite3.IntegrityError:
            # 并发重试撞唯一键：按幂等重放处理
            dup = self.db.execute(
                "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            return {
                "lease_id": lease_id,
                "receipt_id": receipt_id,
                "outcome": dup["outcome"],
                "duplicate": True,
            }

        counter = "succeeded" if final == LeaseState.COMMITTED else "failed"
        self.db.execute(
            f"UPDATE stages SET {counter}={counter}+1 WHERE stage_id=?",
            (lease["stage_id"],),
        )
        tripped = self._maybe_trip_breaker(lease)
        return {
            "lease_id": lease_id,
            "receipt_id": receipt_id,
            "outcome": outcome,
            "duplicate": False,
            "breaker_tripped": tripped,
        }

    # ------------------------------------------------------------------ #
    # 内部工具                                                             #
    # ------------------------------------------------------------------ #

    def _maybe_trip_breaker(self, lease: sqlite3.Row) -> bool:
        """失败率越过阈值则熔断：停掉当前批并暂停整个活动，停止扩散。"""
        st = self.db.execute(
            "SELECT * FROM stages WHERE stage_id=?", (lease["stage_id"],)
        ).fetchone()
        c = self.db.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?", (lease["campaign_id"],)
        ).fetchone()
        total = st["succeeded"] + st["failed"]
        if total >= c["min_samples"] and st["failed"] / total >= c["fail_threshold"]:
            self.db.execute(
                "UPDATE stages SET state=? WHERE stage_id=? AND state IN (?,?)",
                (StageState.STOPPED.value, st["stage_id"], StageState.ACTIVE.value, StageState.PAUSED.value),
            )
            self.db.execute(
                "UPDATE campaigns SET state=? WHERE campaign_id=? AND state=?",
                (CampaignState.PAUSED.value, c["campaign_id"], CampaignState.RUNNING.value),
            )
            # 其余仍在放量的批一并暂停，等待人工定夺
            self.db.execute(
                "UPDATE stages SET state=? WHERE campaign_id=? AND state=?",
                (StageState.PAUSED.value, c["campaign_id"], StageState.ACTIVE.value),
            )
            return True
        return False

    def _get_lease(self, lease_id: str) -> sqlite3.Row:
        row = self.db.execute(
            """SELECT l.*, s.state AS stage_state FROM leases l
               JOIN stages s ON s.stage_id=l.stage_id WHERE l.lease_id=?""",
            (lease_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"lease {lease_id} not found")
        return row

    def _lease_firmware(self, lease: sqlite3.Row) -> dict:
        c = self.db.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?", (lease["campaign_id"],)
        ).fetchone()
        return self.get_firmware(c["firmware_id"])

    def _enforce_gate(self, lease: sqlite3.Row) -> None:
        """暂停/熔断闸门：仅拦截尚未进入关键区的设备。"""
        if lease["state"] in TERMINAL_STATES:
            raise Conflict(f"lease already finalized as {lease['state']}")
        if lease["state"] not in {s.value for s in GATED_STATES}:
            return  # INSTALL_CRITICAL：必须放行去收尾
        c = self.db.execute(
            "SELECT state FROM campaigns WHERE campaign_id=?", (lease["campaign_id"],)
        ).fetchone()
        if c["state"] != CampaignState.RUNNING.value or lease["stage_state"] != StageState.ACTIVE.value:
            raise GateClosed(
                f"rollout gated: campaign={c['state']} stage={lease['stage_state']}"
            )

    def _transition(self, lease: sqlite3.Row, target: LeaseState) -> None:
        """条件更新实现状态机：非法迁移在并发下安全失败。"""
        current = LeaseState(lease["state"])
        allowed = {s.value for s in TRANSITIONS[current]}
        if target.value not in allowed:
            raise Conflict(f"illegal transition {current.value} -> {target.value}")
        cur = self.db.execute(
            "UPDATE leases SET state=?, updated_at=? WHERE lease_id=? AND state=?",
            (target.value, now(), lease["lease_id"], current.value),
        )
        if cur.rowcount == 0:
            raise Conflict(f"illegal transition {current.value} -> {target.value}")
