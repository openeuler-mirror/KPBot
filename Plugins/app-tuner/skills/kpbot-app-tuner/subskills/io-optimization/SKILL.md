---
name: io-optimization
description: 非 CPU 瓶颈统一预筛子 skill，基于 iostat、vmstat、sar、perf 等工具采集的 CPU、iowait、磁盘利用率、内存压力和内存带宽代理指标输出瓶颈分类；并对磁盘 I/O 与内存瓶颈做专项证据采集与方向分析，具体参数动作由 os-optimization 等相邻 skill 统一生成。网络瓶颈只做轻量判定并路由到 network-optimization，不展开网络侧深度分析。已实现真实采集逻辑（配套 detect_bottleneck.sh），工具缺失时保守回退。
---

# IO Optimization

本子 skill 承担两个职责：

1. **非 CPU 瓶颈统一预筛**：主 skill 必须先完成该预筛，再决定进入 CPU 深挖还是转向网络、磁盘、内存、硬件等路线。
2. **磁盘 I/O 与内存瓶颈的专项证据与方向分析**：当预筛结论落在磁盘或内存时，由本子 skill 采集专项证据、评估优化方向并路由到执行方；具体参数动作（含取值、验证与回退）由相邻 skill 统一生成。

网络瓶颈已拆分到 `network-optimization`：本子 skill 只保留网络轻量判定作为路由信号，网络侧深度分析（网卡、IRQ、队列、RSS/RPS/XPS、协议栈）全部移交 `../network-optimization/SKILL.md`。

## Recommended Inputs

- `scenario_description` — 场景描述
- `target_pid` — 目标进程 PID
- `baseline_metrics` — 基线测试结果（含 CPU 利用率、吞吐、延迟）
- `evidence_inputs` — 原始观测数据路径
- 观测时长建议（默认 10 秒）

## Analysis Rules

> **真源声明**：预筛判定逻辑的唯一事实来源是 `../../scripts/detect_bottleneck.sh`（含全部阈值与判定顺序）。本节是对脚本行为的描述，修改阈值时只改脚本，并同步更新本节和 `cases/unit-prescreen-classify/` 的断言；三处不一致时以脚本为准。

### 磁盘瓶颈判定（脚本已实现）

1. `iostat -xmd`：任一设备 `%util > 80` 判定 `disk_bandwidth`
2. `vmstat`：`wa > 10` 判定 `disk_io`（IO 等待优先于带宽判定）
3. 应用同步 IO 模式：若应用为同步写入（如 `fsync` 敏感），磁盘瓶颈门槛更低（人工补充判断，脚本不感知）

### 内存瓶颈判定（预筛层仅容量）

1. 预筛脚本只做容量压力判定：`free` 的 `used/total > 0.9` 输出 `memory_capacity`
2. 内存带宽代理（cache miss rate、numastat 远端访问占比）**不在预筛脚本内**，由 `scripts/collect_memory_evidence.sh` 专项采集后分析
3. `mbw` 或 `stream` 基准测试（如可用）作为人工补充证据

### 网络轻量判定（仅路由，不深挖；脚本已实现）

脚本自动判定（基于 `ss -tinp` 与 `/proc/net/dev` 采样）：

1. 目标进程 socket 的 `sendq` 或 `recvq` > 131072 字节（128 KiB）
2. 采样窗口内网络 drop 计数增量 > 0
3. `tx_pps > 50000` 且平均 `%irq > 10`（mpstat）

命中任一网络信号时，输出 `bottleneck_type=network_bottleneck` 并直接路由到 `../network-optimization/SKILL.md`；本子 skill 不输出网卡、队列、中断亲和等网络侧候选动作。`sar -n DEV` 带宽接近物理上限、TCP 重传率等属于人工补充检查，不参与脚本自动判定。

### 判定优先级（脚本顺序）

1. CPU 饱和（mpstat 聚合 busy core-percent > `cores * 85`；mpstat 缺失时降级为 pidstat 进程 CPU > `cores * 80`）
2. 网络（上述三信号）
3. 磁盘 IO 等待（`wa > 10`）
4. 磁盘带宽（`%util > 80`）
5. 内存容量（`used/total > 0.9`）
6. 均不满足且无降级说明时返回 `no_active_bottleneck`
7. 证据不足（工具缺失等降级）时返回 `unknown_bottleneck`

