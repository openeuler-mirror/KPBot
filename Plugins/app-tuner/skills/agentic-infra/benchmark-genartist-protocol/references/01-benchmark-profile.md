# GenArtist benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（13 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| genartist_01_layout_text_to_image | 34 | 32/0/0/0/0 | 948,702 | 386.6 | process×3 | agbench-genartist_01_layout_text_to_image:v1 |
| genartist_02_object_addition_edit | 31 | 23/4/1/1/0 | 1,161,133 | 528.1 | process×7 | agbench-genartist_02_object_addition_edit:v1 |
| genartist_03_scene_edit_correction | 43 | 41/0/0/0/0 | 2,623,047 | 465.8 | image×1、process×44 | agbench-genartist_03_scene_edit_correction:v1 |
| genartist_04_layout_guided_generation | 55 | 45/2/4/2/0 | 2,806,405 | 832.0 | process×32 | agbench-genartist_04_layout_guided_generation:v1 |
| genartist_05_object_manipulation | 44 | 32/0/5/5/0 | 1,902,737 | 687.4 | image×15、process×9 | agbench-genartist_05_object_manipulation:v1 |
| genartist_06_full_pipeline_correction | 44 | 39/3/0/0/0 | 2,288,541 | 730.3 | image×1、process×30 | agbench-genartist_06_full_pipeline_correction:v1 |
| genartist_07_bsrgan_enhancement | 48 | 42/2/2/0/0 | 2,914,026 | 439.0 | process×45 | agbench-genartist_07_bsrgan_enhancement:v1 |
| genartist_08_dinov2_feature_search | 39 | 26/4/5/2/0 | 1,326,402 | 485.6 | process×12 | agbench-genartist_08_dinov2_feature_search:v1 |
| genartist_09_scipy_filter_pipeline | 39 | 30/3/2/2/0 | 1,814,116 | 454.4 | image×3、process×18 | agbench-genartist_09_scipy_filter_pipeline:v1 |
| genartist_10_torchvision_preprocess | 48 | 42/2/2/0/0 | 2,593,340 | 456.5 | image×2、process×38 | agbench-genartist_10_torchvision_preprocess:v1 |
| genartist_11_groundingdino_detect | 46 | 33/4/3/4/0 | 2,025,700 | 638.7 | image×2、process×19 | agbench-genartist_11_groundingdino_detect:v1 |
| genartist_12_lama_inpaint_mkl | 22 | 17/3/0/0/0 | 986,168 | 250.3 | image×2、process×14 | agbench-genartist_12_lama_inpaint_mkl:v1 |
| genartist_13_anydoor_data_load | 28 | 19/4/3/0/0 | 1,175,592 | 324.5 | process×12 | agbench-genartist_13_anydoor_data_load:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **图像生成/编辑全链路负载**（13 case，agbench 最大池）：text-to-image、物体添加/删除/编辑、场景编辑自纠错、BSRGAN 增强、DINOv2 特征检索、GroundingDINO 检测、LaMa 修复等，沙箱内以 exec 步驱动（torch/diffusers 生态的安装与推理脚本执行）。
- **image/process 跳过调用多**（image 合计 26 次）：主机/模型侧调用不产生沙箱负载，须按净工具步数估压。
- **步骤数与 token 大**：单 case 最高 55 步 / 2.9M token，`--command-timeout-sec` 建议按 case 最长命令放宽。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，genartist 压测子集目录必须**恰好**包含上表 13 条
`genartist_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
