# CodeClash benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（5 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| codeclash_01_corewar_bot | 30 | 25/0/3/0/0 | 797,622 | 139.3 | 无 | agbench-codeclash_01_corewar_bot:v1 |
| codeclash_02_battlesnake_bot | 26 | 19/0/2/3/0 | 642,982 | 74.9 | process×1 | agbench-codeclash_02_battlesnake_bot:v1 |
| codeclash_03_robocode_tank | 38 | 23/6/0/7/0 | 1,052,751 | 183.1 | 无 | agbench-codeclash_03_robocode_tank:v1 |
| codeclash_04_result_aggregation | 63 | 57/0/2/0/2 | 2,011,954 | 202.4 | 无 | agbench-codeclash_04_result_aggregation:v1 |
| codeclash_05_zstd_log_compression | 31 | 29/0/0/0/0 | 1,232,970 | 51.7 | process×6、web_fetch×1 | agbench-codeclash_05_zstd_log_compression:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **编程竞技型负载**：Redcode 战士、BattleSnake 蛇、RoboCode 坦克的编写与对局，含编译执行与多轮对战，沙箱内 CPU 密集。
- **case 03（robocode）构建需 JDK**：`build_agbench_images.py --jdk-path <JDK 路径>`（其余 case 不需要）。
- **case 04 含 apply_patch×2、case 03 含 edit×7**：SWE patch 由导入器解析为原地补丁脚本，与 exec 共用串行命令通道。
- **battlesnake 胜负非确定**：录制期 agent 自身失败命令按原样回放、默认不中断。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，codeclash 压测子集目录必须**恰好**包含上表 5 条
`codeclash_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
