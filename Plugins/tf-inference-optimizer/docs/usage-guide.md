# tf-inference-optimizer 使用指南

本文档介绍 `tf-inference-optimizer` 插件（TF 纯 CPU 推理优化工作流）的使用方法。该工作流面向鲲鹏 aarch64 上的 TF 2.20 自研 fork（KDNN/ANNC），覆盖「环境验证 → 基线建立 → profiling → 优化迭代 → 报告」五阶段。

## 1. 前置准备

### 1.1 安装

```bash
# 项目级安装（安装到指定项目目录）
/path/to/KPBot/install.sh project opencode /path/to/你的TF项目

# 全局安装
/path/to/KPBot/install.sh global opencode
```

验证安装：

```bash
opencode debug skill | grep tf-
```

应看到 7 个 `tf-*` skill 已注册。

### 1.2 远程执行脚本（模板）

插件 `tf-env-verify` skill 的 `scripts/` 目录提供**脱敏脚本模板** `remote-exec.ps1` / `remote-exec.exp`。首次使用时由工作流在「环境询问」完成后自动拷贝到工作目录并填充真实 IP/堡垒机凭据，无需手动操作。

## 2. 快速开始

在 OpenCode 中一句话触发：

```
帮我优化 <模型名> 的推理性能，目标平台鲲鹏 aarch64
```

主 Agent 会自动进入 `tf-inference-optimizer` 主编排，按五阶段依次执行。

## 3. 完整工作流（五阶段）

```
环境验证 → 基线建立(极限测试) → profiling → 优化迭代(循环) → 报告
GATE①环境就绪  GATE②基线已建立  GATE③profiling已确认  GATE④正确性用例(每点)
```

### 阶段 0：环境验证（`tf-env-verify`）

主 Agent 逐项确认环境，**首次使用需要你配合填写**：

| 步骤 | 内容 | 你的操作 |
|---|---|---|
| 环境询问 | 本地/远程、目标 IP、堡垒机、认证方式 | 首次填写 |
| 产物确认 | 源码路径、**源码分支（向客户追问确认）**、binary、模型路径、输出目录 | 首次填写 |
| 测试方法确认 | 正确性测试用例、golden output、QPS 档位、资源约束、trace 方法 | 首次填写 |
| 脚本模板 | 拷贝 `remote-exec.ps1/.exp` 到工作目录并填充凭据 | 自动 |
| 写回记录 | 写入 `./AGENTS.md` | 自动 |

之后自动执行：链路自检 → binary 部署验证（`nm -C` kernel 符号）→ 进程/端口/gflags → server ready。

> 后续会话复用：已有记录时，主 Agent 只展示摘要让你**一键确认 [复用 / 更新 / 重填]**，不再逐项重问。

**GATE① 环境就绪**：全部通过才进入下一阶段。

### 阶段 1：基线建立（`tf-benchmark`，极限测试）

主 Agent 先做**端到端极限测试**（不要跳过直接 profiling）：

- 端到端压测（限 QPS）：记录各档 P99、CPU。
- QPS 容量扫描：找饱和点（actual_qps 落后 + CPU 逼近 80% + P99 跳高）。

产出当前 baseline 的容量上限 / P99 / CPU。

> ⚠️ **先测极限，再 profiling**：没有基线数据，后续所有优化收益都无法归因。

**GATE② 基线已建立**：容量/P99/CPU 已产出。

### 阶段 2：profiling（`tf-profile-collector`，多维采集）

1. 主 Agent 生成**采集计划**（每项写明「为什么采集」）→ 你**整体一键确认**。
2. 全部采集走 subagent：静态图 / trace / perf / topdown / objdump / 多线程。
3. 交叉验证（瓶颈定位类强制两维印证）。
4. 产出 **profiling 报告**（采集方法 / 抓取结果 / 结果分析）→ **你 review 确认**。

**GATE③ profiling 已确认**：报告已 review，结论可追溯。

### 阶段 3：优化迭代（逐优化点 subagent，核心）

主 Agent 按候选池串行处理每个优化点，**每个点走完整流程**，你在多个节点参与：

