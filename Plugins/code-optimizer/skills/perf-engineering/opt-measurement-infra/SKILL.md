---
name: opt-measurement-infra
description: >
  搭建"阶段门控"性能采集基建：被测程序打印结构化阶段标记，采集器 tail 日志按相位起停采集，
  并强制"采集与计时分离"。适用于需要按相位拿 CPU/内存/调用栈证据的场景。
  适用：要按相位采集性能数据、要搭采集器、profiler 会污染吞吐、需要 Go pprof /
  py-spy / perf 多语言采样、或采集器出现"缺容器整体退出/SIGINT 不落盘/端口不通"等问题。
  不适用反例：只需要一份干净的计时数字（那属于基线/量化）、或只做单次静态分析。
---

# 测量基建：阶段门控采集

## 何时使用

- 已经在 `opt-onboarding-and-baseline`（独立 PR 待合入）拿到基线后，需要按相位拿瓶颈证据。
- 需要同时覆盖 Go / Python / C++ / 原生进程的 CPU 与内存采样。
- 采集器报错：容器名对不上、pprof 端口不通、SIGINT 后没落盘、日志被截断。

## 核心原则

1. **采集与计时分离是硬约束。** 在线 profiler 有开销；干净轮产基线数字，采集轮只产瓶颈证据，两套数据永不相混。
2. **阶段标记做成契约。** 被测程序用"结构化 JSON 行 + `flush=True`"向 stdout 自报相位；采集器 `tail -F` 监听标记分段起停。
3. **按进程内语言选采样器**，别指望一个工具覆盖全链路：Go=pprof（拿 Go 栈与堆；**cgo 之下的 C++ 帧默认采不到**，
   需 perf 补原生栈，除非程序显式注册了 `runtime.SetCgoTraceback`）、Python=py-spy、原生=perf。
4. **缺目标显式报错 / 前置条件，别静默跳过**：目标缺失/命名错时，先**启动前筛出可用目标、记录缺失项**——
   要么显式降级（缺失记入 `missing_targets` 继续采其余），要么把「全部容器就绪」列为前置条件并**显式报错
   列出缺失容器名**。不得静默跳过、也不得无声整体退出。
5. **优雅停止**：`set -m` + 前台运行 + 不加 shell 级 `&`，保证 profiler 能收到 SIGINT 落盘。
6. **端口优先探测不写死**：宿主端口 ≠ 容器内端口。

## 步骤速览

阶段标记契约、`collect_phases.sh` 机制、SIGINT 原理与十条容器化采集坑**只在
`references/phase-gated-collector.md` 一处维护**，本节给步骤名与去向：

1. **定义阶段标记契约**：被测程序在相位开始打印 `{"kind","phase",...}` JSON + `flush=True` 到 stdout；
   无显式 `phase_ended` 时精确窗口以 summary 的 `phase_windows` 为准 → references §1。
2. **采集器按标记起停**：`tail -F` 见 ingestion → 起 ingest 采集器；见 qa → SIGINT ingest 起 qa；见
   `test complete` → SIGINT qa 落盘退出 → references §2。
3. **覆盖三维度 + 三保障**：进程级 CPU/RSS + pprof + py-spy + perf；缺目标显式报错、优雅停止、端口探测；
   mixed 模式重叠窗口要标明归因限制 → references §2/§5。
4. **采集轮跑法（先起采集器再跑负载）**：两会话靠同一日志文件耦合（路径完全一致）；起采集器前确保
   `run.log` 不存在或为空（stale 日志 + 快速重写会丢头部标记）→ references §2/§4。

## 门禁

- [ ] 阶段标记是结构化 JSON + flush，且消费方按契约解析
- [ ] 采集轮与计时轮严格分离，不混跑
- [ ] 采集器启动前筛出可用目标并记录缺失项：显式降级（记 missing_targets 继续）或把「全部容器就绪」列为前置条件并显式报错列出缺失名，不静默跳过
- [ ] SIGINT 能优雅落盘（`set -m` + 前台，无 shell `&`）
- [ ] pprof 端口用容器内端口或自动探测，不写死宿主端口
- [ ] 大体积产物（如 Go 堆 dump）有体积预判与关闭开关
- [ ] mixed 模式报告标明重叠窗口与归因限制；需纯相位数据时单相位分开运行
- [ ] 采集器与 benchmark 用同一日志文件路径（绝对路径一致），接口已演示
- [ ] 起采集器前确保 run.log 不存在或为空（stale 日志下 tail -F 截断感知是启发式，快速重写会丢头部标记）

## 产物

- `perf-data/<phase>/{meta.json, samples.csv, perf/, metrics.json, report.html}`
- `phase-markers.log`（供与 summary 的 `phase_windows` 交叉核对）

## 参考

- `references/phase-gated-collector.md`：`collect_phases.sh` 机制、SIGINT 原理、10 条容器化采集坑（29091 vs 9091、容器名精确匹配、gomem 11.5GB 等）。