## Required Output

必须返回以下结论之一：

- `network_bottleneck`
- `disk_bottleneck`
- `memory_capacity_bottleneck`
- `memory_bandwidth_bottleneck`
- `cpu_bottleneck`
- `unknown_bottleneck`
- `no_active_bottleneck`

脚本内部允许先判定更细粒度的结果：

- `cpu`
- `disk_io`
- `disk_bandwidth`
- `network`
- `memory_capacity`
- `memory_bandwidth`
- `unknown`

再映射回当前框架高层结论。

输出格式为 JSON，至少包含：

- `bottleneck_type` — 高层结论（上述枚举之一）
- `legacy_bottleneck_type` — 兼容旧报告的结论字段，可为空
- `detailed_bottleneck_type` — 细粒度判定
- `evidence` — 可量化证据字段（见下方）
- `confidence` — 置信度（`high` / `medium` / `low`）
- `fallback_notes` — 降级说明

`evidence` 至少包含可量化字段：

- `network_total_sendq_bytes`
- `network_total_recvq_bytes`
- `network_rx_bytes_per_sec`
- `network_tx_packets_per_sec`
- `disk_util_pct`
- `iowait_pct`

## 必读 Reference

- 阈值口径、实测标定数据、负载复现方法与常见坑：`references/io-playbook.md`

## 采集脚本

本 subskill 提供以下只读采集脚本，均不做任何系统修改：

| 脚本 | 用途 | 操作性质 |
|------|------|---------|
| `../../scripts/detect_bottleneck.sh <pid> <duration>` | 主流程预筛入口：多指标瓶颈分类，输出统一 JSON 结论 | 只读采集 |
| `scripts/collect_io_evidence.sh [duration] [device]` | 磁盘专项深采：iostat -x 窗口统计、I/O scheduler、readahead、nr_requests、挂载选项、vm 写回参数、PSI IO | 只读采集 |
| `scripts/collect_memory_evidence.sh [duration]` | 内存专项深采：容量/swap 活动、numastat 远端缺失率、各 node 内存占用、perf cache miss rate、THP 模式、PSI memory | 只读采集 |

采集脚本特点：

- 列解析全部基于表头动态定位：覆盖实时 `mpstat`、`mpstat.txt` 存档（`collect_evidence_snapshot.sh` 落盘，供 NPU 馈送判定复用）、`iostat -x` 与 `pidstat`（按表头定位 `%CPU` 列），兼容 sysstat >= 12 的新列序（`%guest`/`%gnice`）与 AM/PM locale 时间前缀（按 `CPU`/`UID` 列位置计算偏移），不依赖固定列号或 `$(NF-n)` 相对偏移。
- 工具缺失时降级到 `notes` 字段，不伪造数据。
- 窗口口径统一：procps `vmstat` 的首个数据行是开机以来均值，解析跳过该行、只平均采样窗口（与 `iostat -x` 只取第二段的口径一致）；窗口为空（如 `DURATION=1`）时降级为 null 并写 `vmstat_since_boot_only` note。
- `iostat` 表头前缀匹配同时兼容 `Device:` 与 `Device` 两种 sysstat 版本形态；表头不可解析时降级写 `iostat_header_unparsed` note，不把解析失败伪装成"无活跃设备"。
- 预筛结论落在 `disk_bottleneck` 时，先跑 `collect_io_evidence.sh` 再评估方向并交接给动作产出方；落在内存瓶颈时，先跑 `collect_memory_evidence.sh`。

## Caller Behavior

主 skill 应按以下方式解释返回值：

- 若为 `network_bottleneck`，直接转交 `subskills/network-optimization/SKILL.md` 执行网络优化链路，本子 skill 不再补采网络证据
- 若为 `disk_bottleneck` 或内存瓶颈，进入本子 skill 的 IO 专项证据与路由流程（见下节）
- 若为 `cpu_bottleneck`，进入火焰图、热点函数、线程和 topdown 深挖
- 若为 `unknown_bottleneck`，补充采集，不得盲目调优
- 若为 `no_active_bottleneck`，输出报告并停止全局优化

