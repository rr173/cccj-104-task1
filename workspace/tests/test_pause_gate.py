"""暂停语义：未进入安装阶段的设备全部停住；已写关键区的设备必须能安全收尾。"""

from conftest import (
    checkin,
    claim,
    create_campaign,
    drive_to_critical,
    drive_to_downloaded,
    make_image,
    upload_firmware,
)


def _campaign_with_image(client):
    image = make_image()
    fw = upload_firmware(client, image=image)
    cid = create_campaign(client, fw)
    return image, cid


def test_pause_blocks_download_and_install_entry(client):
    image, cid = _campaign_with_image(client)
    checkin(client, "dev-p1")
    lid = claim(client, "dev-p1").json()["lease_id"]

    assert client.post(f"/admin/campaigns/{cid}/pause").status_code == 200

    # 下载中的设备：不得继续拉块
    assert client.get(f"/device/leases/{lid}/chunks/0").status_code == 423
    assert client.get(f"/device/leases/{lid}/manifest").status_code == 423

    # 恢复后可以继续，且断点续传不受影响
    assert client.post(f"/admin/campaigns/{cid}/resume").status_code == 200
    assert client.get(f"/device/leases/{lid}/chunks/0").status_code == 200


def test_pause_blocks_install_begin_and_critical_write(client):
    image, cid = _campaign_with_image(client)
    checkin(client, "dev-p2")
    lid = drive_to_downloaded(client, "dev-p2", image)

    assert client.post(f"/admin/campaigns/{cid}/pause").status_code == 200
    # 尚未进入安装阶段：install/begin 被闸门拦住
    assert client.post(f"/device/leases/{lid}/install/begin").status_code == 423

    # 恢复后进入安装准备，再暂停：写关键区前最后检查点同样被拦
    assert client.post(f"/admin/campaigns/{cid}/resume").status_code == 200
    assert client.post(f"/device/leases/{lid}/install/begin").status_code == 200
    assert client.post(f"/admin/campaigns/{cid}/pause").status_code == 200
    assert client.post(f"/device/leases/{lid}/install/critical").status_code == 423


def test_device_in_critical_section_finishes_safely_after_pause(client):
    image, cid = _campaign_with_image(client)
    checkin(client, "dev-p3")
    lid = drive_to_critical(client, "dev-p3", image)

    assert client.post(f"/admin/campaigns/{cid}/pause").status_code == 200

    # 已写入关键区：回执必须被接受（安全收尾），且支持失败回滚上报
    r = client.post(
        f"/device/leases/{lid}/result",
        json={
            "receipt_id": "rcpt-p3",
            "outcome": "rolled_back",
            "reason_code": "E_VERIFY_BOOT",
            "detail": "new slot failed boot verification, switched back",
        },
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "rolled_back"

    # 活动状态里这次失败已计入
    camp = client.get(f"/admin/campaigns/{cid}").json()
    assert camp["stages"][0]["failed"] == 1


def test_stop_aborts_noncritical_but_waits_for_critical(client):
    image, cid = _campaign_with_image(client)
    checkin(client, "dev-s1")
    checkin(client, "dev-s2")
    lid_downloading = claim(client, "dev-s1").json()["lease_id"]
    lid_critical = drive_to_critical(client, "dev-s2", image)

    assert client.post(f"/admin/campaigns/{cid}/stop").status_code == 200

    # 未进关键区的 lease 被安全中止
    r = client.get(f"/device/leases/{lid_downloading}/manifest")
    assert r.status_code == 409  # ABORTED 是终态

    # 关键区设备照常收尾
    r = client.post(
        f"/device/leases/{lid_critical}/result",
        json={"receipt_id": "rcpt-s2", "outcome": "committed"},
    )
    assert r.status_code == 200
