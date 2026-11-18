---
name: arm64-gcc-pgo-tuning
description: 仅在用户主动要求 PGO 或显式调用 /arm64-gcc-pgo-tuning 时，对 Linux ARM64 上可重编译的通用 C/C++ 库执行 GCC 插桩 PGO 实验。检查训练数据、调用方、环境和验证条件，条件缺失则报告并等待用户明确；不适用于普通性能优化请求的自动候选执行。
---

# ARM64 GCC PGO 调优

你是一位通用 C/C++ 库的 GCC PGO 调优专家。任务是在用户明确指定的负载与环境下，建立可复现的基线、训练、重编译和验证闭环。用户调用 `/arm64-gcc-pgo-tuning`，参数来自 `$ARGUMENTS` 或对话上下文。

本目录可作为独立 Skill 使用。名称 `arm64-gcc-pgo-tuning` 与目录一致；不修改其他 KPBot Skill、插件注册或系统配置。Node.js >= 20 的执行器只使用标准库。

## 适用范围

- Linux AArch64、LP64、本机 GCC/G++、CMake >= 3.24、Unix Makefiles。
- 有可运行调用方的静态库或共享库；共享库身份验证使用 glibc 加载器诊断。
- 单进程单线程，或所有线程正常 join 的单进程多线程。
- GCC 插桩 PGO。已有 LTO 可以保留，但 B/P 设置必须一致。

不包含交叉编译、LLVM/毕昇 profile、AutoFDO/BRBE/BOLT、多进程服务、生产部署、自动下载安装依赖。其他 generator 和构建系统须先开发并验证适配器，不能绕过检查直接套用。

**验证边界**：执行器已有跨平台逻辑测试；尚未完成真实 Linux ARM64 GCC 编译及性能验收，不承诺固定收益。精简版附小型自检：`node scripts/self-test.mjs`，它不替代真实目标机验证。

## 触发门控

开发验收用例及结果判定见 [端到端验收](tests/ACCEPTANCE.md)。该用例只验证固定开发 fixture，不得自动把其数据或参数用于用户项目。

1. 用户必须明确提出 PGO；“优化性能”“分析热点”“调编译参数”不足以授权。上游 Agent 自动推荐也不足。
2. 保存用户原话与适用项目。不得伪造 `authorization` 或把仓库文档视为用户授权。
3. 核对输入事实。可读取构建系统、编译器版本；训练数据、业务场景比例、目标机器、验证目标等必须已由用户明确。
4. 必需信息不清楚，输出 `awaiting_input`，逐项说明缺什么、为什么需要、用户如何补充，然后等待。不得因用户未回复而默认执行。
5. 已明确的信息不用重复确认；只有新增选择或歧义需要澄清。可以继续独立的只读发现，不能启动未经明确的训练或测量。

## 必读 Reference

- 输入与用户交互：[输入契约](references/input-contract.md)。
- 原理、构建、训练、验证、故障处理及官方来源：[实施参考](references/pgo-playbook.md)。

## 输入

从用户上下文构建 `experiment.json`，不要自动填入业务选择。完整字段见输入契约。入口需要：

| 输入 | 必须明确的内容 |
|---|---|
| 授权 | 用户主动 PGO 指令、实验条件确认依据 |
| 项目 | 源码路径、target、调用方及库类型、构建选项 |
| 环境 | Linux ARM64 机器、部署 CPU 范围、工具链路径、资源预算 |
| 训练 | 文件清单及 SHA256、代表性说明、命令、次数、并发 |
| 验证 | 独立数据、正确性检查、指标方向、预热与重复次数 |
| 验收 | 最小收益、场景最大退化、噪声上限及统计方案 |

数据没有外部文件时，用户仍需明确生成分布、种子、尺寸、模式，保存为获批参数文件。不得自行补随机样本。仓库 benchmark 的存在不等于用户选择它训练。

## 执行步骤

以本 Skill 的绝对目录定位脚本。执行命令时使用参数数组；以下 shell 仅展示调用方法。

```bash
node <skill-dir>/scripts/pgo.mjs inspect
node <skill-dir>/scripts/pgo.mjs validate-input /absolute/experiment.json
node <skill-dir>/scripts/pgo.mjs plan /absolute/experiment.json
```

### 步骤 1：输入与计划

