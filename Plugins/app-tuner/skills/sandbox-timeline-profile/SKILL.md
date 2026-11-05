---
name: sandbox-timeline-profile
description: 沙箱创建时序图采集与分析——在并发创建沙箱的基准测试过程中，通过对 Cubelet/CubeShim/CubeMaster/CubeAPI 各组件日志做前后偏移切割提取增量，解析每个沙箱创建请求在各子阶段（Create API latency、cubebox-service、sandbox-start、sandbox-create、CreatePodSandbox、LaunchVmm、RestoreVm、ResetVm、CreateSandbox、CreateContainer、sandbox-probe 等）的耗时，建立阶段父子拓扑与顺序关系，最终绘制 Gantt 风格嵌套时序图（SVG）和 Excel 统计报表。适用于 CubeSandbox 性能剖析、阶段级延迟归因、瓶颈定位。
---

# 沙箱创建时序图采集与分析

你是一位性能剖析专家。你的任务是：在并发创建沙箱的基准测试期间，采集各组件日志的增量片段，解析出每个沙箱创建请求在多个子阶段的耗时统计，建立阶段间的父子包含与顺序拓扑关系，最终生成 Gantt 风格嵌套时序图和 Excel 报表。

## 时序图含义

"时序图"指：在测试 N 并发创建 M 个沙箱的过程中，日志会记录每个沙箱创建过程的多个子阶段耗时（如 Create API latency、CreatePodSandbox、LaunchVmm、RestoreVm、ResetVm、CreateSandbox、CreateContainer、sandbox-probe 等）。将这些子阶段耗时统计起来，按父子嵌套关系沿时间轴排列绘制为 Gantt 条形图，即可直观看到各阶段的时间占比和瓶颈所在。

## 输入

| 参数 | 必需 | 说明 |
|------|------|------|
| profile_dir | 是 | 基准测试输出目录，包含 evidence/ 和 startup-latency/ 子目录 |
| bench_script | 否 | 采集脚本路径，默认使用项目内的 run_c50_profile.sh |
| out_dir | 否 | 时序图与报表输出目录，默认为 profile_dir 同级 |

## 整体流程

```
阶段 1: 数据采集 (run_c50_profile.sh)
  ├── 记录各组件日志的偏移前快照 (offsets-before)
  ├── 启动 host 采样 (CPU/mem/ctxt/threads, 100ms 间隔)
  ├── 启动 bpftrace on-CPU profiling
  ├── 运行 cube-bench (并发创建沙箱)
  ├── 记录日志偏移后快照 (offsets-after)
  └── 按偏移量切割出 delta 增量日志

阶段 2: 阶段统计 (analyze_c50_profile.mjs)
  ├── 读取 startup-latency/*.summary.json → benchmark 总体延迟
  ├── 读取 cubelet_stat-delta.log → cubelet 各阶段 (groupStageStats by Callee)
  ├── 读取 shim_stat-delta.log → shim 各阶段 (groupStageStats by CalleeAction)
  ├── 读取 shim_req-delta.log → CreateContainer 详细子阶段 (嵌入式时间戳解析)
  ├── 读取 master-delta.log → CubeMaster ext_info 阶段
  ├── 构建阶段关系拓扑 CSV (parent/child/containment/sequence/additivity)
  └── 输出 stage-latency.csv + analysis JSON

阶段 3: 时序图渲染 (gen_timeline.py)
  ├── 从 Excel/CSV 读取阶段数据 (stage_id → avg_ms, parent_id, depth)
  ├── 按 LAYOUT 定义排列各阶段的时间偏移和绘制类型
  └── 渲染 SVG Gantt 图 (嵌套条形 + 颜色编码 + 图例)

阶段 4: Excel 报表 (generate_excel_charts.py / export_to_excel.py)
  ├── 写入多 sheet Excel (Bench总体/分批时延/Cubelet阶段/Shim阶段/详细子阶段/CubeMaster阶段/Host资源)
  └── 生成 PNG 图表 (阶段对比柱状图/分批时延折线图/Host资源时间线)
```

