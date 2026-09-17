import hashlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

CHUNK = 64  # 测试用小分块，便于构造多块镜像


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), chunk_size=CHUNK)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------- #
# 测试辅助                                                                 #
# --------------------------------------------------------------------- #

def make_image(nbytes: int = 200) -> bytes:
    return bytes((i * 7 + 13) % 256 for i in range(nbytes))


def chunk_hashes(image: bytes, chunk_size: int = CHUNK) -> list[str]:
    return [
        hashlib.sha256(image[off : off + chunk_size]).hexdigest()
        for off in range(0, len(image), chunk_size)
    ]


def upload_firmware(client, image=None, model="sensor-A", target="2.0.0",
                    min_bootloader="1.2", allowed_from="") -> str:
    image = image if image is not None else make_image()
    resp = client.post(
        "/admin/firmwares",
        files={"file": ("fw.bin", image, "application/octet-stream")},
        data={
            "model": model,
            "target_version": target,
            "min_bootloader": min_bootloader,
            "allowed_from": allowed_from,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["firmware_id"]


def create_campaign(client, firmware_id, stages=None, fail_threshold=0.5,
                    min_samples=2, start=True) -> str:
    stages = stages or [{"hw_batches": ["hw-1"], "quota": 100}]
    resp = client.post(
        "/admin/campaigns",
        json={
            "firmware_id": firmware_id,
            "name": "rollout",
            "fail_threshold": fail_threshold,
            "min_samples": min_samples,
            "stages": stages,
        },
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["campaign_id"]
    if start:
        assert client.post(f"/admin/campaigns/{cid}/start").status_code == 200
    return cid


def checkin(client, device_id, model="sensor-A", hw_batch="hw-1",
            bootloader="1.5", current_version="1.0.0"):
    resp = client.post(
        "/device/checkin",
        json={
            "device_id": device_id,
            "model": model,
            "hw_batch": hw_batch,
            "bootloader": bootloader,
            "current_version": current_version,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


def claim(client, device_id):
    return client.post("/device/claim", json={"device_id": device_id})


def drive_to_downloaded(client, device_id, image: bytes) -> str:
    """走完 领取→逐块下载→校验完成 的全流程，返回 lease_id。"""
    lease = claim(client, device_id).json()
    lid = lease["lease_id"]
    manifest = client.get(f"/device/leases/{lid}/manifest").json()
    for c in manifest["chunks"]:
        r = client.get(f"/device/leases/{lid}/chunks/{c['index']}")
        assert r.status_code == 200
        assert hashlib.sha256(r.content).hexdigest() == r.headers["X-Chunk-SHA256"]
    r = client.post(
        f"/device/leases/{lid}/download-complete",
        json={"verified": [c["index"] for c in manifest["chunks"]]},
    )
    assert r.status_code == 200, r.text
    return lid


def drive_to_critical(client, device_id, image: bytes) -> str:
    lid = drive_to_downloaded(client, device_id, image)
    assert client.post(f"/device/leases/{lid}/install/begin").status_code == 200
    assert client.post(f"/device/leases/{lid}/install/critical").status_code == 200
    return lid
