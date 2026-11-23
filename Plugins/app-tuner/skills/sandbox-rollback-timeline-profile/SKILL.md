---
name: sandbox-rollback-timeline-profile
description: 沙箱回滚（Rollback）时序图采集与分析——在并发回滚的基准测试过程中，通过对 Cubelet/CubeShim/CubeMaster/CubeAPI 各组件日志做前后偏移切割提取增量，解析每个回滚请求在各子阶段（Rollback API latency、rollback-service、resolve-targets、derive-rootfs、RollbackSnapshot、DeleteVm、RestoreVm、ResetVm、ConnectAgent、restore-memory、persist-rootfs、network-bump、master 的 job-validate/runtime-ref 等）的耗时，建立阶段父子拓扑与顺序关系，最终绘制 Gantt 风格嵌套时序图（SVG）和 Excel 统计报表。适用于 CubeSandbox 回滚性能剖析、阶段级延迟归因、瓶颈定位。
---

# 沙箱回滚时序图采集与分析

你是一位性能剖析专家。你的任务是：在并发回滚的基准测试期间，采集各组件日志的增量片段，解析出每个回滚请求在多个子阶段的耗时统计，建立阶段间的父子包含与顺序拓扑关系，最终生成 Gantt 风格嵌套时序图和 Excel 报表。

## 时序图含义

"时序图"指：在测试 N 并发回滚 M 轮的过程中，日志会记录每个回滚过程的多个子阶段耗时（如 Rollback API latency、rollback-service、RollbackSnapshot、DeleteVm、RestoreVm、ResetVm、ConnectAgent、restore-memory、persist-rootfs、network-bump 等）。将这些子阶段耗时统计起来，按父子嵌套关系沿时间轴排列绘制为 Gantt 条形图，即可直观看到各阶段的时间占比和瓶颈所在。回滚场景的核心热点是 **RestoreVm（内存恢复）** 与 **vm-downtime（guest 停机窗口）**。

## 与创建时序图 skill 的关系

本 skill 是 `sandbox-timeline-profile` 的回滚孪生版本：

- **采集机制相同**：日志偏移切割（offsets-before/after + `tail -c +offset`）、host 100ms 采样、bpftrace、进程快照。
- **基准不同**：创建用 cube-bench（并发 create）；回滚用 `examples/snapshot-rollback-clone/bench_rollback_concurrency.py`（并发 rollback，支持 `-d` 脏页注入，见 §1.3）。
- **阶段拓扑不同**：回滚没有 CreatePodSandbox/CreateContainer 链条，取而代之的是 RollbackSnapshot 下的 DeleteVm/RestoreVm/ResetVm/ConnectAgent 与 guest 停机窗口；`sandbox-probe` 换成 `persist-rootfs`/`network-bump` 后处理链。

## 输入

| 参数 | 必需 | 说明 |
|------|------|------|
| profile_dir | 是 | 基准测试输出目录，包含 evidence/ 和 rollback-latency/ 子目录 |
| bench_script | 否 | 采集脚本路径，默认使用项目内的 run_rollback_profile.sh |
| out_dir | 否 | 时序图与报表输出目录，默认为 profile_dir 同级 |

## 整体流程

```
阶段 1: 数据采集 (run_rollback_profile.sh)
  ├── 记录各组件日志的偏移前快照 (offsets-before)
  ├── 启动 host 采样 (CPU/mem/ctxt/threads, 100ms 间隔)
  ├── 启动 bpftrace on-CPU profiling
  ├── 运行 rollback 基准（每个沙箱先自建快照，再并发回滚，按轮重复）
  ├── 记录日志偏移后快照 (offsets-after)
  └── 按偏移量切割出 delta 增量日志

阶段 2: 阶段统计 (analyze_rollback_profile.mjs)
  ├── 读取 rollback-latency/*.summary.json → 回滚总体延迟（wall_ms / per_rollback_ms）
  ├── 读取 cubelet_stat-delta.log → cubelet 各阶段 (groupStageStats by Callee)
  ├── 读取 shim_stat-delta.log → shim 各阶段 (groupStageStats by CalleeAction)
  ├── 读取 shim_req-delta.log → 回滚详细子阶段（嵌入式时间戳解析）
  ├── 读取 master-delta.log → CubeMaster ext_info 阶段（snapshot-rollback/job-validate/runtime-ref）
  ├── 构建阶段关系拓扑 CSV (parent/child/containment/sequence/additivity)
  └── 输出 stage-latency.csv + analysis JSON

阶段 3: 时序图渲染 (gen_timeline.py)
  ├── 从 Excel/CSV 读取阶段数据 (stage_id → avg_ms, parent_id, depth)
  ├── 按 LAYOUT 定义排列各阶段的时间偏移和绘制类型
  └── 渲染 SVG Gantt 图 (嵌套条形 + 颜色编码 + 图例)

阶段 4: Excel 报表 (generate_excel_charts.py / export_to_excel.py)
  ├── 写入多 sheet Excel (Bench总体/分轮时延/Cubelet阶段/Shim阶段/详细子阶段/CubeMaster阶段/Host资源)
  └── 生成 PNG 图表 (阶段对比柱状图/分轮时延折线图/Host资源时间线)
```

