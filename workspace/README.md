# OTA Fleet Update Service

面向**大量间歇联网终端**的固件升级服务。运营上传镜像后按硬件批次逐级放量；
终端只领取与自身兼容的镜像，下载断点续传，安装失败自动回滚并上报；
批次可暂停、失败率越限自动熔断，全链路幂等。

## 快速开始（容器，可复现）

```bash
docker compose up --build app        # 启动服务，监听 :8080
docker compose run --rm test         # 在容器内跑完整测试套件（21 个用例）
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -q
uvicorn app.main:app --port 8080
```

配置项（环境变量）：`OTA_DATA_DIR`（数据目录，默认 `./data`）、
`OTA_CHUNK_SIZE`（分块字节数，默认 256KiB）、`OTA_FAIL_THRESHOLD`（默认 0.05）、
`OTA_MIN_SAMPLES`（默认 20）。SQLite 库与固件分块都落在数据目录，挂卷即可持久化。

## 架构

```
┌────────────┐   multipart    ┌──────────────────────────────────────┐
│  运营控制台  │ ─────────────▶ │  admin API                            │
└────────────┘   编排/暂停     │   镜像切块+哈希落盘 / 活动 / 放量阶段    │
                               ├──────────────────────────────────────┤
┌────────────┐  checkin/claim  │  device API                           │
│  终端设备   │ ◀─────────────▶ │   兼容性匹配 / 名额占位 / 分块下载      │
│ (间歇联网)  │  chunk/resume   │   闸门 / 回执 / 熔断                  │
└────────────┘  result         ├──────────────────────────────────────┤
                               │  SQLite (WAL)                         │
                               │  devices firmwares campaigns stages   │
                               │  leases(唯一约束) receipts(幂等键)      │
                               └──────────────────────────────────────┘
```

单进程 + SQLite 是有意的取舍：间歇联网终端的 QPS 上限低，
`BEGIN IMMEDIATE` 单写者事务让名额占位、熔断判定等并发敏感逻辑**天然串行、无需外部锁**。
需要水平扩展时，把 `app/db.py` 换成 PostgreSQL 即可，事务与唯一约束设计不变。

## 核心设计

### 1. 兼容性匹配（终端只拿得到能装的镜像）

`POST /device/claim` 时逐活动过滤，四重条件全部满足才占位：

| 条件 | 规则 |
|---|---|
| 型号 | `firmware.model == device.model` |
| 引导程序 | `device.bootloader >= firmware.min_bootloader`（语义化版本比较） |
| 当前版本 | 不等于目标版本，且在 `allowed_from` 升级路径列表内（空列表 = 任意旧版本） |
| 硬件批次 | `device.hw_batch` 属于某个 `active` 阶段的批次集合 |

### 2. 逐级放量与名额

活动（campaign）含若干阶段（stage），每级绑定一批硬件批次和名额 `quota`。
`start` 激活第一级；`POST /admin/campaigns/{id}/stages/{seq}/activate` 开启下一级（允许交叠灰度）。

名额占位是一条**条件 UPDATE**，原子完成：

```sql
UPDATE stages SET granted = granted + 1
WHERE stage_id = ? AND state = 'active' AND granted < quota;   -- rowcount=0 即无名额
```

名额只增不减（失败设备也占名额，作为熔断的统计输入），从机制上杜绝超发。

### 3. 断点续传（从已校验的数据块继续）

- 上传时服务端按 `chunk_size` 切块，逐块 SHA256 落盘，清单入库。
- 设备 `GET /leases/{id}/manifest` 拿到全部块的 `{index, size, sha256}`；
  每下载一块（`GET /leases/{id}/chunks/{i}`，响应头带 `X-Chunk-SHA256`）
  **校验通过才落盘、才计入本地 verified 集合**。
- 断线重连后 `POST /leases/{id}/resume {verified:[...]}`，服务端只返回缺失块清单。
- 全部块校验通过后 `POST /leases/{id}/download-complete`，服务端核对集合完整才放行到 `DOWNLOADED`。

### 4. 设备状态机与暂停闸门

```
OFFERED → DOWNLOADING → DOWNLOADED → INSTALL_PREP → INSTALL_CRITICAL → COMMITTED
                                        │                  └────────→ ROLLED_BACK
                                        └（暂停闸门在此关闭）
```

- **闸门**（`_enforce_gate`）：活动非 running 或批次非 active 时，
  `OFFERED/DOWNLOADING/DOWNLOADED/INSTALL_PREP` 的设备一切前进动作（拉块、
  manifest、install/begin、install/critical）都返回 `423 Locked`——
  **尚未进入安装阶段的设备不得继续**。
- `INSTALL_CRITICAL` 是写关键区（A/B 分区切换点）之前的最后检查点。
  一旦进入，**暂停、停止、熔断都不再拦截它的回执**——已写关键区的设备必须安全收尾。
- 回滚由设备侧完成（A/B 分区切回上一可启动槽位），服务端通过
  `rolled_back + reason_code`（必填）记录原因并计入失败率。
- `POST /admin/campaigns/{id}/stop` 终止活动：未进关键区的 lease 置 `ABORTED`，
  关键区 lease 保留等待收尾。

### 5. 失败率熔断（自动停止扩散）

