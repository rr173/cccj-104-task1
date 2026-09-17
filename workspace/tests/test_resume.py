"""断点续传：分块清单、逐块校验、中断后只补缺失块。"""

import hashlib

from conftest import CHUNK, checkin, chunk_hashes, claim, create_campaign, make_image, upload_firmware


def _setup(client, nbytes=200):
    image = make_image(nbytes)
    fw = upload_firmware(client, image=image)
    create_campaign(client, fw)
    checkin(client, "dev-dl")
    lid = claim(client, "dev-dl").json()["lease_id"]
    return image, lid


def test_manifest_lists_verified_chunks(client):
    image, lid = _setup(client)
    m = client.get(f"/device/leases/{lid}/manifest").json()
    assert m["sha256"] == hashlib.sha256(image).hexdigest()
    assert m["chunk_size"] == CHUNK
    assert [c["sha256"] for c in m["chunks"]] == chunk_hashes(image)
    assert sum(c["size"] for c in m["chunks"]) == len(image)


def test_resume_returns_only_missing_chunks(client):
    image, lid = _setup(client)
    # 模拟设备已下载并校验通过第 0、1 块后断线
    r = client.post(f"/device/leases/{lid}/resume", json={"verified": [0, 1]})
    assert r.status_code == 200
    body = r.json()
    assert [c["index"] for c in body["missing"]] == [2, 3]
    assert body["complete"] is False

    # 补完剩余块后再次 resume，应报告完整
    r = client.post(f"/device/leases/{lid}/resume", json={"verified": [0, 1, 2, 3]})
    assert r.json()["complete"] is True


def test_chunk_payload_matches_source_bytes(client):
    image, lid = _setup(client)
    blob = b""
    for i in range(len(chunk_hashes(image))):
        r = client.get(f"/device/leases/{lid}/chunks/{i}")
        assert r.headers["X-Chunk-SHA256"] == chunk_hashes(image)[i]
        blob += r.content
    assert blob == image  # 拼回完整镜像


def test_download_complete_requires_full_coverage(client):
    image, lid = _setup(client)
    client.get(f"/device/leases/{lid}/chunks/0")  # 进入 DOWNLOADING
    r = client.post(f"/device/leases/{lid}/download-complete", json={"verified": [0, 1, 2]})
    assert r.status_code == 400  # 缺一块，不允许谎称完成
    r = client.post(f"/device/leases/{lid}/download-complete", json={"verified": [0, 1, 2, 3]})
    assert r.status_code == 200
    assert r.json()["state"] == "DOWNLOADED"