## 阶段 1：数据采集

### 1.1 日志偏移切割原理

与创建场景相同：测试前后分别记录每个日志文件的 `size` 和 `inode`，测试结束后用 `tail -c +offset` 切割出增量片段（delta log），确保只分析本次回滚测试产生的日志行。

**涉及的日志文件**：

| 名称 | 路径 | 说明 |
|------|------|------|
| cubelet_req | /data/log/Cubelet/Cubelet-req.log | Cubelet 请求日志 |
| cubelet_stat | /data/log/Cubelet/Cubelet-stat.log | Cubelet 统计日志（含 CostTime） |
| shim_req | /data/log/CubeShim/cube-shim-req.log | Shim 请求日志（含嵌入式时间戳） |
| shim_stat | /data/log/CubeShim/cube-shim-stat.log | Shim 统计日志（含 CostTime） |
| vmm | /data/log/CubeVmm/vmm.log | VMM 日志 |
| master | /data/log/CubeMaster/cubemaster-req.log | CubeMaster 请求日志 |
| api | /data/log/CubeAPI/cube-api-*.log | API 网关日志 |

**切割逻辑**：
- 若 inode 不变且 size 增长 → `tail -c +$((before_size + 1))` 提取增量
- 若 inode 变化或 size 缩小（日志轮转）→ 复制整个文件并记录 warning

### 1.2 host 采样

以 100ms 间隔采样 `/proc/stat`、`/proc/loadavg`、`/proc/meminfo`，记录 CPU 占用、上下文切换、运行线程数、shim/vmm 进程数等，用于关联资源瓶颈。

### 1.3 触发回滚基准

```
CASE_MATRIX="profile-rb-c1-n5 1 5 0
profile-rb-c5-n5 5 5 0
profile-rb-c10-n5 10 5 0"
```

格式：`case_name concurrency rounds dirty_mb`。每轮调用一次
`bench_rollback_concurrency.py -c <concurrency> -n 1 -d <dirty_mb> --no-header`，
捕获该轮的 wall_ms 与 per-rollback ms，写入 `rollback-latency/<case>.json` 的 raw 数组
（每个元素 = 1 轮）。`dirty_mb>0` 时每轮回滚前先向 guest 写脏页，用于量化 **内存恢复（RestoreVm）** 的成本。

## 阶段 2：阶段统计

### 2.1 日志格式

与创建场景相同的 JSON Lines 格式，`Callee`（Cubelet）/`CalleeAction`（Shim）为阶段名，
`CostTime` 为毫秒耗时，`InstanceId` 用于跨组件关联。回滚记录的 `Action` 为 `Rollback`。

```json
{"Timestamp": "2025-01-01T12:00:00.123Z", "Action": "Rollback", "Callee": "rollback-service", "CalleeAction": "RollbackSnapshot", "InstanceId": "sb-xxx", "CostTime": 45.2, "LogContent": "..."}
```

### 2.2 阶段统计方法

对每个组件的 delta 日志：
1. 过滤出 `Action == "Rollback"` 且 `InstanceId` 在回滚请求集合中的记录
2. 按 `Callee`（Cubelet）或 `CalleeAction`（Shim）分组
3. 对每组的 `CostTime` 计算统计量：count、avg、min、p50、p95、max

### 2.3 回滚详细子阶段

Shim 的 req 日志（`shim_req-delta.log`）包含嵌入式时间戳和事件标记，通过正则解析提取：

| 事件标记 | 解析出的字段 |
|---------|-------------|
| `rollback req start` | rollback_req_start_host_ms |
| `delete vm start` | delete_vm_start_host_ms |
| `delete vm finish` | delete_vm_finish_host_ms |
| `restore start` | restore_start_host_ms |
| `restore finish` | restore_finish_host_ms |
| `memory restore finish at:NN` | memory_restore_cumulative_ms |
| `agent is ready` | agent_ready_host_ms |
| `rollback req finish` | rollback_req_finish_host_ms |
| `{"msg":"[cube-strace]recv restore"}` | guest_restore_receive_ms |
| `{"msg":"restore by snapshot"}` | guest_restore_branch_ms |

