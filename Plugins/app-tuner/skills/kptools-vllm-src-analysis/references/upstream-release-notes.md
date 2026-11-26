# 官方发布说明（线上）要点

证据等级：`claimed_official_docs`。这是官方文档的宣称与计划，不是实测数据；
性能幅度仍需 benchmark / profiling 证据。

## 来源

| 用途 | 地址 |
|---|---|
| 中文文档（用户提供） | https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/latest/user_guide/release_notes.html |
| 稳定版文档入口 | https://docs.vllm.ai/projects/ascend/zh-cn/v0.30.0 |
| raw markdown（脚本使用） | https://raw.githubusercontent.com/vllm-project/vllm-ascend/main/docs/source/user_guide/release_notes.md |

注意：`latest` 是开发者预览（main 分支），可能包含本地 clone 还没有的新版本；
引用稳定版结论时应指向固定版本路径（如 `/v0.30.0/`）。

## v0.30.0 稳定版（2026-10-29）关键声明

脚本复核：

```bash
python3 scripts/release_notes.py --online --version v0.30.0 \
  --sections Performance,Deprecation,Known\ Issues --json rn30.json
```

| 主题 | 官方表述（转述） | 对回答的影响 |
|---|---|---|
| 默认 ModelRunner | 该版本仍**默认使用 MRV1**，部分模型需切到 MRV2 才获支持；计划在下个版本全面切换到 Model Runner V2 | 回答“v2 默认版本”时必须给 unknown/计划，不能把 v0.30.0 说成已默认 |
| 已知问题 | 默认仍 MRV1；**主分支已默认切换为 MRV2** | “主分支”≠“已发布稳定版”，必须区分 |
| 弃用 | MRV1 将跟随上游，并在**下个版本移除**；仅兼容 MRV1 的外部接口也将废弃 | 属于官方计划，回答趋势时标注为 official plan，不是已发生 |
| 依赖 | vLLM v0.30.0；CANN 9.2.1；torch/torch_npu 2.10.0 / 2.10.0.post7；Triton Ascend 3.6.0.post2（A2/A3/950）；Python 3.12 | 与 rc1（CANN 9.2.0、post4、Triton 3.2.2）不同，做 profiling 归因要写清版本 |
| 新增能力主线 | FlashMLA decode + DCP/DSpark、KV Pool 分层池化、PCP 投机解码、EPLB 对 MRV2 的加固、Kimi K3 支持 | 用于解释 0.30 线变化方向，不作性能结论 |

## 与本地 tag 的关系

- 本地 vllm-ascend clone 的 tag 只到 `v0.30.0rc1`，`main` 也还没有 v0.30.0 的发布说明。
- 因此 **v0.30.0 稳定版的条目只能通过 `--online` 获取**；`versions.py` 仍以本地 tag 为准，
  两者出现版本差时，报告里要显式写“本地未含、线上已发布”。
- 线上内容会随 main 分支变化；需要可复现时，把用到的条目和 PR 号抄进本文件并注明抓取日期。

## 回答模板

> 截至 v0.30.0（2026-10-29，官方 release notes）：默认仍是 MRV1，部分模型需启用 MRV2；
> 官方计划下个版本全面切换并移除 MRV1。证据等级：官方宣称（非实测）。
> 主分支已默认 MRV2，但这不等于任何已发布稳定版默认 MRV2。
