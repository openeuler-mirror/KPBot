---
name: sandbox-timeline-profile
description: CubeSandbox 创建性能剖析的一键 Workflow、路由与共享实现；按证据采集→阶段分析→SVG 渲染→Excel/PNG 报告串联四个聚焦 Skill，并保留原 run_c50_profile.sh 一键 cube-bench 兼容入口。适用于需要完整时序剖析或复用共享脚本时。
---

# CubeSandbox 创建时序剖析 Workflow

本 Skill 是薄编排与共享实现，不是第五个聚焦执行 Skill。实际任务应路由到下列单点入口：

| 阶段 | 聚焦 Skill | 输入 → 核心产物 |
|---|---|---|
| 1 证据采集 | `sandbox-profile-evidence-collect` | 外部 argv workload + 日志 → evidence、manifest |
| 2 阶段分析 | `sandbox-stage-latency-analyze` | evidence + startup-latency → analysis、CSV、JSONL、摘要 |
| 3 SVG 渲染 | `sandbox-timeline-svg-render` | analysis + CSV → timeline.svg、渲染元数据 |
| 4 报告 | `sandbox-profile-report` | analysis → 多 sheet Excel、三类 PNG、报告 manifest |

各 Skill 的 `scripts/run.sh` 都定位 `../sandbox-timeline-profile/scripts/run_split_skill.sh`；源码树和安装后的平铺 skills 目录布局一致。缺共享实现时入口明确 blocked，不复制四份解析/绘图代码。

## 一键 Workflow

### 已有证据

```bash
SKILL_DIR=Plugins/app-tuner/skills/sandbox-timeline-profile
bash "$SKILL_DIR/scripts/run_profile_workflow.sh" PROFILE_DIR [OUTPUT_DIR]
```

### 外部 workload 采集并完成后处理

```bash
bash "$SKILL_DIR/scripts/run_profile_workflow.sh" \
  --collect-external PROFILE_DIR --log-dir /data/log \
  --no-bpftrace -- COMMAND [ARG ...]
```

`--` 后参数原样执行，禁止 eval。调用方必须在 workload 退出前向 `PROFILE_DIR/startup-latency/` 写入同一 case 的 `<case>.summary.json` 和 `<case>.json`；采集器本身只生成 evidence，不会伪造这两份基准结果。每轮外部采集必须使用尚不存在 `startup-latency` 的 `PROFILE_DIR`，Workflow 会在采集前拒绝旧目录，并在采集后核对本轮唯一且配对的 case 文件。缺文件时保留 evidence、给出修复提示且不分析；外部命令失败会保留 evidence 并优先传播其退出码。

### 原 cube-bench 一键采集兼容

```bash
export BASE_DIR=/path/to/CubeSandbox/examples
export OUT_DIR=/path/to/profile-results
export TEMPLATE_ID=... API_KEY=... BENCH=... SHIM_BIN=... TASK_DIR=...
bash "$SKILL_DIR/scripts/run_profile_workflow.sh" --collect-legacy
```

也可只运行原入口 `scripts/run_c50_profile.sh`，环境变量、`RUNNER`、`CASE_MATRIX=profile-c50-n500 50 500` 和 `STARTUP_BEGIN` 触发语义保持。原 runner 仍是 `scripts/tools/run_cubesandbox_openeuler_template_perf.sh`，没有复制成新 Skill，也不依赖 lifecycle 分支新增入口。`BPFTRACE_MODE=auto|on|off` 控制可选追踪，默认 auto；外部采集默认 off。

## 共享脚本

| 脚本 | 职责 |
|---|---|
| `scripts/collect_evidence.sh` | offset/delta、轮转 warning、Host/process/metrics、可选 bpftrace、manifest |
| `scripts/run_c50_profile.sh` | 原 cube-bench 环境变量协议到共享采集器的兼容适配 |
| `scripts/analyze_c50_profile.mjs` | 唯一阶段统计与 CreateContainer 详细子阶段实现 |
| `scripts/run_stage_analysis.sh` | 临时目录校验后发布分析四件套和摘要 |
| `scripts/gen_timeline.py` | JSON/CSV → SVG；旧 XLSX 位置参数仍兼容 |
| `scripts/generate_excel_charts.py` | 只消费既有 analysis 的完整报告；旧 profile_dir 形状保留 |
| `scripts/export_to_excel.py` | analysis+CSV 的轻量兼容导出器，不运行分析器 |
| `scripts/run_profile_workflow.sh` | 四阶段一键编排 |

## 运行前置与 blocked

- 采集需要真实组件日志和调用方 workload；真机一键 runner 另需其原有环境变量与 cube-bench。
- 分析需要 Node；SVG 直读只需 Python 标准库；完整报告需要 openpyxl/matplotlib/numpy。
- 各阶段只消费上一阶段产物。存在 `evidence-manifest.json` 时仅 `status: success` 可进入分析；失败状态保留证据且不发布分析。没有 manifest 的旧目录仍可分析。缺输入、坏 JSONL、无可渲染阶段、缺分批/Host 数据或缺最终产物都会非零失败，不能静默跳过。
- SVG/metadata、analysis、CSV、report manifest 与 PNG/XLSX 的真实路径必须互不重合；入口在首次建目录、删除或写入前拒绝碰撞，保留原文件。
- `run_c50_profile.sh` 与 report 的旧命令形状可继续使用，但 report 的旧形式现在要求 `PROFILE_DIR/analysis.json` 已存在，不再隐式分析。

## 必须保留的判读纪律

1. 父子、same-call、并行、累计、派生残差和传播副本大多不可加；只有明确 sequential 的关系才可顺序相加。
2. 跨进程时钟不同源，CubeMaster→Cubelet 等计时不可逐样本相减成“传输耗时”。
3. network/storage/create-sandbox-metadata 是阈值触发部分观测，count 不足不代表全部请求。
4. 日志轮转时 delta 回退为全量复制，必须检查 `log-rotation-warnings.txt` 与 manifest。
5. API 正式请求和 CubeMaster warmup 样本口径不同，不能用 count 差异直接判定丢失。
6. CreateContainer 残差包含请求准备/RPC 等未解析部分，不等同于单一 start_exec_process 耗时。

完整日志字段、阶段拓扑与 SVG/报告解释见 [references/interpretation.md](references/interpretation.md)。tier-A 合成日志回归仍在 `cases/analyze-mock-logs/`；四个聚焦 Skill 各有独立 case。
