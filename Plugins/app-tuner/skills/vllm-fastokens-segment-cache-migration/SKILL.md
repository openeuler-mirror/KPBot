---
name: vllm-fastokens-segment-cache-migration
description: 评估 vLLM 部署将分词后端切换为 fastokens 官方最新发行版并启用段缓存（segment cache）的可行性：以合成多轮会话对比基线组与段缓存开启组，验证 token ID 一致性与并发稳定性，测量分词耗时与内存占用，并生成迁移前快照与回退说明。适用于 vLLM 分词后端的评估与切换、fastokens 的启用或升级、VLLM_USE_FASTOKENS 与 FASTOKENS_SEGMENT_CACHE 的配置，以及多轮对话或智能体流量下的分词耗时分析。
---

# vLLM 分词后端迁移至 fastokens 并启用段缓存

## 概述

本技能用于评估 vLLM 部署能否将分词后端切换为 fastokens 官方最新发行版并启用段缓存。验证在分词器层进行，不重启服务，采用两组对比：

| 组 | 配置 |
|---|---|
| 基线组 | 部署当前使用的分词后端与配置 |
| 段缓存开启组 | fastokens 目标版本，`VLLM_USE_FASTOKENS=1`，`FASTOKENS_SEGMENT_CACHE=<段缓存预算（MiB）>` |

| 术语 | 定义 |
|---|---|
| 段缓存（segment cache） | fastokens 以特殊 token 为界将输入切分为文本段，以文本段内容为键缓存分词结果；多轮对话中重复出现的历史文本段复用缓存结果 |
| 生效的分词后端 | vLLM 进程实际执行分词所使用的实现，可能与环境中已安装的分词库不同 |
| 分词器层 | 测量层级之一：经 vLLM 的分词器加载入口加载分词器，按 vLLM 服务端的调用方式执行分词，不启动推理引擎 |

## 适用范围

适用于 vLLM 0.22 及以上版本、使用 Python 前端的部署，包括 vLLM-Ascend。目标版本为不低于 0.3.5 的 fastokens 官方发行版，宜选用最新版本；官方发行版自 0.3.5 起包含段缓存。不满足条件的部署由探测步骤给出原因，流程随之终止。

以下内容不在本技能范围内：服务的切换、重启与回退操作，首 token 时延（TTFT）等服务层指标的测量，vLLM 其他参数的调优，fastokens 与 vLLM 的源码构建。

## 组成

| 路径 | 说明 |
|---|---|
| `scripts/fastokens_segment_cache.py` | 检查工具，含迁移判定逻辑；仅依赖 Python 3.9 及以上版本的标准库 |
| `subskills/` | 可按任务使用的六项技能 |
| `tests/` | 检查工具的单元测试与端到端测试 |
| `evals/` | 技能评测用例 |

检查工具的子命令：

| 子命令 | 功能 |
|---|---|
| `probe` | 采集版本、前端类型、运行中 vLLM 进程的环境变量与已加载的 fastokens 原生库，给出迁移判定；仅读取信息，不变更进程 |
| `snapshot` | 记录依赖清单与环境变量，将已安装的 fastokens 重新打包为 wheel，生成回退说明 `RESTORE.md` |
| `make-sessions` | 生成合成多轮会话，作为回放数据 |
| `verify` | 分词器层的两组对比 |

## 参考依据

| 内容 | 来源 |
|---|---|
| 段缓存的配置项与行为 | 目标版本 fastokens 的 README，“Segment cache”一节（https://github.com/crusoecloud/fastokens） |
| 包含段缓存的最低官方版本 | fastokens 0.3.5 的 README 含“Segment cache”一节，0.3.4 的 README 不含该节 |
| `VLLM_USE_FASTOKENS` 开关 | vLLM 源码 `vllm/envs.py` 与 `vllm/tokenizers/fastokens.py`；自 0.22 起提供，取值按整数解析 |
| vLLM 服务端的分词调用方式 | vLLM 源码 `vllm/utils/async_utils.py`（0.23） |
| fastokens 发行版本 | PyPI（https://pypi.org/project/fastokens/）。GitHub Releases 与 PyPI 不完全同步，例如 0.3.5 在 GitHub 上无对应的 tag 与 Release |

## 操作约束

