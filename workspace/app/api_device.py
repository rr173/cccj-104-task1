"""终端 API：签到、领取、断点续传下载、安装推进与回执。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .db import tx
from .deps import get_service
from .service import OtaService

router = APIRouter(prefix="/device", tags=["device"])


class CheckinBody(BaseModel):
    device_id: str
    model: str
    hw_batch: str
    bootloader: str
    current_version: str


class ClaimBody(BaseModel):
    device_id: str


class ResumeBody(BaseModel):
    verified: list[int] = Field(default_factory=list)


class ResultBody(BaseModel):
    receipt_id: str  # 客户端生成的幂等键（重试必须沿用同一个）
    outcome: str     # committed | rolled_back
    reason_code: str | None = None
    detail: str | None = None


@router.post("/checkin")
def checkin(body: CheckinBody, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.checkin(body.model_dump())


@router.post("/claim", status_code=201)
def claim(body: ClaimBody, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.claim(body.device_id)


@router.get("/leases/{lease_id}/manifest")
def manifest(lease_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.manifest(lease_id)


@router.get("/leases/{lease_id}/chunks/{index}")
def get_chunk(lease_id: str, index: int, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        path, sha = svc.chunk_path(lease_id, index)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"X-Chunk-Index": str(index), "X-Chunk-SHA256": sha},
    )


@router.post("/leases/{lease_id}/resume")
def resume(lease_id: str, body: ResumeBody, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.resume(lease_id, body.verified)


@router.post("/leases/{lease_id}/download-complete")
def download_complete(lease_id: str, body: ResumeBody, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.download_complete(lease_id, body.verified)


@router.post("/leases/{lease_id}/install/begin")
def install_begin(lease_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.install_begin(lease_id)


@router.post("/leases/{lease_id}/install/critical")
def install_critical(lease_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.install_critical(lease_id)


@router.post("/leases/{lease_id}/result")
def report_result(lease_id: str, body: ResultBody, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.report_result(
            lease_id,
            receipt_id=body.receipt_id,
            outcome=body.outcome,
            reason_code=body.reason_code,
            detail=body.detail,
        )
