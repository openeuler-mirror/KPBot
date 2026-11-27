# WebArena benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（4 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| webarena_01_shopping_ear_cups | 40 | 37/0/1/0/0 | 1,164,755 | 151.9 | 无 | agbench-webarena_01_shopping_ear_cups:v1 |
| webarena_02_reddit_downvoted_comments | 36 | 33/0/1/0/0 | 1,023,838 | 178.7 | 无 | agbench-webarena_02_reddit_downvoted_comments:v1 |
| webarena_03_gitlab_todos | 32 | 29/0/1/0/0 | 681,849 | 93.1 | 无 | agbench-webarena_03_gitlab_todos:v1 |
| webarena_04_multi_tab_navigation | 37 | 30/4/1/0/0 | 1,056,637 | 97.5 | process×1 | agbench-webarena_04_multi_tab_navigation:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **浏览器导航型负载**：购物站点（耳杯选购）、Reddit 评论、GitLab 待办、多标签页导航（go_back/new_tab/page_focus 等），exec 密集（29-37 步/case）。
- **站点本地化**：WebArena 站点以本地实例随镜像提供，导航脚本在沙箱内执行——归因看 exec 分布与网络栈本地开销。
- **池小（4 case）**：`--base-count 4`；外推需声明池规模限制。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，webarena 压测子集目录必须**恰好**包含上表 4 条
`webarena_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