- 本技能不重启服务，不变更服务的依赖、镜像与环境变量。服务的切换、重启与回退由用户按其发布流程执行。
- 候选版本安装在独立目录中，探测、快照与分词器层对比均不变更服务环境。
- 仅在用户已授权加载模型自定义代码时使用 `--trust-remote-code`。
- 报告不应包含凭证。
- 分词耗时与内存占用的验收阈值由用户提供；未提供时，评估结论在与用户确认阈值后给出。

## 前置条件

探测、快照和验证使用 vLLM 服务所用的 Python 解释器（记为 `$PY`）。容器化部署时，在指定的服务容器内运行。已安装的父技能目录记为 `$S`。只生成回放数据或汇总已有报告时，无需访问服务。

## 按任务选择

用户指定单项任务时，读取下表对应技能，只执行该项。已有符合要求的产物可直接使用，无需重复探测或测量。

| 任务 | 子技能 |
|---|---|
| 核对当前分词后端及迁移条件；复核切换或回退结果 | [当前分词后端分析](subskills/inspect-tokenizer-backend/SKILL.md) |
| 准备指定 fastokens 版本并确认能加载 | [候选版本准备与加载验证](subskills/prepare-fastokens-candidate/SKILL.md) |
| 保存已有构建及恢复配置 | [迁移快照与回退准备](subskills/snapshot-tokenizer-environment/SKILL.md) |
| 生成多轮合成回放 | [生成多轮回放数据](subskills/generate-replay-sessions/SKILL.md) |
| 检查一致性、并发、缓存命中及分词性能 | [分词回放验证](subskills/verify-tokenizer-replay/SKILL.md) |
| 用已有证据给出迁移结论 | [迁移评估报告](subskills/summarize-fastokens-migration/SKILL.md) |

## 操作流程

1. 用 [当前分词后端分析](subskills/inspect-tokenizer-backend/SKILL.md) 确认实际后端及适用性。不支持时说明原因；缺少目标版本时，向用户索取官方 wheel。
2. 用 [迁移快照与回退准备](subskills/snapshot-tokenizer-environment/SKILL.md) 保存原环境，再按 [候选版本准备与加载验证](subskills/prepare-fastokens-candidate/SKILL.md) 准备候选版本。环境已安装指定官方版本时仍做加载核对；加载不符则停止回放。
3. 使用已有回放文件，或按 [生成多轮回放数据](subskills/generate-replay-sessions/SKILL.md) 生成，再运行 [分词回放验证](subskills/verify-tokenizer-replay/SKILL.md)。
4. 按 [迁移评估报告](subskills/summarize-fastokens-migration/SKILL.md) 汇总验证及回退准备。如果满足切换条件，给出指定版本的安装方案（容器部署则更新镜像），把 `VLLM_USE_FASTOKENS=1` 与 `FASTOKENS_SEGMENT_CACHE=<段缓存预算（MiB）>` 配置到所有 API 进程的启动环境。使用服务原有的发布和安装方式。
5. 切换或回退由用户执行。执行后用 [当前分词后端分析](subskills/inspect-tokenizer-backend/SKILL.md) 核对进程读到的配置和加载的原生库。回退时以快照的 `RESTORE.md` 为准。

## 报告要求

- 报告应使用中文撰写。
- 每个数值应注明来源文件、基线、测量层级（分词器层）与场景（模型、合成会话参数、并发检查线程数、段缓存预算）。
- 倍数应表述为“是原来的 X 倍”（X = 原耗时 ÷ 新耗时）或“提升 Y%”（Y = (X − 1) × 100），不应使用“提升了 X 倍”。
- 未测量的项目应标注“未测”，不应进行推算。

报告结构：

```markdown
# fastokens 段缓存迁移报告
## 结论
（是否可切换；一致性与稳定性结果；对照阈值的评估结果）
## 环境
（vLLM、vllm-ascend、transformers、fastokens 版本；前端类型；生效的分词后端；目标版本及来源）
## 一致性与稳定性
（合成会话参数、请求数、会话数、并发检查线程数、验证结果）
## 性能
（分词耗时与内存占用，取自 verify 报告）
## 切换与回退
（切换方案；快照目录与 RESTORE.md 路径；切换后与回退后的 probe 结果，未执行时标注“未执行”）
## 待确认事项
```
