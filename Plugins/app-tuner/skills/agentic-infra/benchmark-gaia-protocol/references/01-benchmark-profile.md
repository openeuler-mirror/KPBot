# GAIA benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（6 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| gaia_01_invasive_clownfish_zipcodes | 24 | 21/0/1/0/0 | 1,343,209 | 83.8 | process×6、web_fetch×7、web_search×4 | agbench-gaia_01_invasive_clownfish_zipcodes:v1 |
| gaia_02_arxiv_lattice_articles | 27 | 24/0/1/0/0 | 659,323 | 64.8 | process×6 | agbench-gaia_02_arxiv_lattice_articles:v1 |
| gaia_03_numpy_regression_date | 27 | 24/0/1/0/0 | 648,099 | 89.1 | process×1、web_fetch×1、web_search×1 | agbench-gaia_03_numpy_regression_date:v1 |
| gaia_04_multiformat_parsing | 51 | 46/1/2/0/0 | 1,356,849 | 98.9 | process×15 | agbench-gaia_04_multiformat_parsing:v1 |
| gaia_05_ocr_image_analysis | 58 | 53/2/1/0/0 | 1,755,844 | 164.3 | process×25 | agbench-gaia_05_ocr_image_analysis:v1 |
| gaia_06_audio_transcription | 35 | 29/1/3/0/0 | 786,576 | 93.6 | process×2 | agbench-gaia_06_audio_transcription:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **web_fetch/web_search/process 跳过调用合计约 68 次**（01:17、02:6、03:3、04:15、05:25、06:2）：主机/模型侧调用不产生沙箱负载，须按净工具步数估压。
- **exec 密集的研究型负载**：多跳信息检索 + 多格式解析（pypdf2/python-docx/openpyxl/beautifulsoup4）+ OCR（pytesseract）+ 音频（ffmpeg），归因重点看 exec 耗时分布与 busy/stall 子窗口。
- **media 用例**（05 OCR / 06 音频）：BENCH_* 环境已烤入镜像，fixtures 服务回放自启，无需额外步骤。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，gaia 压测子集目录必须**恰好**包含上表 6 条
`gaia_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
