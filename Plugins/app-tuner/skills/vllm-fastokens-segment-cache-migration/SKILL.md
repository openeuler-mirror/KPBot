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
- 报告不应包含凭证。
- 分词耗时与内存占用的验收阈值由用户提供；未提供时，评估结论在与用户确认阈值后给出。

## 前置条件

检查工具应使用 vLLM 服务所用的 Python 解释器运行（下文记为 `$PY`），以使采集的 vllm、transformers 及 fastokens 信息同服务环境一致。容器化部署时，检查工具在服务容器内运行。检查工具的路径记为 `$S`。

## 操作流程

`probe` 在三个时间点使用：迁移前（第 1 步）、用户完成切换后（第 6 步）、用户完成回退后（第 7 步）。

### 1. 环境探测

```bash
$PY $S probe --model <模型名或本地分词器路径> [--tokenizer-mode <服务的分词模式>] --out <输出目录>
```

`--model` 的取值为服务的 `--tokenizer` 参数值；服务未指定 `--tokenizer` 时，取服务的 `--model` 参数值。

生效的分词后端根据运行中的 vLLM 进程判定：进程环境变量中 `VLLM_USE_FASTOKENS=1` 且进程已加载 fastokens 原生库时，判定为 fastokens。在 Linux 上，检查工具读取 vLLM 进程及其各级子进程的环境变量、已加载的共享库与 RSS，也可通过 `--pid` 指定进程。无运行中的 vLLM 进程时，检查工具按当前 shell 环境加载分词器并据此判定。

### 2. 迁移判定

| 判定 | 后续处理 |
|---|---|
| 不支持 | 向用户报告判定原因，流程终止 |
| 已是最新官方发行版 | 跳过安装，执行第 4 步；`verify` 不指定 `--overlay` |
| 可切换至 fastokens==X | 执行第 3 步；X 取自 PyPI |
| 缺少目标版本 | 无法访问 PyPI 时给出该判定。向用户索取 wheel 文件（要求见第 3 步），以该文件的版本作为 X，执行第 3 步 |

### 3. 迁移准备

1. 保存迁移前快照：

   ```bash
   $PY $S snapshot --out <快照目录> [--pid <vLLM 进程号>]
   ```

   快照包含依赖清单、相关环境变量、由已安装文件重新打包的 fastokens wheel，以及回退说明 `RESTORE.md`。回退时安装快照中的 wheel，以恢复同一构建：自行构建的 fastokens 可能与官方发行版版本号相同，相同版本号可能对应不同构建。

2. 将候选版本安装到独立目录（下文称候选安装目录），不变更服务环境。安装方式按环境条件确定：

   | 环境条件 | 安装方式 |
   |---|---|
   | pip 可访问 PyPI 或镜像源 | `$PY -m pip install --no-deps --target <候选安装目录> fastokens==X` |
   | 环境中无 pip，uv 可访问 PyPI 或镜像源 | `uv pip install --python "$PY" --no-deps --target <候选安装目录> fastokens==X` |
   | 无法访问 PyPI 或镜像源 | 由用户提供 wheel 文件，以 Python 标准库解包：`$PY -m zipfile -e <wheel 文件> <候选安装目录>` |

   向用户索取的 wheel 文件应满足以下要求：

   - fastokens 官方发行版，版本不低于 0.3.5，宜为 PyPI 上的最新版本；
   - 取自 PyPI 的 fastokens 文件列表（https://pypi.org/project/fastokens/#files），文件类型为 wheel（`.whl`）；
   - 文件名中的平台标签与服务所在机器一致：Linux 上为 manylinux 标签（例如 `manylinux_2_28_aarch64`），架构与第 1 步输出的架构一致，标签中的 glibc 版本不高于第 1 步输出的 libc 版本。

   wheel 文件名中的版本即为 X。解包后的候选安装目录应包含 `fastokens` 包目录与 `fastokens-X.dist-info` 目录。解包过程不检查平台兼容性，兼容性由下一项加载测试确认。

   第 6 步的安装命令与 `RESTORE.md` 中的回退命令按上表所选的安装方式相应替换。无法使用 pip 或 uv 安装时，服务环境的安装与回退由用户按其发布流程（例如更新镜像）执行。

