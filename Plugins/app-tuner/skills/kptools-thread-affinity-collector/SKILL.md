---
name: kptools-thread-affinity-collector
description: >
  运行期线程绑核采集器（只读，任意进程）：对任意进程持续采样每线程的
  CPU 允许掩码（Cpus_allowed_list）与实际落点（processor/utime/stime），
  产出三件配套产物——samples.jsonl（机器格式时间序列）、trace.log（人类
  可读事件日志：新线程/掩码变化/线程退出/心跳/终态名册/统计，cat 即懂）、
  topology_raw.txt（采样机拓扑快照）。当用户需要记录线程绑核、采样线程
  亲和性、采集线程掩码时间线、在 NPU 上采集线程分布数据、把线程数据
  采下来之后拿回来分析、观察运行期线程变化时触发。采样机的机器特定
  路径/凭据一律占位符化。只采集不出报告——判读与 HTML 报告由
  kptools-thread-affinity-analyzer 对本 skill 的输出目录生成；单次瞬时
  快照由 kptools-proc-collector 覆盖。
license: MulanPSL-2.0
compatibility: Linux（openEuler 验证基线）；/proc 只读，同 uid 免 root；bash 4+；采样机的机器特定路径/凭据一律占位符化
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: thread_affinity
  version: "1.0.0"
---

# kptools-thread-affinity-collector — 运行期线程绑核采集器

本 skill 只做一件事：**把"运行期间每个线程绑在哪些核上"完整采下来**，
产出一份人能直接读懂的事件日志（trace.log）加一份机器可分析的时间序列
（samples.jsonl）。判读、异常发现与 HTML 报告由
`kptools-thread-affinity-analyzer` 基于输出目录生成——采集与职责分离，
两者可独立使用（目标机上只装 collector，采完把输出目录拿走即可）。

## 口径声明（先读）

- 双通道：**配置面**（`Cpus_allowed_list` 允许掩码时间线）+ **实际面**
  （`stat` 的 processor/utime/stime 落点与忙闲）
- 每个 tick 重新枚举 `/proc/PID/task/`（新线程即刻入采，无启动期名册）
- `processor` 是"最后运行所在核"快照：**迁移数为下界估计**（两次采样
  之间的多次迁移不可见），落点直方图交 analyzer
- 生存期短于一个采样间隔的线程采不到；事件日志以"线程翻动"指标提示
- `Cpus_allowed_list` 已含 cgroup cpuset 约束，容器视角即容器边界

## 工作流程

1. 目标机环境自检：`bash scripts/discover.sh`
2. 采集：`bash scripts/collect.sh -p <PID> -T 300`（或 `--auto` 自动发现
   python*/torchrun/vllm/raylet/deepspeed 类主进程；`--auto-list` 先列候选）
3. 产出目录内三件套；目标机上只装 collector 时，把输出目录整体取回，
   在任意机器上用 `kptools-thread-affinity-analyzer` 出报告

> 只读观测：不设置任何亲和性、不写目标进程任何状态。

## 采集命令

```bash
# 跟踪指定进程，默认 0.5s 一个 tick，Ctrl-C 停止（已采数据完整落盘）
bash scripts/collect.sh -p 12345

# 定长窗口：采样 300 秒后自动停止
bash scripts/collect.sh -p 12345 -T 300

# 自动发现训练/推理主进程（python*/torchrun/vllm/raylet/deepspeed，
# 取最新启动者）；不确定目标时先 --auto-list
bash scripts/collect.sh --auto -T 600 --out ./af_20261123

# 加密采样（迁移快的线程）；间隔下限 0.05s
bash scripts/collect.sh -p 12345 -i 0.1 -T 60
```

目标在容器里时，采样器必须与目标在**同一个 PID namespace** 内运行。

## trace.log（人类可读事件日志）

事件驱动：只在**有变化**时输出（新线程 / 掩码变化 / 线程退出 / 目标
不可见与恢复），无变化时每 50 tick 一行心跳证明采集活着；结束附终态
名册与统计。`cat trace.log` 或 `tail -f` 直接读：