如果工具缺失：

- 必须显式提示工具不足
- 采用保守判断
- 不得伪造瓶颈结论

## IO 专项证据与路由方向

预筛结论落在磁盘或内存时，本子 skill 负责采集专项证据、评估优化方向并路由到动作产出方；**本子 skill 自身不产出系统参数候选动作**。分析阶段只读；真实修改由动作产出方生成候选动作后，回到主流程串行执行验证阶段，并满足 `approved_execute` 门禁。

### 磁盘 I/O 证据与方向

本子 skill 采集并组织的证据（`collect_io_evidence.sh`）：

- 设备级指标：`iostat -x` 的 util/await/aqu-sz、r/s、w/s、rkB/s、wkB/s
- 设备当前参数：scheduler（含方括号当前值）、read_ahead_kb、nr_requests
- 挂载与文件系统现状：块设备挂载点、fstype、挂载选项
- 写回参数现状：`vm.dirty_ratio` / `vm.dirty_background_ratio` / `dirty_expire_centisecs` / `dirty_writeback_centisecs`、PSI IO
- 复测证据：`fio` 只读探测（如可用）

方向评估输出的是「证据指向哪个参数方向」（如 scheduler 切换、readahead 调整、挂载选项、写回参数权衡），**不附带取值与执行计划**；具体候选动作由 `os-optimization` 生成（其 playbook 含取值推荐表与 sysctl 回退样例），本子 skill 以 `evidence_refs` 附上采集证据交其使用。

### 内存优化方向

- 容量压力：swappiness 调整、大页/THP 路线评估（动作回流 `os-optimization` 统一执行）
- 内存带宽：NUMA 远端访问占比高时，转交 `../cpu-affinity-optimization/SKILL.md` 做设备-NUMA 对齐与绑核
- 应用层缓存/批量/队列配置：回流 `../application-config-optimization/SKILL.md`

### 与相邻 skill 的边界

- **`os-optimization`**：sysctl、queue、挂载选项、THP/HugePages 等系统参数候选动作的**唯一产出方**（含取值、验证与回退）。本子 skill 只负责瓶颈判定、专项证据采集和方向评估，以 `evidence_refs` 交接；两侧同时命中磁盘/内存方向时，本子 skill 不得对同一 sysctl key 或 queue 参数另行产出候选动作，避免双头产出、取值口径不一致与两套回退计划。
- **`network-optimization`**：网络判定只路由不深挖（见上文）。
- **`cpu-affinity-optimization`**：内存带宽/NUMA 跨节点问题的绑核方案由其输出。
- **`application-config-optimization`**：应用同步 IO 模式、缓存批量配置等应用层改造回流其处理。

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| `iostat` | 磁盘利用率 | 磁盘瓶颈判定降级 |
| `vmstat` | iowait 和系统概览 | IO 等待判定降级 |
| `sar` | 网络带宽统计 | 网络路由信号降级 |
| `perf` | 缓存和内存带宽分析 | 内存带宽判定降级 |
| `ip` | 网络错误和队列统计 | 部分网络路由信号降级 |

缺失任一工具时，对应子系统的瓶颈判定置信度降低，但不应阻止其他子系统的判定。

## Candidate Action Contract

本子 skill 默认只输出瓶颈分类、专项证据和路由建议（`route_to` + `evidence_refs`），不产出系统参数候选动作：

- scheduler、readahead、挂载选项、写回参数等系统参数动作由 `os-optimization` 唯一生成（含取值、验证与回退）；本子 skill 以 `evidence_refs` 交接证据，不重复生成执行细节。
- 归属 `cpu-affinity-optimization` 的绑核动作、归属 `application-config-optimization` 的应用层动作，同样只输出路由建议和证据引用。
- 仅当动作不属于任何相邻 skill 的职责且证据充分时，才允许输出 `candidate_actions[]`（每项必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`）。
- 证据不足时只输出 `required_evidence` 和下一步路由，不伪造优化动作。
