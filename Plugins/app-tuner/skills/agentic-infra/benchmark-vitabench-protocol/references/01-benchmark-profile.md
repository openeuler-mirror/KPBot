# VitaBench benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（4 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| vitabench_01_ota_hotel_booking | 40 | 37/0/1/0/0 | 1,560,531 | 100.3 | 无 | agbench-vitabench_01_ota_hotel_booking:v1 |
| vitabench_02_delivery_rice_noodles | 52 | 50/0/0/0/0 | 1,655,751 | 188.4 | 无 | agbench-vitabench_02_delivery_rice_noodles:v1 |
| vitabench_03_instore_gym_search | 46 | 44/0/0/0/0 | 1,445,894 | 138.7 | 无 | agbench-vitabench_03_instore_gym_search:v1 |
| vitabench_04_redis_session_store | 37 | 32/0/2/1/0 | 1,528,006 | 207.3 | 无 | agbench-vitabench_04_redis_session_store:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **交互式生活服务工具负载**：OTA 酒店预订、外卖点单、到店健身房搜索（exec 密集 32-50 步/case）+ Redis 会话存储部署（含 write/edit）。
- **零 web 侧跳过**：全部录制调用映射为沙箱步骤，总时长可近似估计沙箱压力形态。
- **池小（4 case）**：`--base-count 4`；外推需声明池规模限制。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，vitabench 压测子集目录必须**恰好**包含上表 4 条
`vitabench_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