3. 执行加载测试：

   ```bash
   $PY $S probe --model <同上> --overlay <候选安装目录> [--target-version X --offline]
   ```

   无法访问 PyPI 时追加 `--target-version X --offline`。输出中“按 VLLM_USE_FASTOKENS=1 加载”一项应显示后端为 fastokens、版本为 X。该测试与服务使用相同的加载入口（vLLM 的 `get_tokenizer`）。加载测试失败时，服务以相同配置启动亦将失败，流程终止。

### 4. 生成回放数据

```bash
$PY $S make-sessions --out <回放文件> [--sessions <会话数>] [--turns <轮数>] [--block-chars <工具输出长度>] [--seed <随机种子>]
```

合成会话的结构：每个会话以一条 system 消息开头，每轮追加一条 user 消息与一条 assistant 消息，每隔一轮的 user 消息附带一段代码形式的工具输出；每条请求包含截至该轮的完整对话，因此历史文本段在后续请求中重复出现。默认参数为 8 个会话、每个会话 12 轮、工具输出 6000 字符、随机种子 0。会话数、轮数与工具输出长度宜按部署的流量特征设置；参数相同时生成的数据相同。

### 5. 分词器层对比

```bash
$PY $S verify --model <同上> --sessions <回放文件> --overlay <候选安装目录> \
    --orig-env <服务当前配置> --cache-mib <段缓存预算（MiB）> --out <输出目录>
```

参数说明：

- `--orig-env` 设定基线组的环境变量，使其与服务当前配置一致。服务当前使用 HF tokenizers 时取 `VLLM_USE_FASTOKENS=`（空值表示删除该变量）；服务当前已使用 fastokens 时，按服务配置设定 `VLLM_USE_FASTOKENS=1` 与 `FASTOKENS_SEGMENT_CACHE`。
- 服务使用 `--chat-template` 时，通过 `--chat-template` 传入同一文件。
- 计时应在无其他高负载任务的机器上进行。

两组分别在独立进程中运行，默认 3 轮，每轮轮换两组的执行顺序；第 1 轮按会话分配线程，进行并发检查。

验收：报告结论为“通过”时执行第 6 步。结论为“未通过”时，向用户报告所列问题，流程终止；此时服务未变更，不必回退。判为未通过的情形包括：

- token ID 不一致；
- 各轮结果不一致；
- 并发检查出现结果不一致或异常；
- 运行超时；
- 段缓存开启组生效的分词后端并非 fastokens；
- 段缓存开启组的命中数为 0。

### 6. 评估与切换方案

将分词耗时与内存占用逐项同用户给定的阈值比较；内存占用为单个分词器测试进程的 RSS。任一项未达标时，向用户报告结果，流程终止。全部达标时，向用户给出切换方案：

- 安装：`$PY -m pip install --no-deps --force-reinstall fastokens==X`；容器化部署时更新镜像。
- 环境变量：`VLLM_USE_FASTOKENS=1` 与 `FASTOKENS_SEGMENT_CACHE=<段缓存预算（MiB）>`，写入服务配置（启动脚本、容器环境或 systemd 单元），以传递至每个 API 进程；重启服务后生效。

用户完成切换后，以 `probe` 确认：各 vLLM 进程的环境变量包含上述设置，已加载 fastokens 原生库，迁移判定为“已是最新官方发行版”。无法访问 PyPI 时追加 `--target-version X --offline`。

### 7. 回退

切换后服务出现异常时，由用户按快照目录中的 `RESTORE.md` 回退：安装快照中的 wheel，恢复环境变量，重启服务。回退完成后，以 `probe` 核对 fastokens 原生库的 sha256 与环境变量是否与快照一致。

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
