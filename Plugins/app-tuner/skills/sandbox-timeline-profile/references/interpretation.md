# CubeSandbox 阶段与证据判读约定

## 日志和关联

采集器在 workload 前后记录 size/inode。inode 不变且文件增长时，`tail -c +$((before_size + 1))` 得到精确增量；inode 改变或 size 缩小时复制当前完整文件并记录轮转 warning，这种输入可能混入其他时段，不能当作严格 delta。

分析输入包括：

- `startup-latency/*.json` 与 `*.summary.json`：API 客户端请求与总体统计；
- `cubelet_stat-delta.log`：按 `Action=Create`、根 `Callee=cubebox-service` 和 `InstanceId` 关联，再按 `Callee/CostTime` 分组；
- `shim_stat-delta.log`：按 `CalleeAction/CostTime` 统计同一实例集合；
- `master-delta.log`：从 `CreateSandbox_rsp:` 的 `ext_info` 读取 CubeMaster 阶段；
- `shim_req-delta.log`：解析 CreateContainer 嵌入式时间戳、累计计时和 Guest 事件。

非空坏 JSONL 是输入错误，不再静默丢弃。统计量为 count/avg/min/p50/p95/max；分批图每 50 个请求一批。

## 本轮证据、新鲜度与敏感信息

外部 Workflow 的 evidence 与 benchmark 必须属于同一轮：采集前不得存在旧 `startup-latency`，workload 退出前必须新建唯一且同 case 的 `<case>.summary.json` / `<case>.json` 配对。缺少配对时只能保留证据并要求调用方补齐流程，不能读取上一轮 benchmark 继续分析。旧式离线输入没有 manifest 仍可分析；新 manifest 若存在，则只有 `status: success` 能进入分析。

日志 offset 的 before/after 文件采用 TSV，以完整保留含空格路径。process 快照不采集 `args`，只保留 `comm` 与调度、CPU/内存等性能字段，避免把其他主机进程的 token/password 带入证据包。`--no-bpftrace` 轮次开始前移除旧 `bpftrace-profile.txt`，历史 migration 样本不得进入本轮 manifest 或统计。

metrics-before/after 失败都记退出码并将 manifest 标为 failed；before 失败不得阻止 workload。发布分析、SVG 和报告前还必须排除所有输入/输出真实路径重合，防止覆盖输入或用 manifest 冒充图表。

## CreateContainer 子阶段

分析器识别 `create req start`、`start vm start`、`agent is ready`、`create req finish`、`load spec finish at:N`、`start sandbox finish at:N`、`start container finish at:N` 以及 Guest create/restore 日志。

主要派生量：

- `create_request_total = create_req_finish - create_req_start`；
- `vm_start_to_agent_ready = agent_ready - start_vm`；
- `sandbox_to_container_finish = container_finish_cumulative - sandbox_finish_cumulative`；
- `guest_receive_to_restore_branch = guest_restore_branch - guest_create_receive`；
- `restore_path_unresolved = shim.CreateContainer - guest_receive_to_restore_branch`。

最后一项是诊断残差，包含请求准备、RPC、start_exec_process 等未拆分部分，不能命名为某一个函数的直接耗时。转发 Guest stdout/stderr 的外层时间戳可能异步或乱序，不用来跨域相减。

## 阶段拓扑

```text
api.create
└── master.cube-e2e
    └── cubelet.cubebox-service
        ├── cubelet.cubebox-service-inner (派生残差)
        └── cubelet.cubebox
            ├── cubelet.network / storage / create-sandbox-metadata (部分观测)
            ├── cubelet.sandbox-start
            │   └── cubelet.sandbox-create
            │       └── shim.CreatePodSandbox
            │           ├── shim.LaunchVmm / RestoreVm / ResetVm / CreateSandbox
            │           ├── shim.CreateContainer
            │           │   ├── guest.receive-to-restore-branch
            │           │   └── derived.restore-path-unresolved
            │           ├── shim.create-request-total (边界标记)
            │           └── shim.vm-start-to-agent-ready (重叠窗口)
            └── cubelet.sandbox-probe
                └── master.sandbox-probe (传播副本)
    └── master.all-probe (聚合探针)
```

`stage-latency.csv` 的 `containment_relation`、`sequence_group/index`、`timing_kind`、`additivity`、`source_field` 是解释依据。父子包含、same-call 对端、parallel、cumulative、derived residual、aggregate 和 propagated copy 默认不可加；不能为了得到总耗时把 CSV 的 avg 列求和。

## 时钟、样本与 warmup

- 不同进程/Guest 的计时来源和时钟可能不同，不做逐样本跨进程相减。
- network/storage/create-sandbox-metadata 只记录超过阈值的样本，count 小于请求数属于部分观测，不是完整分布。
- API 侧通常仅含正式请求，CubeMaster 可含 warmup；样本数不同应先检查口径。
- SVG 用父子窗口等比布局保证视觉包含关系，条内标签才是真实 avg；估计 offset 只用于相对布局，不是跨组件实测时间戳。

## 报告边界

完整报告直接消费 `analysis.json`，不重新运行 analyzer。Excel 提供 Bench总体、分批、Cubelet、Shim、详细子阶段、CubeMaster 和 Host sheet；PNG 分别是阶段 avg/P50/P95、分批延迟和 Host 资源。缺 `benchmark_blocks` 或 `host_intervals` 时必须 blocked，而不是省略对应 PNG 后宣称完整成功。
