"""失败率熔断：越过阈值自动停止扩散；幂等：重复上线/重复回执不重复占名额。"""

from conftest import (
    checkin,
    claim,
    create_campaign,
    drive_to_critical,
    make_image,
    upload_firmware,
)


def _setup(client, fail_threshold=0.5, min_samples=2, quota=100):
    image = make_image()
    fw = upload_firmware(client, image=image)
    cid = create_campaign(
        client, fw,
        stages=[{"hw_batches": ["hw-1"], "quota": quota}],
        fail_threshold=fail_threshold,
        min_samples=min_samples,
    )
    return image, cid


def _fail_device(client, device_id, image, receipt_id):
    checkin(client, device_id)
    lid = drive_to_critical(client, device_id, image)
    return client.post(
        f"/device/leases/{lid}/result",
        json={
            "receipt_id": receipt_id,
            "outcome": "rolled_back",
            "reason_code": "E_FLASH",
            "detail": "write verify mismatch",
        },
    )


def test_breaker_trips_when_failure_rate_exceeds_threshold(client):
    image, cid = _setup(client, fail_threshold=0.5, min_samples=2)

    r = _fail_device(client, "dev-f1", image, "rcpt-f1")
    assert r.json()["breaker_tripped"] is False  # 样本不足 2，暂不熔断

    r = _fail_device(client, "dev-f2", image, "rcpt-f2")
    assert r.json()["breaker_tripped"] is True   # 2/2 失败 ≥ 50%，熔断

    camp = client.get(f"/admin/campaigns/{cid}").json()
    assert camp["state"] == "paused"             # 活动被自动暂停
    assert camp["stages"][0]["state"] == "stopped"

    # 扩散已停止：新设备无法领取
    checkin(client, "dev-f3")
    assert claim(client, "dev-f3").status_code == 404


def test_breaker_not_tripped_below_threshold(client):
    image, cid = _setup(client, fail_threshold=0.6, min_samples=2)

    _fail_device(client, "dev-g1", image, "rcpt-g1")
    checkin(client, "dev-g2")
    lid = drive_to_critical(client, "dev-g2", image)
    r = client.post(
        f"/device/leases/{lid}/result",
        json={"receipt_id": "rcpt-g2", "outcome": "committed"},
    )
    assert r.json()["breaker_tripped"] is False  # 1/2 = 50% < 60%，不熔断

    camp = client.get(f"/admin/campaigns/{cid}").json()
    assert camp["state"] == "running"
    assert camp["stages"][0]["state"] == "active"
