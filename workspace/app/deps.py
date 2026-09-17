"""请求级依赖：每请求一条 SQLite 连接，结束自动关闭。"""
from __future__ import annotations

from fastapi import Request

from .db import connect
from .service import OtaService


def get_service(request: Request):
    settings = request.app.state.settings
    db = connect(settings.db_path)
    try:
        yield OtaService(db, settings.firmware_dir, settings.chunk_size)
    finally:
        db.close()
