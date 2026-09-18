---
name: kptools-proc-collector
description: >
  采集进程/线程级性能数据：线程-CPU 分布、线程均衡性、上下文切换、CPU 迁移、
  CPU 亲和性掩码、进程内存映射与 DSO 依赖、进程 NUMA 归属、cgroup 资源限制、
  二进制 ISA 架构、线程池环境变量（OMP/MKL_NUM_THREADS）。当用户需要分析线程绑核、
  线程在哪些核上运行、上下文切换率、线程迁移、taskset 亲和性、进程加载了哪些库、
  容器 cgroup 限制、可执行文件架构信息、OMP 线程数是否过提交时触发。
license: GPL-3.0
compatibility: Linux, cgroup v1/v2
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: process_thread
  version: "1.0.0"
---

# kptools-proc-collector — 进程/线程级数据采集

## 工作流程

1. 检查工具可用性：`bash scripts/discover.sh`（ps 必有；pidstat/perf/taskset/numastat/readelf/ldd 为可选，缺失时对应模式降级）
2. 工具缺失时：`bash scripts/deploy.sh`（root 自动通过包管理器安装并复验；非 root 打印安装命令，需用户确认后安装），安装成功后重试采集一次；仍失败则向用户说明缺失影响并给出安装命令，不要静默跳过
3. 执行采集：`bash scripts/collect.sh -p <PID> --<mode>`
4. 辅助分析：`bash scripts/check_balance.sh -p <PID>`、`bash scripts/parse_maps.sh -p <PID>`

## 采集命令

```bash
# 线程-CPU 分布快照（pid,tid,psr,pcpu,stat,comm,wchan）
bash scripts/collect.sh -p <PID> --threads

# 每线程 CPU 使用率 / 上下文切换（时长 -d 秒，默认 5）
bash scripts/collect.sh -p <PID> --cpu -d 5
bash scripts/collect.sh -p <PID> --ctx -d 5

# CPU 迁移与切换计数（perf stat，每秒间隔）
bash scripts/collect.sh -p <PID> --migration -d 5
bash scripts/collect.sh -p <PID> --migration -d 5 --tracepoint   # 附加 sched_migrate_task，需 root

# CPU 亲和性（主进程 + 每线程）
bash scripts/collect.sh -p <PID> --affinity

# 内存映射 / NUMA 归属 / cgroup 限制 / 二进制信息 / 线程池环境变量
bash scripts/collect.sh -p <PID> --maps
bash scripts/collect.sh -p <PID> --numa
bash scripts/collect.sh -p <PID> --cgroup
bash scripts/collect.sh -p <PID> --binary
bash scripts/collect.sh -p <PID> --env

# 全部模式顺序执行
bash scripts/collect.sh -p <PID> --all
```

## 辅助分析

```bash
# 线程-CPU 均衡性判定（输出 JSON：balance_status/hot_cpu_list/thread_distribution）
bash scripts/check_balance.sh -p <PID>
bash scripts/check_balance.sh -f <ps输出文件>

# 内存映射 → DSO 分类列表（main_binary/libc/third_party/other）
bash scripts/parse_maps.sh -p <PID>
```

均衡性判定规则（与 app-tuner cpu-affinity 分析一致）：按线程 psr 计数，`max-min >= 2` → `skewed`；单核 → `single_cpu_or_single_thread`；`hot_cpu_list` = 线程数最多的 CPU。

## 输出

- stdout: 原始采集数据（工具原生格式）或 JSON（check_balance/parse_maps）
- stderr: 诊断信息（数据来源标注、依赖缺失、权限错误）

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标进程（--binary/--cgroup 之外必填） | - |
| `-d <sec>` | 计时模式时长（--cpu/--ctx/--migration） | 5 |
| `--tracepoint` | --migration 附加 sched:sched_migrate_task tracepoint | 关闭 |
| `--all` | 全部模式顺序执行 | - |
| `--help` | 显示帮助 | - |

## 约束

- 跨 uid 目标进程：maps/numa_maps/environ/taskset/affinity 读取需同 uid 或 root
- perf 事件需 `perf_event_paranoid` 允许；`--tracepoint` 需 root/tracefs
- cgroup 输出解析目标 PID 自身所在 cgroup（`/proc/PID/cgroup` 的 `0::/path`），非根层级；v2 下 leaf 为 `max` 或控制器未委托时会向上回溯标注最近设置限制的祖先；v1 下 PID 路径目录不可见（容器命名空间视图）时回退读控制器挂载根并 stderr 标注，目录存在但文件不可读（权限问题）不回退
- 容器内采集仅见容器视角的 cgroup 与进程视图

## 错误处理

- `process does not exist` → PID 失效，确认目标进程
- `cannot read /proc/PID/...`（跨 uid）→ 切换同 uid 用户或 root 执行
- `pidstat not found (install: sysstat)` → 安装 sysstat 后重试对应模式
- `perf ... failed` → 检查 `perf_event_paranoid`/`kptr_restrict`；仍失败改用 --threads/--ctx 近似分析
- check_balance 输出 `ps_thread_view_empty` → 进程已退出或权限不足

## 参考

- [进程线程采集指南](references/proc-guide.md)
