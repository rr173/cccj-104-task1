"""应用装配：配置、建库、路由、统一错误响应。"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import api_admin, api_device
from .config import Settings
from .db import connect, init_db
from .service import ApiError, OtaService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    os.makedirs(settings.firmware_dir, exist_ok=True)
    init_db(settings.db_path)

    app = FastAPI(title="OTA Fleet Update Service", version="1.0.0")
    app.state.settings = settings

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": exc.message})

    app.include_router(api_admin.router)
    app.include_router(api_device.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app


app = create_app()