## 阶段 1：数据采集

### 1.1 日志偏移切割原理

并发测试期间，各组件持续写日志。采集脚本在测试前后分别记录每个日志文件的 `size` 和 `inode`，测试结束后用 `tail -c +offset` 切割出增量片段（delta log），确保只分析本次测试产生的日志行。

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

### 1.3 触发基准测试

```
CASE_MATRIX="profile-c50-n500 50 500"
```
即 50 并发创建 500 个沙箱，cube-bench 工具输出 startup-latency/*.json（含每个请求的 create_ms）。

## 阶段 2：阶段统计

### 2.1 日志格式

Cubelet/Shim 的 stat 日志为 JSON Lines 格式，每行包含：

```json
{"Timestamp": "2025-01-01T12:00:00.123Z", "Action": "Create", "Callee": "cubebox-service", "CalleeAction": "CreatePodSandbox", "InstanceId": "sb-xxx", "CostTime": 45.2, "LogContent": "..."}
```

- `Callee` (Cubelet) / `CalleeAction` (Shim)：阶段名称
- `CostTime`：该阶段耗时（毫秒）
- `InstanceId`：沙箱实例 ID，用于跨组件关联

### 2.2 阶段统计方法

对每个组件的 delta 日志：
1. 过滤出 `Action == "Create"` 且 `InstanceId` 在创建请求集合中的记录
2. 按 `Callee`（Cubelet）或 `CalleeAction`（Shim）分组
3. 对每组的 `CostTime` 计算统计量：count、avg、min、p50、p95、max

### 2.3 CreateContainer 详细子阶段

Shim 的 req 日志（`shim_req-delta.log`）包含嵌入式时间戳和事件标记，通过正则和 JSON 嵌套解析提取：

| 事件标记 | 解析出的字段 |
|---------|-------------|
| `create req start` | create_req_start_host_ms |
| `start vm start` | start_vm_host_ms |
| `agent is ready` | agent_ready_host_ms |
| `create req finish` | create_req_finish_host_ms |
| `load spec finish at:NN` | load_spec_cumulative_ms |
| `start sandbox finish at:NN` | sandbox_finish_cumulative_ms |
| `start container finish at:NN` | container_finish_cumulative_ms |
| `{"msg":"[cube-strace]recv create container"}` | guest_create_receive_ms |
| `{"msg":"create container by restore"}` | guest_restore_branch_ms |
| `created container!, add_devices: Nms, ...` | normal path 子阶段 |

派生指标（差值计算）：
- `create_request_total` = finish - start
- `vm_start_to_agent_ready` = agent_ready - start_vm
- `sandbox_to_container_finish` = container_finish_cumulative - sandbox_finish_cumulative
- `guest_receive_to_restore_branch` = guest_restore_branch - guest_create_receive
- `restore_path_unresolved` = shim_create_container - guest_receive_to_restore_branch（诊断残差）

### 2.4 阶段拓扑定义

这是时序图的核心——定义各阶段的父子包含关系、顺序关系、可加性。拓扑定义见 `analyze_c50_profile.mjs` 的 `definitions` 数组，每个阶段包含：

| 字段 | 说明 |
|------|------|
| depth | 嵌套深度（0=顶层，越大越深） |
| component | 所属组件（API/CubeMaster/Cubelet/CubeShim/GuestAgent/Derived） |
| id | 阶段唯一标识 |
| parent | 父阶段 ID |
| containment | 包含关系类型 |
| group / index | 顺序分组及组内序号 |
| kind | 计时类型 |
| additivity | 可加性 |
| source | 数据来源字段 |

**完整阶段拓扑**（按 order 排序）：

```
api.create (depth 0, root)
└── master.cube-e2e (depth 1, CubeMaster 主计时)
    └── cubelet.cubebox-service (depth 2, 跨进程服务窗口)
        ├── cubelet.cubebox-service-inner (depth 3, 派生残差)
        └── cubelet.cubebox (depth 3, Cubelet 工作流外层)
            ├── cubelet.network (depth 4, 阈值采样)
            ├── cubelet.storage (depth 4, 阈值采样)
            ├── cubelet.create-sandbox-metadata (depth 4, 元数据创建)
            ├── cubelet.sandbox-start (depth 4, 累计时段)
            │   └── cubelet.sandbox-create (depth 5, NewTask 调用)
            │       └── shim.CreatePodSandbox (depth 6, Shim 创建主窗口)
            │           ├── shim.LaunchVmm (depth 7, 启动 VMM)
            │           ├── shim.RestoreVm (depth 7, 快照恢复)
            │           ├── shim.ResetVm (depth 7, 重置 Guest)
            │           ├── shim.CreateSandbox (depth 7, Guest Agent RPC)
            │           ├── shim.CreateContainer (depth 7, Guest Agent RPC, 主热点)
            │           │   ├── guest.receive-to-restore-branch (depth 8, 分支选择)
            │           │   └── derived.restore-path-unresolved (depth 8, 诊断残差)
            │           ├── shim.create-request-total (depth 7, 边界标记)
            │           └── shim.vm-start-to-agent-ready (depth 7, 重叠窗口)
            └── cubelet.sandbox-probe (depth 4, 就绪探针)
                └── master.sandbox-probe (depth 5, 传播副本)
    └── master.all-probe (depth 2, 聚合探针)
```

**containment 关系类型**：
- `root` — 顶层根阶段
- `observed_within` — 在父阶段内观测到
- `remote_child` — 跨进程子调用
- `inside` — 直接包含在父阶段内
- `partial_observation` — 部分观测（仅超阈值样本）
- `cross_component_inside` — 跨组件包含
- `equivalent_window` — 等效窗口
- `cumulative_from_origin` — 从原点累计
- `derived_from` — 派生
- `aggregate_of` — 聚合
- `propagated_copy` — 传播副本

### 2.5 输出 CSV 格式

`stage-latency.csv` 列：

```
flow_order, depth, component, stage_id, stage_name, parent_stage_id,
hierarchy_path, containment_relation, sequence_group, sequence_index, previous_stage_id,
related_stage_ids, timing_kind, additivity, source_field, count, avg_ms,
min_ms, p50_ms, p95_ms, max_ms, notes
```

## 阶段 3：时序图渲染

### 3.1 LAYOUT 定义

`gen_timeline.py` 的 `LAYOUT` 数组定义每个阶段在时间轴上的绘制方式：

| 条目 | start_offset (ms) | kind | 说明 |
|------|-------------------|------|------|
| api.create | 0 | bar | 顶层链起点 |
| master.cube-e2e | 16 | bar | CubeMaster 主计时 |
| cubelet.cubebox-service | 23 | bar | Cubelet 服务窗口 |
| cubelet.cubebox | 38 | bar | Cubelet 工作流 |
| cubelet.sandbox-start | 45 | bar | sandbox 启动 |
| cubelet.sandbox-create | 46 | bar | NewTask 调用 |
| shim.CreatePodSandbox | 50 | same-call | 同一 gRPC 调用对端 |
| shim.LaunchVmm | 51 | bar | VMM 启动 |
| shim.RestoreVm | 52 | bar | 快照恢复 |
| shim.ResetVm | 63 | bar | Guest 重置 |
| shim.CreateSandbox | 67 | bar | Sandbox RPC |
| shim.CreateContainer | 68 | bar | Container RPC |
| guest.receive-to-restore-branch | 68.0 | sub | CreateContainer 子阶段 |
| derived.restore-path-unresolved | 69.0 | sub | 诊断残差 |
| cubelet.storage | 45 | parallel | 并行执行 |
| cubelet.network | 45 | parallel | 并行执行 |
| cubelet.sandbox-probe | 85 | bar | 就绪探针 |
| master.all-probe | 85 | bar | 聚合探针 |

**kind 类型**：
- `bar` — 普通条形
- `same-call` — 同一 gRPC 调用两端各自计时，虚线半透明
- `parallel` — 并行执行，半透明
- `sub` — 父阶段子项，缩高 + 连接线
- `span` — 累积/跨阶段标记

### 3.2 SVG 渲染规则

- 画布：1280px 宽，时间轴 0–130ms
- 条形高度 24px，间隔 10px
- 组件颜色编码：API=#4ECDC4, CubeMaster=#FF6B6B, Cubelet=#45B7D1, CubeShim=#96CEB4, GuestAgent=#DDA0DD, Derived=#FFEAA7
- 深度缩进：每级 18px
- 条形内显示 avg_ms（宽度 > 35px 时）
- 右侧显示备注（same-call/parallel 等）

## 阶段 4：Excel 报表

### 4.1 Excel Sheets

| Sheet | 内容 |
|-------|------|
| Bench总体 | 成功率/吞吐/QPS/延迟分位数 |
| 分批时延 | 每50个请求一批的 avg/P50/P95/max |
| Cubelet阶段 | cubebox-service/cubebox/sandbox-start/sandbox-create/sandbox-probe 等 |
| Shim阶段 | CreatePodSandbox/LaunchVmm/RestoreVm/ResetVm/CreateSandbox/CreateContainer |
| 详细子阶段 | create_request_total/vm_start_to_agent_ready/sandbox_to_container_finish 等 |
| CubeMaster阶段 | cube-e2e/sandbox-probe/all-probe |
| Host资源 | max_runnable/max_shims/max_iowait/max_system/max_ctxt 等 |

### 4.2 PNG 图表

- `stages_chart_*.png` — 各组件阶段 avg/P50/P95 对比柱状图
- `blocks_timeline_*.png` — 分批时延折线图
- `host_resources_*.png` — Host 资源时间线（runnable/CPU/ctxt/threads）

## 执行步骤

本 Skill 根目录记为 `SKILL_DIR`，所有脚本位于 `$SKILL_DIR/scripts/`：

| 脚本 | 作用 |
|------|------|
| `scripts/run_c50_profile.sh` | 阶段 1 数据采集（日志偏移切割 + host 采样 + bpftrace + 触发 cube-bench） |
| `scripts/tools/sample_c50_host.sh` | 被采集脚本调用，100ms 间隔采样 /proc/stat、loadavg、meminfo |
| `scripts/tools/run_cubesandbox_openeuler_template_perf.sh` | 被采集脚本调用，实际执行 cube-bench 基准 |
| `scripts/analyze_c50_profile.mjs` | 阶段 2 阶段统计与拓扑 CSV |
| `scripts/export_to_excel.py` | 阶段 3 生成 xlsx（Stage Latency + Summary sheet） |
| `scripts/gen_timeline.py` | 阶段 3 渲染 Gantt 时序图 SVG |
| `scripts/generate_excel_charts.py` | 阶段 4 一键生成多 sheet Excel + 3 张 PNG（内部自行调用 analyze） |

### 前置条件

1. 运行环境：CubeSandbox 基准测试目标机器，Cubelet/CubeShim/CubeMaster/CubeAPI 组件在运行且日志写在 `/data/log/` 下
2. 工具依赖：`node`（analyze 脚本）、`python3` + `openpyxl` + `matplotlib`（Excel/PNG/SVG）、`bpftrace`（on-CPU profiling，可选）、`cube-bench` 二进制
3. 环境变量：
   - **必填**：`BASE_DIR`（CubeSandbox examples 目录）、`TEMPLATE_ID`（沙箱模板 ID）、`API_KEY`（CubeSandbox API 密钥）、`BENCH`（cube-bench 二进制路径）、`SHIM_BIN`（containerd-shim-cube-rs 路径）、`TASK_DIR`（containerd v2 task 根目录）
   - **可选**：`OUT_DIR`（默认 `$BASE_DIR/profile-results`）、`CASE_NAME`（默认 `profile-c50-n500`）、`LOG_DIR`（默认 `/data/log`）、`METRICS_URL`（默认 `http://127.0.0.1:9998/v1/metrics`）、`API_URL`（默认 `http://127.0.0.1:3000`）、`SHIM_PATTERN` / `VMM_PATTERN`（host 采样进程匹配模式，tools/sample_c50_host.sh）、`MAX_ATTEMPTS`（RETRY_UNTIL_SUCCESS=1 模式的最大重试次数，默认 5）、`RETRY_DEADLINE_SECONDS`（重试总时间预算，默认 7200；超限即 STARTUP_ABORT 中止并标记 FAILED）
   - 脚本不内置任何用户名路径、内部部署路径或占位凭证，未设置必填变量时会立即报错退出

### 移植适配点

本 skill 绑定 CubeSandbox 业务场景。迁移到新环境时按序检查：
1. 上面环境变量是否匹配目标部署（日志/二进制/端口/模板 ID）
2. `analyze_c50_profile.mjs` 的 `definitions` 阶段拓扑与日志字段名（Callee/CalleeAction/CostTime/InstanceId）——组件日志格式变化时需同步修改
3. `gen_timeline.py` 的 `LAYOUT`（阶段 ID + 估计偏移）——阶段增删时同步修改
4. `run_c50_profile.sh` 中 bpftrace 的进程名列表（cubelet/cubemaster/network-agent/vcpu*/cloud-hypervis/cube-vmm）

