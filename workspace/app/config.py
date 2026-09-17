"""运行时配置，全部可通过环境变量覆盖，便于容器化部署。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # 数据根目录：SQLite 库与固件分块都存放在这里（容器中挂载卷即可持久化）
    data_dir: str = field(default_factory=lambda: os.environ.get("OTA_DATA_DIR", "./data"))
    # 断点续传的分块大小（字节）
    chunk_size: int = field(default_factory=lambda: int(os.environ.get("OTA_CHUNK_SIZE", "262144")))
    # 默认熔断参数（创建活动时未显式指定则使用）
    default_fail_threshold: float = field(
        default_factory=lambda: float(os.environ.get("OTA_FAIL_THRESHOLD", "0.05"))
    )
    default_min_samples: int = field(
        default_factory=lambda: int(os.environ.get("OTA_MIN_SAMPLES", "20"))
    )

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "ota.db")

    @property
    def firmware_dir(self) -> str:
        return os.path.join(self.data_dir, "firmware")
