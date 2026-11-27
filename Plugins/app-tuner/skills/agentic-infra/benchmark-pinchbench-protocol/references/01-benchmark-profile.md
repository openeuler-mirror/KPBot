# PinchBench benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（3 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| pinchbench_01_access_log_anomaly | 34 | 29/0/3/0/0 | 1,064,259 | 123.4 | 无 | agbench-pinchbench_01_access_log_anomaly:v1 |
| pinchbench_02_browser_automation | 36 | 24/8/1/1/0 | 774,875 | 125.5 | 无 | agbench-pinchbench_02_browser_automation:v1 |
| pinchbench_03_byok_best_practices | 47 | 29/3/1/12/0 | 2,398,199 | 221.9 | edit×1、web_search×1 | agbench-pinchbench_03_byok_best_practices:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **混合能力小池**：日志异常分析（exec 密集）、浏览器自动化（read×8）、最佳实践调研（edit×12，含 1 次 web_search 与 1 次 edit 跳过——主机侧调用不产生沙箱负载）。
- **池小（3 case）**：压测基线 `--base-count 3`；结论外推到同 benchmark 全集时需声明池规模限制。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，pinchbench 压测子集目录必须**恰好**包含上表 3 条
`pinchbench_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
