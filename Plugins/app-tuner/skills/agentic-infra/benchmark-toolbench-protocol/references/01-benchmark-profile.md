# ToolBench benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（3 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| toolbench_01_customs_agency_lookup | 31 | 29/0/0/0/0 | 831,169 | 119.2 | 无 | agbench-toolbench_01_customs_agency_lookup:v1 |
| toolbench_02_package_carrier_tracking | 28 | 26/0/0/0/0 | 778,822 | 107.0 | 无 | agbench-toolbench_02_package_carrier_tracking:v1 |
| toolbench_03_beach_cocktail_recommendation | 39 | 37/0/0/0/0 | 1,284,722 | 203.0 | 无 | agbench-toolbench_03_beach_cocktail_recommendation:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **工具调用编排型负载**：海关代理查询（G1 单工具）、快递轨迹跟踪（G2 类内多工具）、海滩鸡尾酒推荐（G3 跨类多工具），exec 密集（26-37 步/case）。
- **零跳过调用**：全部录制调用映射为沙箱步骤，总时长可近似估计沙箱压力形态。
- **池小（3 case）**：`--base-count 3`；外推需声明池规模限制。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，toolbench 压测子集目录必须**恰好**包含上表 3 条
`toolbench_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
