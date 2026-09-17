"""幂等性：重复签到、重复领取、重复回执都不产生副作用。"""

from conftest import (
    checkin,
    claim,
    create_campaign,
    drive_to_critical,
    make_image,
    upload_firmware,
)


def _setup(client, quota=10):
    image = make_image()
    fw = upload_firmware(client, image=image)
    cid = create_campaign(client, fw, stages=[{"hw_batches": ["hw-1"], "quota": quota}])
    return image, cid


def _granted(client, cid):
    return client.get(f"/admin/campaigns/{cid}").json()["stages"][0]["granted"]


def test_repeated_checkin_is_idempotent(client):
    _setup(client)
    for _ in range(3):
        r = checkin(client, "dev-i1")
        assert r.status_code == 200


def test_repeated_claim_reuses_lease_and_quota(client):
    _, cid = _setup(client)
    checkin(client, "dev-i2")

    first = claim(client, "dev-i2")
    assert first.status_code == 201
    lid = first.json()["lease_id"]
    assert first.json()["reused"] is False
    assert _granted(client, cid) == 1

    # 断网重连后重复领取：同一个 lease，名额不增加
    for _ in range(3):
        again = claim(client, "dev-i2")
        assert again.status_code == 201
        assert again.json()["lease_id"] == lid
        assert again.json()["reused"] is True
    assert _granted(client, cid) == 1


def test_repeated_receipt_is_idempotent(client):
    image, cid = _setup(client)
    checkin(client, "dev-i3")
    lid = drive_to_critical(client, "dev-i3", image)

    body = {"receipt_id": "rcpt-i3", "outcome": "committed"}
    first = client.post(f"/device/leases/{lid}/result", json=body)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False

    # 网络重试重复上报：返回首次结果，计数不变
    for _ in range(3):
        dup = client.post(f"/device/leases/{lid}/result", json=body)
        assert dup.status_code == 200
        assert dup.json()["duplicate"] is True

    stage = client.get(f"/admin/campaigns/{cid}").json()["stages"][0]
    assert stage["succeeded"] == 1
    assert stage["failed"] == 0


def test_conflicting_receipt_after_finalize_is_rejected(client):
    image, _ = _setup(client)
    checkin(client, "dev-i4")
    lid = drive_to_critical(client, "dev-i4", image)

    ok = client.post(
        f"/device/leases/{lid}/result",
        json={"receipt_id": "rcpt-i4", "outcome": "committed"},
    )
    assert ok.status_code == 200

    # 换一个 receipt_id 对已终态的 lease 再报：不是幂等重放，必须拒绝
    conflict = client.post(
        f"/device/leases/{lid}/result",
        json={"receipt_id": "rcpt-i4-other", "outcome": "rolled_back", "reason_code": "E_X"},
    )
    assert conflict.status_code == 409


def test_rolled_back_requires_reason(client):
    image, _ = _setup(client)
    checkin(client, "dev-i5")
    lid = drive_to_critical(client, "dev-i5", image)
    r = client.post(
        f"/device/leases/{lid}/result",
        json={"receipt_id": "rcpt-i5", "outcome": "rolled_back"},
    )
    assert r.status_code == 400  # 回滚必须上报原因