派生指标（差值计算）：
- `rollback_request_total` = req finish - req start（等价于 RollbackSnapshot 窗口）
- `delete_vm` = delete finish - delete start
- `restore_ms` = restore finish - restore start（回滚主热点）
- `memory_restore_cumulative` = 从 req 起点累计到内存恢复完成
- `vm_downtime` = agent_ready - delete_vm_start（guest 停机窗口：删 VM 起到 agent 重新就绪）
- `guest_receive_to_restore_branch` = guest_restore_branch - guest_restore_receive
- `restore_path_unresolved` = shim_restore - guest_receive_to_restore_branch（诊断残差）

### 2.4 阶段拓扑定义

这是时序图的核心——定义各阶段的父子包含关系、顺序关系、可加性。拓扑定义见
`analyze_rollback_profile.mjs` 的 `definitions` 数组，字段同创建场景
（depth/component/id/parent/containment/group/index/kind/additivity/source）。

**完整阶段拓扑**（按 order 排序）：

```
api.rollback (depth 0, root)
└── master.snapshot-rollback (depth 1, CubeMaster 主计时)
    ├── master.job-validate (depth 2, PREPARING: 校验/锁/gen 分配)
    ├── cubelet.rollback-service (depth 2, 跨进程服务窗口)
    │   ├── cubelet.rollback-inner (depth 3, 派生残差)
    │   ├── cubelet.resolve-targets (depth 3, 阈值采样)
    │   ├── cubelet.mount-metadata (depth 3, S3 专用, 阈值采样)
    │   ├── cubelet.derive-rootfs (depth 3, FICLONE 派生)
    │   ├── shim.RollbackSnapshot (depth 4, Shim 回滚主窗口)
    │   │   ├── shim.DeleteVm (depth 5, VmDelete)
    │   │   ├── shim.RestoreVm (depth 5, VmResumeFromSnapshot, 主热点)
    │   │   │   └── hypervisor.restore-memory (depth 6, 读 guest 内存镜像)
    │   │   ├── shim.ResetVm (depth 5, reset_guest)
    │   │   ├── shim.ConnectAgent (depth 5, guest agent 重连)
    │   │   ├── shim.rollback-request-total (depth 5, 边界标记)
    │   │   └── shim.vm-downtime (depth 5, 重叠窗口)
    │   ├── cubelet.persist-rootfs (depth 3, 后处理)
    │   └── cubelet.network-bump (depth 3, 阈值采样, best-effort)
    └── master.runtime-ref (depth 2, RECOVERING: 运行时引用绑定)
```

**containment 关系类型**（沿用创建场景并补充）：
- `root` / `observed_within` / `remote_child` / `inside` / `partial_observation`
- `cross_component_inside` / `equivalent_window` / `overlapping_window`
- `derived_from` / `propagated_copy` / `aggregate_of`

### 2.5 输出 CSV 格式

`stage-latency.csv` 列与创建场景一致：

```
flow_order, depth, component, stage_id, stage_name, parent_stage_id,
hierarchy_path, containment_relation, sequence_group, sequence_index, previous_stage_id,
related_stage_ids, timing_kind, additivity, source_field, count, avg_ms,
min_ms, p50_ms, p95_ms, max_ms, notes
```

## 阶段 3：时序图渲染

### 3.1 LAYOUT 定义

`gen_timeline.py` 的 `LAYOUT` 数组定义每个阶段在时间轴上的绘制方式（回滚版）：

| 条目 | start_offset (ms) | kind | 说明 |
|------|-------------------|------|------|
| api.rollback | 0 | bar | 顶层链起点 |
| master.snapshot-rollback | 10 | bar | CubeMaster 主计时 |
| master.job-validate | 12 | bar | 校验/锁/gen |
| cubelet.rollback-service | 18 | bar | Cubelet 服务窗口 |
| cubelet.rollback-inner | 20 | bar | 派生残差 |
| cubelet.resolve-targets | 22 | bar | 目标解析 |
| cubelet.mount-metadata | 23 | parallel | S3 元数据挂载 |
| cubelet.derive-rootfs | 25 | bar | FICLONE 派生 |
| shim.RollbackSnapshot | 28 | same-call | 同一 Update 调用对端 |
| shim.DeleteVm | 30 | bar | 删除旧 VM |
| shim.RestoreVm | 33 | bar | 快照恢复（热点） |
| hypervisor.restore-memory | 34 | sub | 内存恢复 |
| shim.ResetVm | 46 | bar | Guest 重置 |
| shim.ConnectAgent | 49 | bar | Agent 重连 |
| shim.vm-downtime | 30 | span | guest 停机窗口 |
| shim.rollback-request-total | 28 | span | 请求边界 |
| cubelet.persist-rootfs | 55 | bar | 后处理 |
| cubelet.network-bump | 56 | parallel | 网络代际 bump |
| master.runtime-ref | 58 | bar | 运行时引用 |