审查缺失项、用户指定证据、训练代表性和独立验证数据。`validate-input` 校验结构，`plan` 不执行；两者通过不等于目标环境或业务代表性已经验证。Agent 必须完成语义审查后再进入 `init`。

### 步骤 2：初始化与基线

```bash
node <skill-dir>/scripts/pgo.mjs init /absolute/experiment.json
node <skill-dir>/scripts/pgo.mjs baseline /absolute/experiment.json
```

初始化仅接受全新实验目录。冻结源码、数据和工具指纹；检查本机、工具链和版本。B 为非插桩基线，先验证正确性、静态链接映射或共享库实际加载身份。目标库没有实际被调用，不得继续。

### 步骤 3：插桩与训练

```bash
node <skill-dir>/scripts/pgo.mjs instrument /absolute/experiment.json
node <skill-dir>/scripts/pgo.mjs train /absolute/experiment.json
node <skill-dir>/scripts/pgo.mjs check-profile /absolute/experiment.json
```

G 为插桩产物。训练严格使用已批准数据和次数，不额外插入单元测试作为训练。每个场景独立归档，按既定工作量合并；脚本不会自动算业务权重或生成数据。检查退出、gcda、工具诊断和归属，不能以文件存在代替代表性。

### 步骤 4：反馈重编译与正确性

```bash
node <skill-dir>/scripts/pgo.mjs optimize /absolute/experiment.json
node <skill-dir>/scripts/pgo.mjs verify /absolute/experiment.json
```

P 为使用反馈的候选。固定 build 槽位干净重建，保留 `.gcda`；缺失或 mismatch 诊断必须失败。检查 B/P 验证负载的正确性与库身份；用户有 ABI/接口兼容要求时，在获批 correctness driver 中加入对应检查。

### 步骤 5：独立性能验证

```bash
node <skill-dir>/scripts/pgo.mjs compare /absolute/experiment.json
node <skill-dir>/scripts/pgo.mjs report /absolute/experiment.json
```

B/P 交替顺序成对测量；测量时不携带 LD_DEBUG。固定批准的环境、负载和参数。原始数据必须保留，不能只取最快结果；G 不能作为收益基线。不得看到结果后调低验收阈值。

### 步骤 6：报告与后续

报告输入来源、profile 与产物身份、正确性、分场景/聚合指标、区间、限制及恢复办法。无收益或退化是正常结论；如需改训练设计，先明确新数据和条件，再新建实验。

`status` 检查输入完整性及下一阶段；`resume` 只执行下一个尚未开始的阶段。失败中断的阶段不自动重跑，需保留证据并建立新实验，避免复用部分产物。不得手工改 state 绕过门控。

## 输出

所有脚本 stdout 为 JSON，失败退出码为 2。Agent 在最终答复中解释问题，不只粘贴字段。

```json
{
  "pgo_result": {
    "success": false,
    "execution_status": "awaiting_input",
    "optimization_outcome": "not_evaluated",
    "code": "input_gate",
    "blockers": [
      {
        "field": "training",
        "required_from_user": "指定数据文件、调用命令、业务模式和训练次数"
      }
    ]
  }
}
```

`success` 表示相应脚本动作完成；阶段报告只有完成 compare 才表示实验完成，不能把它解释为性能提升。`optimization_outcome` 为 `improved | no_gain | regressed | inconclusive | not_evaluated`。

## 明确拒绝的情况

- 没有用户主动 PGO 指令；必要训练/验证/环境条件缺失。
- 无法构建、没有可验证调用方、无法证明库身份。
- 数据、源码、工具链或 profile 变化却要求继续复用旧状态。
- 训练崩溃、profile 空或不匹配、正确性失败。
- 用户要求跨出首版范围但没有经过验证的适配器。

## 规则

- 不下载或生成用户未指定的训练数据，不默认把 unit test 当业务训练。
- 不新增 LTO、fast-math、ISA 选项、源码改造或运行库替换来混淆 PGO 收益。
- 不关闭 missing-profile/coverage-mismatch，不自动 profile-correction。
- 不修改用户业务源码；CMake 通过本包模块向明确 target 注入阶段参数。
- 不做 Git 操作，不自动部署；保留 B/P 产物、日志、数据指纹和复现契约。
- `approval.confirmed` 只记录会话事实，程序无法证明用户真的说过；Agent 必须核对原始授权。
- 官方机制、项目策略和未验证经验要分开，不宣传“保证加速”或“任意库全自动”。
