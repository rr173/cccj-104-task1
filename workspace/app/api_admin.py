"""运营端 API：镜像上传、活动编排、逐级放量、暂停/恢复/终止。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from pydantic import BaseModel, Field

from .db import tx
from .deps import get_service
from .service import OtaService

router = APIRouter(prefix="/admin", tags=["admin"])


class StageSpec(BaseModel):
    hw_batches: list[str] = Field(min_length=1)
    quota: int = Field(gt=0)


class CampaignSpec(BaseModel):
    firmware_id: str
    name: str
    fail_threshold: float | None = None
    min_samples: int | None = None
    stages: list[StageSpec] = Field(min_length=1)


@router.post("/firmwares", status_code=201)
async def upload_firmware(
    request: Request,
    file: UploadFile,
    model: str = Form(...),
    target_version: str = Form(...),
    min_bootloader: str = Form(...),
    allowed_from: str = Form(default=""),  # 逗号分隔；空 = 任意旧版本
    svc: OtaService = Depends(get_service),
):
    data = await file.read()
    allowed = [v.strip() for v in allowed_from.split(",") if v.strip()]
    db = svc.db
    with tx(db):
        return svc.register_firmware(model, target_version, min_bootloader, allowed, data)


@router.get("/firmwares/{firmware_id}")
def get_firmware(firmware_id: str, svc: OtaService = Depends(get_service)):
    return svc.get_firmware(firmware_id)


@router.post("/campaigns", status_code=201)
def create_campaign(spec: CampaignSpec, request: Request, svc: OtaService = Depends(get_service)):
    settings = request.app.state.settings
    with tx(svc.db):
        return svc.create_campaign(
            firmware_id=spec.firmware_id,
            name=spec.name,
            fail_threshold=spec.fail_threshold
            if spec.fail_threshold is not None
            else settings.default_fail_threshold,
            min_samples=spec.min_samples
            if spec.min_samples is not None
            else settings.default_min_samples,
            stages=[s.model_dump() for s in spec.stages],
        )


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, svc: OtaService = Depends(get_service)):
    return svc.get_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/start")
def start_campaign(campaign_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.start_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/stages/{seq}/activate")
def activate_stage(campaign_id: str, seq: int, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.activate_stage(campaign_id, seq)


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.pause_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.resume_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/stop")
def stop_campaign(campaign_id: str, svc: OtaService = Depends(get_service)):
    with tx(svc.db):
        return svc.stop_campaign(campaign_id)