```
① 设计（subagent 产出方案）→ 你确认是否认同，不认同则讨论重设计
② 实施（subagent 写代码 + commit）
③ 外部构建：你 push + 构建（CI/开发者完成）；构建问题回传，修复 commit rebase 进特性 commit
④ 部署：你部署完成后通知 → 触发验证
⑤ 验证（单独 subagent）：正确性用例 + 三口径收益 + 负收益根因
⑥ 完成后问你：是否启动下一个候选优化点
```

**关键约束**：
- **滚动 baseline**：每个优化点基于上一优化点合入后的新 baseline 实施；「opt（带加速 flag）」已经是优化后的版本，它就是进一步优化的 baseline。
- **单变量原则**：每个优化点单独 commit + 单独验证。
- **正确性硬约束**：正确性用例必须全部通过（GATE④，口径由用户定义），不过即回退该点。
- **负收益根因分析**：负收益先分析噪声 vs 真实开销，不简单回退。

### 阶段 4：报告

主 Agent 汇总所有优化点结果，三口径交叉印证，手写 `results/OPTIMIZATION_WORK_SUMMARY.md`（含收益归因 + 未做候选点清单）。

## 4. 产物目录（`results/`）

所有工作流产物统一存到**工作目录根下的 `results/` 目录**，方便查阅和校验（不散落到 `.opencode/` 等隐藏目录）：

```
results/
├── baseline_state.json / optimization_points_state.json   # 状态
├── collection_plan.json / graph_profile.json              # profiling 产物
├── baseline_profile.json / cross_validation.json
├── profiling_report.md                                    # profiling 报告
├── metadata_*/  perf_*/                                   # trace / perf 原始数据
├── design/      rounds/                                   # 设计提案 / 验证结果
└── OPTIMIZATION_WORK_SUMMARY.md                           # 工作报告
```

## 5. 你的交互点汇总

| 环节 | 你的动作 |
|---|---|
| 环境信息 | 首次填写（后自动复用 + 一键确认） |
| 源码分支 | 向客户追问确认是否正确 |
| 测试方法 | 首次确认 |
| 采集计划 | 一键确认 |
| profiling 报告 | review 确认结论 |
| 每个优化点设计 | 认同 / 讨论重设计 |
| 构建 | 手动 push + 构建，回传问题 |
| 部署 | 手动部署后通知 |
| 每个优化点完成后 | 决定继续 / 停止 |

## 6. 关键概念

### 6.1 滚动 baseline

- baseline = **本轮优化的对照起点**，即「当前已合入的最新优化状态」，不是「无优化」的同义词。
- 带加速 flag 的版本（如 `--enable_kdnn=true --annc=true`）是**已优化后的状态**，它就是进一步优化的 baseline。
- 无加速 flag 的版本是「原生基线」，仅首次评估整体加速比时对照使用。
- 判断标准：**看「当前已合入了哪些优化点」，不看 flag/binary 名叫 baseline 还是 opt**。

### 6.2 三口径交叉印证

- op 级（trace）：融合生效没、哪个 op 省了多少。
- 端到端（benchmark）：P99 / CPU。
- 容量（QPS 扫描）：饱和点 QPS。

三者互相印证，避免单一口径误导。

### 6.3 负收益根因分析

负收益（≤0%）不立即回退：先复测排除噪声 → 根因定位（噪声 vs 真实开销）→ 分析能否消除 → 可消除则追加修复轮次，不可消除才 rejected（有据）。

## 7. 常见问题

**Q1：为什么 profiling 前要先做极限测试？**
没有基线数据，后续优化收益无法归因（最后会发现「连基线都没有」）。

**Q2：为什么优化点要逐个做，不批量？**
单变量原则：每个优化点单独 commit + 单独验证，避免多个优化点混在一起导致无法归因收益。

**Q3：构建/部署为什么要人工完成？**
构建依赖 CI 流水线/开发者，无法在 subagent 内完成。构建问题回传后修复 commit 会 rebase 进特性 commit。

**Q4：负收益为什么不一刀切回退？**
负收益可能是测试噪声，也可能是可消除的开销。先根因分析再决策，避免错误丢失重要优化点（如 QKV 延迟持平但 CPU 明确下降，本质仍有效）。

## 8. 参考

- 环境验证细节：`skills/tf-env-verify/SKILL.md`
- profiling 方法：`skills/tf-profile-collector/SKILL.md` + `references/`
- 验证归因：`skills/tf-result-verify/SKILL.md`
- 端到端案例：`skills/tf-inference-optimizer/references/optimization-work-summary.md`