每条回执在**同一事务内**更新计数并判定：

```
total = succeeded + failed
total >= min_samples 且 failed/total >= fail_threshold
    → 当前批次 STOPPED，活动 PAUSED，其余在放量的批次一并 PAUSED
```

闸门随即关闭，新设备领不到名额，扩散自动停止，等待人工定夺（`resume` 不会复活已熔断的批次）。

### 6. 幂等（重复上线 / 重复回执不占名额）

| 场景 | 机制 |
|---|---|
| 重复签到 | `devices` 主键 upsert，无副作用 |
| 重复领取 | `leases(campaign_id, device_id)` 唯一约束；已有 lease 直接重放（`reused:true`），名额不增；并发撞键时回退占位 |
| 重复回执 | `receipts.receipt_id` 主键；重复 POST 返回首次结果（`duplicate:true`），计数不变；换 receipt_id 对已终态 lease 上报返回 409 |

设备重试时必须沿用同一个 `receipt_id`（建议 `device_id + campaign_id + attempt` 或持久化 UUID）。

## API 一览

**运营端 `/admin`**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/firmwares` | 上传镜像（multipart：`file` + `model`/`target_version`/`min_bootloader`/`allowed_from`） |
| POST | `/admin/campaigns` | 创建活动（含 stages、熔断阈值） |
| POST | `/admin/campaigns/{id}/start` | 启动并激活第一级 |
| POST | `/admin/campaigns/{id}/stages/{seq}/activate` | 开启下一级放量 |
| POST | `/admin/campaigns/{id}/pause` `/resume` `/stop` | 暂停 / 恢复 / 终止 |
| GET | `/admin/campaigns/{id}` | 各阶段名额、成败计数、失败率 |

**终端 `/device`**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/device/checkin` | 幂等签到（型号/批次/引导程序/当前版本） |
| POST | `/device/claim` | 领取名额（幂等），返回 lease 与镜像摘要 |
| GET | `/device/leases/{id}/manifest` | 分块清单与哈希 |
| GET | `/device/leases/{id}/chunks/{i}` | 下载第 i 块（头带 SHA256） |
| POST | `/device/leases/{id}/resume` | 上报已校验块，返回缺失清单 |
| POST | `/device/leases/{id}/download-complete` | 声明全部块校验通过 |
| POST | `/device/leases/{id}/install/begin` | 进入安装准备（闸门） |
| POST | `/device/leases/{id}/install/critical` | 写关键区前最后检查点（闸门） |
| POST | `/device/leases/{id}/result` | 幂等回执：`committed` / `rolled_back`（必填 `reason_code`） |

## 端到端示例

```bash
B=http://localhost:8080
# 上传镜像
FW=$(curl -s -F file=@fw-2.0.0.bin -F model=sensor-A -F target_version=2.0.0 \
     -F min_bootloader=1.2 -F allowed_from=1.0.0,1.1.0 $B/admin/firmwares | jq -r .firmware_id)
# 两级放量：先 hw-1 五百台，再 hw-2 五万台；失败率 ≥5% 且样本 ≥20 即熔断
CID=$(curl -s -X POST $B/admin/campaigns -H 'content-type: application/json' -d "{
  \"firmware_id\":\"$FW\",\"name\":\"v2.0\",\"fail_threshold\":0.05,\"min_samples\":20,
  \"stages\":[{\"hw_batches\":[\"hw-1\"],\"quota\":500},
             {\"hw_batches\":[\"hw-2\"],\"quota\":50000}]}" | jq -r .campaign_id)
curl -X POST $B/admin/campaigns/$CID/start
# 设备侧
curl -X POST $B/device/checkin -H 'content-type: application/json' -d \
  '{"device_id":"d-001","model":"sensor-A","hw_batch":"hw-1","bootloader":"1.5","current_version":"1.0.0"}'
LID=$(curl -s -X POST $B/device/claim -H 'content-type: application/json' \
  -d '{"device_id":"d-001"}' | jq -r .lease_id)
curl $B/device/leases/$LID/manifest          # 拿分块清单，逐块下载+校验
curl -X POST $B/device/leases/$LID/resume -H 'content-type: application/json' -d '{"verified":[0,1]}'
```

## 测试

`tests/` 覆盖六类关键行为（21 个用例）：

- `test_eligibility.py` — 四重兼容性、批次灰度门控、名额上限
- `test_resume.py` — 分块清单哈希、断点只补缺失块、完整性校验
- `test_pause_gate.py` — 暂停拦截下载/安装入口、关键区设备安全收尾、stop 语义
- `test_circuit_breaker.py` — 越限自动熔断停扩散、未越限不误杀
- `test_idempotency.py` — 重复签到/领取/回执、冲突回执拒绝、回滚必填原因

## 目录结构

```
app/
  config.py      环境变量配置
  domain.py      状态机定义与迁移表
  versioning.py  语义化版本比较
  db.py          SQLite 连接、schema、事务
  service.py     核心业务（匹配/占位/闸门/熔断/幂等）
  deps.py        请求级依赖注入
  api_admin.py   运营端路由
  api_device.py  终端路由
  main.py        应用装配
tests/           pytest 套件
Dockerfile       runtime / test 双阶段
docker-compose.yml
```
