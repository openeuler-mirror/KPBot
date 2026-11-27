# UniVA benchmark 画像（agbench 导入）

> 数据源：suite `artifacts/traces/agbench/import-manifest.json`（`agbench-import-v1`，commit
> `673be52`，delay-model `recorded`，model `cannbot/deepseek-v4.1-flash`）；数字与 manifest 按
> case_id 行级逐字段对账（tier-A 用例看护），上游更新时须同步修订本表。

## Case 清单（6 条）

| case_id | steps | exec/read/write/edit/apply_patch | tokens | think_time(s) | 跳过 | 镜像 |
|---|---|---|---|---|---|---|
| univa_01_image_text_to_video | 40 | 38/0/0/0/0 | 2,511,746 | 421.1 | process×39 | agbench-univa_01_image_text_to_video:v1 |
| univa_02_video_style_editing | 41 | 39/0/0/0/0 | 2,375,398 | 372.1 | image×3、process×34 | agbench-univa_02_video_style_editing:v1 |
| univa_03_long_video_understanding | 21 | 15/4/0/0/0 | 690,515 | 225.7 | image×1、process×6 | agbench-univa_03_long_video_understanding:v1 |
| univa_04_storyboard_pipeline | 56 | 47/2/2/3/0 | 2,955,440 | 1439.9 | process×19 | agbench-univa_04_storyboard_pipeline:v1 |
| univa_05_video_editing_stylization | 43 | 41/0/0/0/0 | 1,225,937 | 736.5 | process×8 | agbench-univa_05_video_editing_stylization:v1 |
| univa_06_segmentation_tracking | 38 | 35/1/0/0/0 | 822,872 | 577.2 | process×5 | agbench-univa_06_segmentation_tracking:v1 |

每条轨迹：第 0 步 `prelude`（git 身份）+ 工具步骤 + 末步 `verify`（`python3 bench.py
verify`，判定读该步 stdout）。

## 负载特征（对采集与归因的含义）

- **逐 case 负载**：01 文生视频、02 视频风格化编辑、03 长视频理解、04 故事板流水线（storyboard_generate→text2video_gen 级联→merge）、05 视频编辑风格化（vision2text_gen→refine_gen_prompt→style_transfer）、06 指代分割与跟踪（video_referring_segmentation·video_tracking，含 audio_gen 子步骤）。
- **image/process 跳过调用合计 115 次**（01:39、02:37、03:7、04:19、05:8、06:5）：主机/模型侧调用不产生沙箱负载，须按净工具步数估压。
- **净工具步 19-54 步/case**（exec+read+write+edit+apply_patch，区别于含跳过调用的会话总步数），token 0.7-3.0M——压测口径以净工具步数为准。

## 压测子集契约

runner 以 `--trajectory-dir` 整目录执行，univa 压测子集目录必须**恰好**包含上表 6 条
`univa_*.replay.json` 与对应 image-map 条目；runbook §3 以画像 case 清单为期望集做三方
一致校验，混入其他轨迹或替换轨迹时校验失败（tier-A 用例的命令纯度检查项亦为此设）。