```
==== kptools-thread-affinity-collector 线程绑核采集日志 ====
目标    : pid=12345 (python3.11)
拓扑    : 96 CPU / 2 NUMA 节点( node0:0-47 node1:48-95) / NPU:2 台(0000:8f:00.0(node1),...)
口径    : 间隔 0.5s；允许掩码=配置面 / 落点=实际面（处理器快照，迁移数为下界）
======================================================================
[10:21:33] #1 在册 9 线程（首 tick 全部视为新线程入册）
[10:21:35] #4 掩码变化 tid=302 (omp_worker) : 0-127 → 0-7
[10:21:40] #10 + 新线程 tid=623 (flash) 允许核=0-127
[10:22:20] #50 心跳：9 线程在册，无变化
[10:22:31] #60 - 线程退出 tid=610 (flash)（在册 #10-#57）
[10:23:33] #120 ! 目标进程不可见（开始计数，连续 10 次判定结束）
[10:24:00] #140 结束：原因 timeout，共 600 tick
==== 线程终态名册（8 个线程仍在册）====
tid      comm               允许核       在册区间
12345    python3.11         0            #1-#600
302      omp_worker         0-7          #4-#600
==== 统计 ====
新线程 2 / 线程退出 1 / 掩码变化 5 / 目标不可见 2 tick

完整判读与 HTML 报告：python3 .../kptools-thread-affinity-analyzer/scripts/analyze.py <本目录>
```

日志事件由持久化状态（输出目录内 `.trace_state.tsv`，结束后自动删除）
驱动，`--tick` 跨进程调用同样生效。

## 输出契约

```
<out>/
├── trace.log            # 人类可读事件日志（cat 即懂）
├── samples.jsonl        # 机器格式时间序列：首行 meta，中间每行一个 tick，run 模式末行 end
├── topology_raw.txt     # 采样机拓扑快照（=== 分节：clk_tck/NUMA/NPU PCI/cpuset/线程环境变量）
└── .trace_state.tsv     # 事件状态（run 结束/中断后自动删除；--tick 模式保留供续采）
```

- stdout：每 50 tick 一行进度 + 结束原因 + 数据路径；stderr：诊断
  （目标丢失计数、权限错误）
- 退出码：`0` 成功；`1` 参数错误；`2` 环境不可用（/proc 不可读）；
  `3` 启动时目标不存在 / tick 时目标已消失；`130` 信号中断（已采数据
  完整落盘）

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标进程（与 --auto 互斥） | - |
| `--auto` / `--auto-list` | 自动发现 / 列出候选主进程 | - |
| `-i <sec>` | 采样间隔（下限 0.05） | 0.5 |
| `-T <sec>` | 时长上限，0=不限直至目标结束/信号 | 0 |
| `--tolerate <N>` | 目标消失连续 N tick 才收尾 | 10 |
| `--out <dir>` | 输出目录（run 模式须为空或不存在） | `af_<UTC时间>` |
| `--init` / `--tick` | 内部子命令：只建 meta+topology / 只追加一个 tick（外部编排分步采集用） | - |
| `--proc-root <dir>` | procfs 根（测试注入用） | /proc |

## 约束

- 只读：不设亲和性、不写目标任何状态
- 同 uid 免 root；跨 uid 需 root
- 机器特定路径/凭据不落文档：拓扑/凭据均在采样机环境内，本文档只给
  方法与口径
- 采样机的 NUMA/NPU 拓扑快照在 `topology_raw.txt`——分析机器与采样
  机器不同时，报告的 NUMA/NPU 归属以该快照为准（跨机取回使用时必须
  带上）

### 和同类 skill 的分界

- 单次瞬时快照（ps -L 一把抓）由 `kptools-proc-collector` 覆盖；本 skill
  是持续时间线采集
- 判读/异常发现/HTML 报告由 `kptools-thread-affinity-analyzer` 覆盖；
  本 skill 只采集
- 绑核动作与策略由 `cpu-affinity-optimization` 覆盖

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 3 + `TARGET_MISSING` | 启动时进程不存在；确认 PID 或用 `--auto-list` |
| 退出码 2 + `PROCFS_UNREADABLE` | /proc 不可读或跨 uid；用 root 重跑 |
| stderr `target gone (N/M)` | 目标消失计数中，未达 `--tolerate` 前继续采样；达阈值后以 reason=target_gone 结束 |
| `--auto` 报 AUTO_NO_CANDIDATE | 没有 python*/torchrun/vllm 类进程；候选按 **comm 前缀**匹配（`python*` 盖住 python3.11 等版本化形态），用 `-p` 指定或 `--auto-list` 排查 |

## 参考资料

- [采集操作指南](references/collection-guide.md) — 长任务模式、时机选择、跨机取回、超时纪律
- 数据契约（samples.jsonl 行格式/topology 分节）：见 `kptools-thread-affinity-analyzer` 的 references/data-contract.md