**kind 类型**：`bar`（普通条形）、`same-call`（同一调用两端各自计时，虚线半透明）、
`parallel`（并行/独立执行，半透明）、`sub`（父阶段子项，缩高 + 连接线）、
`span`（累积/跨阶段标记）。

### 3.2 SVG 渲染规则

- 画布：1280px 宽
- 条形高度 24px，间隔 10px
- 组件颜色编码：API=#4ECDC4, CubeMaster=#FF6B6B, Cubelet=#45B7D1, CubeShim=#96CEB4, Hypervisor=#F9A825, GuestAgent=#DDA0DD, Derived=#FFEAA7
- 深度缩进：每级 18px
- 条形内显示 avg_ms（宽度 > 35px 时）
- 右侧显示备注（same-call/parallel/span 等）

## 阶段 4：Excel 报表

### 4.1 Excel Sheets

| Sheet | 内容 |
|-------|------|
| Bench总体 | 成功率/轮次/并发/脏页/wall_ms 与 per_rollback_ms 分位数 |
| 分轮时延 | 每轮（round）的 wall_ms/per_ms |
| Cubelet阶段 | rollback-service/rollback-inner/resolve-targets/derive-rootfs/persist-rootfs/network-bump 等 |
| Shim阶段 | RollbackSnapshot/DeleteVm/RestoreVm/ResetVm/ConnectAgent/RestoreMemory |
| 详细子阶段 | rollback_request_total/delete_vm/restore_ms/memory_restore/vm_downtime 等 |
| CubeMaster阶段 | snapshot-rollback/job-validate/runtime-ref |
| Host资源 | max_runnable/max_shims/max_iowait/max_system/max_ctxt 等 |

### 4.2 PNG 图表

- `stages_chart_*.png` — 各组件阶段 avg/P50/P95 对比柱状图
- `rounds_timeline_*.png` — 分轮时延折线图
- `host_resources_*.png` — Host 资源时间线

## 执行步骤

本 Skill 根目录记为 `SKILL_DIR`，所有脚本位于 `$SKILL_DIR/scripts/`：

| 脚本 | 作用 |
|------|------|
| `scripts/run_rollback_profile.sh` | 阶段 1 数据采集（日志偏移切割 + host 采样 + bpftrace + 触发回滚基准） |
| `scripts/tools/sample_host.sh` | 被采集脚本调用，100ms 间隔采样 /proc/stat、loadavg、meminfo |
| `scripts/tools/run_rollback_perf.sh` | 被采集脚本调用，实际执行回滚基准（逐轮驱动 bench_rollback_concurrency.py） |
| `scripts/analyze_rollback_profile.mjs` | 阶段 2 阶段统计与拓扑 CSV |
| `scripts/export_to_excel.py` | 阶段 3 生成 xlsx（Stage Latency + Summary sheet） |
| `scripts/gen_timeline.py` | 阶段 3 渲染 Gantt 时序图 SVG |
| `scripts/generate_excel_charts.py` | 阶段 4 一键生成多 sheet Excel + 3 张 PNG（内部自行调用 analyze） |

### 前置条件

1. 运行环境：CubeSandbox 基准测试目标机器，Cubelet/CubeShim/CubeMaster/CubeAPI 组件在运行且日志写在 `/data/log/` 下
2. 工具依赖：`node`（analyze 脚本）、`python3` + `openpyxl` + `matplotlib`（Excel/PNG/SVG）、`bpftrace`（on-CPU profiling，可选）、`cubesandbox` Python SDK（回滚基准脚本依赖）
3. 环境变量：
   - **必填**：`BASE_DIR`（CubeSandbox examples 目录）、`TEMPLATE_ID`（沙箱模板 ID）、`API_KEY`（CubeSandbox API 密钥）、`BENCH_SCRIPT`（`bench_rollback_concurrency.py` 路径，默认 `$BASE_DIR/snapshot-rollback-clone/bench_rollback_concurrency.py`）
   - **可选**：`OUT_DIR`（默认 `$BASE_DIR/rollback-profile-results`）、`CASE_NAME`（默认 `profile-rb-c1-n5`）、`LOG_DIR`（默认 `/data/log`）、`METRICS_URL`（默认 `http://127.0.0.1:9998/v1/metrics`）、`API_URL`（默认 `http://127.0.0.1:3000`）、`SHIM_PATTERN` / `VMM_PATTERN`（host 采样进程匹配模式）、`PYTHON_BIN`（默认 python3，需已安装 cubesandbox SDK）
   - 脚本不内置任何用户名路径、内部部署路径或占位凭证，未设置必填变量时会立即报错退出