### 步骤 1：确认环境

1. 检查 profile_dir 是否存在 `evidence/` 和 `startup-latency/` 子目录
2. 若不存在 → 需要先运行 `bash $SKILL_DIR/scripts/run_c50_profile.sh` 采集数据（需在基准测试目标机器上执行）
3. 检查 evidence/ 下是否有 `*-delta.log` 文件

### 步骤 2：运行阶段统计

```bash
node $SKILL_DIR/scripts/analyze_c50_profile.mjs <profile_dir> \
  --csv-out <profile_dir>/stage-latency.csv \
  --details-out <profile_dir>/create-container-details.jsonl \
  > <profile_dir>/analysis.json
```

输出：analysis.json（stdout 重定向）+ stage-latency.csv

### 步骤 3：生成 Excel

```bash
python3 $SKILL_DIR/scripts/export_to_excel.py <profile_dir>/analysis.json <profile_dir>/stage-latency.csv <out_dir>/profile-results.xlsx
```

### 步骤 4：渲染时序图 SVG

```bash
python3 $SKILL_DIR/scripts/gen_timeline.py <out_dir>/profile-results.xlsx
```

输出：`timeline.svg`（生成在 gen_timeline.py 所在目录）

### 步骤 5：生成完整报表（可选，替代步骤 2-4）

```bash
python3 $SKILL_DIR/scripts/generate_excel_charts.py <profile_dir> <out_dir>
```

输出：`timing_report_*.xlsx` + 3 张 PNG 图表

## 关键注意事项

1. **不可加性**：大多数阶段不可直接相加——父子阶段存在包含关系，同调用对端（same-call）是两端各自计时不重叠。只有 `sequential_child` 类型（如 sandbox-start + sandbox-probe）才可顺序相加。
2. **跨进程计时不可相减**：CubeMaster→Cubelet 是跨进程调用，两端时钟不同源，不能逐样本相减得到"传输耗时"。
3. **部分观测阶段**：`network`/`storage`/`create-sandbox-metadata` 只记录超过日志阈值的样本，count 不足时不能代表全部请求。
4. **日志轮转风险**：若测试期间日志发生轮转（inode 变化），delta 切割会回退为全量复制，可能混入其他时段的日志，需检查 `log-rotation-warnings.txt`。
5. **warmup 样本差异**：API 侧仅含正式请求（500个），CubeMaster 侧含 warmup（503个），样本数不同需注意。
