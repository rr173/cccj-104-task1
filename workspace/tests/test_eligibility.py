"""兼容性匹配：型号 / 引导程序 / 当前版本 / 硬件批次 四重约束。"""

from conftest import checkin, claim, create_campaign, upload_firmware


def test_model_mismatch_gets_no_offer(client):
    fw = upload_firmware(client, model="sensor-A")
    create_campaign(client, fw)
    checkin(client, "dev-model-x", model="sensor-B")
    assert claim(client, "dev-model-x").status_code == 404


def test_old_bootloader_gets_no_offer(client):
    fw = upload_firmware(client, min_bootloader="2.0")
    create_campaign(client, fw)
    checkin(client, "dev-bl-old", bootloader="1.9")
    assert claim(client, "dev-bl-old").status_code == 404
    checkin(client, "dev-bl-ok", bootloader="2.0")
    assert claim(client, "dev-bl-ok").status_code == 201


def test_current_version_not_on_upgrade_path(client):
    fw = upload_firmware(client, allowed_from="1.0.0,1.1.0")
    create_campaign(client, fw)
    checkin(client, "dev-bad-from", current_version="0.9.0")
    assert claim(client, "dev-bad-from").status_code == 404
    checkin(client, "dev-good-from", current_version="1.1.0")
    assert claim(client, "dev-good-from").status_code == 201


def test_already_on_target_version_gets_no_offer(client):
    fw = upload_firmware(client, target="2.0.0")
    create_campaign(client, fw)
    checkin(client, "dev-latest", current_version="2.0.0")
    assert claim(client, "dev-latest").status_code == 404


def test_hw_batch_gating_and_staged_rollout(client):
    """第一批只覆盖 hw-1；hw-2 的设备要等第二级放量开启后才能领取。"""
    fw = upload_firmware(client)
    cid = create_campaign(
        client,
        fw,
        stages=[
            {"hw_batches": ["hw-1"], "quota": 10},
            {"hw_batches": ["hw-2"], "quota": 10},
        ],
    )
    checkin(client, "dev-hw2", hw_batch="hw-2")
    assert claim(client, "dev-hw2").status_code == 404  # 第二级尚未开启

    assert client.post(f"/admin/campaigns/{cid}/stages/1/activate").status_code == 200
    assert claim(client, "dev-hw2").status_code == 201


def test_quota_is_enforced(client):
    fw = upload_firmware(client)
    create_campaign(client, fw, stages=[{"hw_batches": ["hw-1"], "quota": 1}])
    checkin(client, "dev-q1")
    checkin(client, "dev-q2")
    assert claim(client, "dev-q1").status_code == 201
    assert claim(client, "dev-q2").status_code == 404  # 名额已用尽
