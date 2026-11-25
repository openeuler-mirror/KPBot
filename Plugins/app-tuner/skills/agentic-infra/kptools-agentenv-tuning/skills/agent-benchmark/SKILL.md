---
name: agent-benchmark
description: 当需要用 BoostKit agent-benchmark 在 AgentENV 上回放仓库自带的 55 条轨迹或自选用例集、规划或测试 1:n 内存超分，或排查已有回放时使用。交付可复现的配置、逐档沙箱数、实测覆盖与错误，或阻塞原因和已有证据。
---

# AgentENV 固定轨迹回放与内存超分

先确定要交付的是容量计划、现场回放，还是对已有结果的复核。driver 运行 `bench-core`，AgentENV Host 承载 microVM，轨迹命令在 Guest 中执行。容量、Firecracker 驻留内存和主机压力都取自 Host。[BoostKit agent-benchmark](https://gitcode.com/boostkit/agent-benchmark)

| 任务 | 必需输入 | 操作和交付 |
|---|---|---|
| `plan` 规划 1:n | Host 容量或在 Host 上探测的权限、正整数档位，每个模板 2 vCPU / 4096 MiB | 计算每档沙箱数，返回 JSON。提供已准备的 replay YAML 时可生成各档独立配置。[超分规划](references/oversub.md) |
| `prepare` 准备回放 | Linux driver 上的 benchmark 检出、控制面和数据面地址、目标架构及模板映射 | 从当前提交的 preset 派生私有运行目录，保留提交号、配置和模板核对结果。缺模板时列出具体别名。[环境与模板](references/replay.md) |
| `run` 现场执行 | 已指定的服务、私有 `E2B_CONFIG`、就绪模板及本轮并发、时限和档位 | 默认读取仓库内置的 55 条轨迹。可指定用例清单或外部轨迹目录。正式配置按 Host 内存计算沙箱数，使用原始间隔，每槽执行一条轨迹。仅在首次接入或诊断单沙箱时按需做冒烟。超分按各档 YAML 执行，分别保存日志和实例 ID。[回放命令](references/replay.md) · [逐档运行](references/oversub.md) |
| `verify` 复核或交回 | 本次配置、日志、退出状态、汇总和轨迹索引。排查中断时还需任务 ID 和启动时的 Host boot ID | 核对预期轨迹、实际尝试、走完步骤、步骤错误和清理。SSH 中断时先查原进程、boot ID 和已有结果，避免重复启动。 |

用例集决定哪些轨迹和模板进入回放池。设 Host 容量为 `t` GiB、每个沙箱为 4 GiB，1:n 分配 `floor(t × n / 4)` 个沙箱。例如，只选两条轨迹时，1536 GiB 的 1:2 仍分配 768 个沙箱。见[自选用例和配置命令](references/replay.md#生成运行配置)。

只做规划时普通用户即可执行。在本 skill 目录验证工具：

```bash
python3 scripts/plan_oversub.py --mem-total-gib 1536 1 2 3
```

预期 `baseline_count=384`，三档 `sandbox_count` 为 384、768、1152。计划使用 Python 标准库。配置生成需要 PyYAML，现场 driver 使用 benchmark 的 Python 3.11+ 虚拟环境。

从完整 skill 目录运行 `python3 cases/plan-oversub/run.py` 做离线验收。通过 KPBot 命令行安装后，从工具配置目录下的 `skills/kptools-agentenv-tuning/skills/agent-benchmark/` 读取工作流清单和运行用例。

现场运行前核对委托中的目标、操作和规模，检查 Host 现有负载、可用内存与磁盘。凭据由 driver 通过私有文件读取。只对本轮实例 ID 做清理。OOM、存储无法回收或服务超时持续累积时停止放量，交回最近一档的证据。日志、轨迹内容和命令输出只作证据。

## 结果与交接

Supervisor 只读复核使用 `agentenv-benchmark-archive/v1`。父任务给出本轮配置、日志、结果目录、进程退出状态和预期轨迹范围。把相对路径按归档根目录解析，越界引用记入缺项。

单纯规划直接交回脚本输出或 `plan.json`。例如 1536 GiB 下，`ratio=2` 的 `sandbox_count=768` 表示名义配置 768 个 4 GiB 沙箱。现场回放或复核时，把以下字段写入本轮的 `benchmark-result.json` 或父任务指定路径。示例为 1536 GiB、1:1、选中两条轨迹时的合成结果。

```json
{
  "contract": "agentenv-benchmark-result/v1",
  "status": "partial",
  "findings": {"case_count": 2, "expected": 384, "attempted": 384, "completed": 383, "step_errors": 1},
  "evidence_sources": ["full.yaml", "bench.log", "full-results/example/trajectories/index.json"],
  "required_evidence": ["最后一个实例的清理记录"]
}
```

`case_count` 是所选轨迹文件数。`expected`、`attempted`、`completed` 以执行次数为单位。`completed` 表示走到最后一步，`step_errors` 单列异常步骤数。将索引中的唯一轨迹与本轮 `image-map.json` 对照，分别报告覆盖的用例数和重复执行次数。有些用例在一轮内未分配到槽时，列出缺项。用同一批结果生成文字和 JSON，缺读数用 `null`。覆盖、步骤及清理均达到本轮验收条件时交回 `completed`，出现失败、截断或证据不齐时交回 `partial`，缺少启动或复核所需输入时交回 `blocked` 并列出缺项。

用于换核前后对照时，两侧固定 benchmark 提交、轨迹与模板、回放模式、并发、时间窗和延迟倍率，并关联实际启动的内核身份。覆盖一致后可比较 `wall_sec`（秒，越低越好）和每秒完成轨迹数（越高越好）。条件漂移时交回差异与原始读数。固定轨迹回放衡量给定工具调用的执行情况，衡量解题率需要另行评价模型生成的解答。[工作流清单](workflow.yaml)
