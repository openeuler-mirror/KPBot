# 术语与架构口径

## 先定义，再回答

本 skill 语境里的 **v1 / v2 指 ModelRunner V1 / ModelRunner V2**，不是 vLLM 的
V0/V1 engine，也不是 vLLM 的发布大版本号。用户问“v2 是哪版引入的”时，必须先确认
指的是 ModelRunner V2；否则应改用 `timeline.py` 对具体符号/文件求证。

## ModelRunner V1

| 项 | 值 | 证据 |
|---|---|---|
| Ascend 实现 | `vllm_ascend/worker/model_runner_v1.py`，`class NPUModelRunner(GPUModelRunner)` | v0.30.0rc1 class@364 |
| 上游基类 | `vllm/v1/worker/gpu_model_runner.py` 的 `GPUModelRunner` | v0.13.0rc1 class@268；v0.30.0 class@498 |
| 存在范围 | v0.8.4rc1 起的所有成熟版本 | `versions.py` 的 `model_runner.v1` |
| v0.30.0rc1 锚点 | class@364，`execute_model`@2229 | `anchors.py --version v0.30.0rc1` |

## ModelRunner V2

| 项 | 值 | 证据 |
|---|---|---|
| Ascend 实现 | `vllm_ascend/worker/v2/model_runner.py`，`class NPUModelRunner(GPUModelRunner)` | v0.30.0rc1 class@125 |
| 上游基类 | `vllm/v1/worker/gpu/model_runner.py` 的 `GPUModelRunner` | v0.13.0rc1 class@67；v0.30.0 class@187 |
| 代码引入 | commit `b69b04d3a91c`（2025-12-18，`implement model runner v2 basic framework (#5051)`） | `timeline.py --path` |
| 首个发布 | **v0.13.0rc1**（2025-12-27） | `timeline.py` / `versions.py` |
| 上游对应 | vllm `GPU Model Runner V2 (#25266)`（2025-11-21），首个 vllm tag v0.13.0rc1 | `git tag --contains 30b44a1598` |
| 选择开关 | v0.13.0rc1 起 `VLLM_USE_V2_MODEL_RUNNER`（vllm `envs.py` 默认 False）；Ascend `worker.py` 读取后选择 V2 实现 | `git grep VLLM_USE_V2_MODEL_RUNNER` |
| 默认状态 | 至 v0.30.0rc1 **未默认开启**：vllm envs 为 `bool \| None = None`，由 `vllm_config.use_v2_model_runner` 决策；启用时 worker 打印 `npu model runner v2 is in developing` | v0.30.0rc1 `worker.py:499-506` |
| 官方计划 | **v0.30.0 稳定版仍默认 v1**；官方 release notes 称计划在下个版本全面切换到 MRV2，并移除 MRV1（主分支已默认 MRV2，但这不是已发布稳定版） | [upstream-release-notes.md](upstream-release-notes.md) |
| 成熟度 | README 标 `[Experimental] Model Runner V2` | `vllm_ascend/worker/v2/README.md` |
| v0.30.0rc1 锚点 | class@125，`execute_model`@486，`prepare_inputs`@639，`sample`@918 | `anchors.py --version v0.30.0rc1` |

v2 目录随版本扩张（v0.13.0rc1 仅 7 个文件；v0.30.0rc1 已有 `sample/`、`model_states/`、
`eplb.py`、`pcp_manager.py` 等），用 `anchors.py --diff` 可看具体漂移。

## 易混淆：vLLM engine V0 / V1

这是调度/执行引擎层，与 ModelRunner V1/V2 是两条线：

| 状态 | 事实 | 证据 |
|---|---|---|
| 代码引入 | `[V1] Implement vLLM V1 [1/N] (#9289)`，2024-10-22 | `git log -S`，首个包含 tag v0.6.4 |
| 默认开启 | `VLLM_USE_V1: bool = True` 在本地可查的 v0.9.0 `envs.py` | `git show v0.9.0:vllm/envs.py` |
| 移除开关 | `[[V0 deprecation]] Remove VLLM_USE_V1 env (#28204)` | `git log -S` |

回答版本问题时不要把它和 ModelRunner V1/V2 混为一谈；需要时把两条时间线分别列出。

## 版本配对

vllm-ascend 与 vllm 的配对以各 tag 内
`docs/source/community/versioning_policy.md` 的 release compatibility matrix 为准
（同表还给 CANN / torch_npu / Triton Ascend / Mooncake）。矩阵缺行时才回退到
`.github/vllm-release-tag.commit`、同名 tag、nearest-lower，并降级 confidence。
