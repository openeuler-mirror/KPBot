# PaperQA benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（6 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| paperqa_01_antibiotic_resistance | 47 | 44/0/1/0/0 | 686,636 | 55.6 | 无 | agbench-paperqa_01_antibiotic_resistance:v1 |
| paperqa_02_olfactory_receptor_contacts | 38 | 35/0/1/0/0 | 590,346 | 48.3 | 无 | agbench-paperqa_02_olfactory_receptor_contacts:v1 |
| paperqa_03_protein_interaction_assay | 38 | 35/0/1/0/0 | 534,716 | 59.9 | 无 | agbench-paperqa_03_protein_interaction_assay:v1 |
| paperqa_04_pdf_processing_pipeline | 50 | 44/0/3/1/0 | 1,958,796 | 231.5 | process×14 | agbench-paperqa_04_pdf_processing_pipeline:v1 |
| paperqa_05_embedding_search | 62 | 58/1/1/0/0 | 3,364,927 | 166.7 | process×58 | agbench-paperqa_05_embedding_search:v1 |
| paperqa_06_html_processing | 61 | 56/1/1/1/0 | 1,914,965 | 139.8 | process×21 | agbench-paperqa_06_html_processing:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **文档问答型负载**：文献检索问答（3 case）+ PDF 抽取/tiktoken 计数/tantivy 索引（04）、sentence-transformers 嵌入与余弦检索（05）、html2text/bs4 解析（06），exec 密集（单 case 35-58 步）。
- **process 跳过调用合计 93 次**（04:14、05:58、06:21）：主机/模型侧调用不产生沙箱负载，须按净工具步数估压。
- **case 05（embedding 检索）最重**：62 步 / 3.4M token，净 exec 58 步。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，paperqa 压测子集目录必须**恰好**包含上表 6 条
`paperqa_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
