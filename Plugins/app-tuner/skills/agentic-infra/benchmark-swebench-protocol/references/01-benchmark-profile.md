# SWE-bench benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（3 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| swebench_01_invalid_url_check | 32 | 27/2/0/1/0 | 604,006 | 87.8 | 无 | agbench-swebench_01_invalid_url_check:v1 |
| swebench_02_empty_blueprint_name | 27 | 17/3/4/1/0 | 582,815 | 70.3 | 无 | agbench-swebench_02_empty_blueprint_name:v1 |
| swebench_03_get_content_length | 40 | 33/2/0/3/0 | 657,765 | 107.4 | 无 | agbench-swebench_03_get_content_length:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **库级 bugfix 修复型负载**：3 条 case 均为 Python 库缺陷修复（URL host 校验 / Blueprint 空 / 无 body 的 Content-Length），含 read 定位 + edit/apply 修复 + 测试验证，是 agbench 中最接近真实编码工作流的池。
- **零 web 侧跳过**：全部录制调用映射为沙箱步骤，总时长可近似估计沙箱压力形态。
- **池小（3 case）**：`--base-count 3`；外推需声明池规模限制。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，swebench 压测子集目录必须**恰好**包含上表 3 条
`swebench_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