### 移植适配点

本 skill 绑定 CubeSandbox 回滚场景。迁移到新环境时按序检查：
1. 上面环境变量是否匹配目标部署（日志/二进制/端口/模板 ID）
2. `analyze_rollback_profile.mjs` 的 `definitions` 阶段拓扑与日志字段名（Callee/CalleeAction/CostTime/InstanceId）——组件日志格式变化时需同步修改；尤其回滚的 `Action` 值（当前假设为 `Rollback`）与 Master 响应 `RollbackSnapshot_rsp:` 前缀
3. `gen_timeline.py` 的 `LAYOUT`（阶段 ID + 估计偏移）——阶段增删时同步修改
4. `run_rollback_profile.sh` 中 bpftrace 的进程名列表（cubelet/cubemaster/network-agent/vcpu*/cloud-hypervis/cube-vmm）
5. 回滚基准脚本 `bench_rollback_concurrency.py` 的 `-n 1` 逐轮驱动方式（每轮含 1 次 warm-up），输出行格式变化需同步修改 `run_rollback_perf.sh` 的解析

### 步骤 1：确认环境

1. 检查 profile_dir 是否存在 `evidence/` 和 `rollback-latency/` 子目录
2. 若不存在 → 需要先运行 `bash $SKILL_DIR/scripts/run_rollback_profile.sh` 采集数据（需在基准测试目标机器上执行）
3. 检查 evidence/ 下是否有 `*-delta.log` 文件

### 步骤 2：运行阶段统计

```bash
node $SKILL_DIR/scripts/analyze_rollback_profile.mjs <profile_dir> \
  --csv-out <profile_dir>/stage-latency.csv \
  --details-out <profile_dir>/rollback-details.jsonl \
  > <profile_dir>/analysis.json
```

输出：analysis.json（stdout 重定向）+ stage-latency.csv

### 步骤 3：生成 Excel

```bash
python3 $SKILL_DIR/scripts/export_to_excel.py <profile_dir>/analysis.json <profile_dir>/stage-latency.csv <out_dir>/rollback-profile-results.xlsx
```

### 步骤 4：渲染时序图 SVG

```bash
python3 $SKILL_DIR/scripts/gen_timeline.py <out_dir>/rollback-profile-results.xlsx
```

输出：`rollback-timeline.svg`（生成在 gen_timeline.py 所在目录）

### 步骤 5：生成完整报表（可选，替代步骤 2-4）

```bash
python3 $SKILL_DIR/scripts/generate_excel_charts.py <profile_dir> <out_dir>
```

输出：`timing_report_*.xlsx` + 3 张 PNG 图表

## 关键注意事项

1. **不可加性**：大多数阶段不可直接相加——父子阶段存在包含关系，同调用对端（same-call）是两端各自计时不重叠。`shim.DeleteVm + RestoreVm + ResetVm + ConnectAgent` 是顺序子阶段，但不覆盖连接与跨进程残差。
2. **跨进程计时不可相减**：CubeMaster→Cubelet 是跨进程调用，两端时钟不同源，不能逐样本相减得到"传输耗时"。
3. **停机窗口**：`shim.vm-downtime`（delete_vm_start → agent_ready）是 guest 完全不可用的窗口，与 DeleteVm/RestoreVm/ResetVm/ConnectAgent 重叠，不可与它们相加。
4. **部分观测阶段**：`resolve-targets`/`mount-metadata`/`network-bump`/`derive-rootfs` 只记录超过日志阈值的样本，count 不足时不能代表全部请求。
5. **脏页对比**：用 `dirty_mb` 从 0 扫到 2048/8192，对比 RestoreVm 与 vm-downtime 的增幅，即可量化"内存全量恢复"瓶颈（回滚读侧无增量优化）。
6. **日志轮转风险**：若测试期间日志发生轮转（inode 变化），delta 切割会回退为全量复制，需检查 `log-rotation-warnings.txt`。
