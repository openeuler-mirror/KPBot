# 环境诊断规则 / Environment Diagnosis

环境诊断必须在 `backup_environment.sh` 完成后、服务健康检查前执行。它用于判断当前环境是否已经满足优化前提，而不是实施任何变更。诊断结果必须先给用户确认，用户确认前不得进入服务健康检查、目标实例身份确认、正式基线或基线性能确认。

多节点场景下，环境诊断不是“只诊断服务端”。客户端、服务端以及链路中的关键节点都必须按相同诊断契约产出节点级结果，并汇总为全链路诊断结论。

## 执行位置

顺序必须是：

1. `environment_backup_created`
2. `environment_diagnosis_completed`
3. `environment_diagnosis_confirmation`
4. `service_health_check`
5. `baseline_run`

若环境诊断发现阻塞项，应设置：

- `overall_progress.status=blocked`
- `blocked_gate=environment_diagnosis`
- `environment_diagnosis.status=failed` 或 `blocked`

若证据不足但不影响继续做服务健康检查，应设置：

- `environment_diagnosis.status=degraded`
- `degraded_capabilities` 记录缺失证据

即使诊断状态为 `passed` 或 `degraded`，也必须先展示给用户并等待确认：

- 用户确认继续：`environment_diagnosis_confirmation_status=confirmed`
- 用户要求补采、修复或重新诊断：`environment_diagnosis_confirmation_status=rebuild_required`
- 用户拒绝当前诊断结果：`environment_diagnosis_confirmation_status=rejected`
- 用户尚未确认：`environment_diagnosis_confirmation_status=pending`

`environment_diagnosis_confirmation_status` 不是 `confirmed` 时，`service_health_status` 和 `baseline_confirmation_status` 必须保持 `not_entered` / `blocked` / `pending`，不得执行正式基线性能确认。

## 诊断项

### 0. 多节点与容器诊断范围

如果 `node_inventory` 中存在多个节点，必须为每个节点输出 `per_node_environment_diagnosis[]`：

- `node_id`、`role`、`host`、`collection_scope`。
- `backup_dir`、`diagnosis.status`、`blocked_items`、`degraded_items`、`evidence_paths`。
- 客户端节点必须检查压测工具版本、CPU/内存/网卡余量、网络链路和客户端瓶颈风险。
- 服务端节点必须检查目标服务、容器/虚拟化、应用配置、PMU/perf、NUMA、磁盘和网卡/IRQ。
- 任一关键节点诊断失败时，全局 `environment_diagnosis.status` 必须为 `blocked` 或 `failed`；任一关键节点降级时，全局状态最高只能为 `degraded`。

容器场景必须分成宿主机视角和容器内视角：

- 宿主机视角：硬件、内核、PMU/perf、IRQ、网卡、块设备、cgroup 和容器运行时。
- 容器内视角：应用进程、应用配置、路径、运行用户、用户态依赖、数据库状态和容器内资源限制。
- 如果容器可进入但 Agent 未进入容器采集应用侧证据，诊断必须标记为 `degraded`。
- 如果容器不可进入，必须记录 `container_access_status=blocked|degraded`、失败命令和用户可执行修复建议。

### 1. 历史 Reference 问题集回归检查

如果存在历史 reference 问题集，则必须检查问题集中提到的参数或配置是否已经正常。

输入字段：

- `reference_issue_set_path`
- `reference_issue_set_status`
- `reference_issue_checks`

默认行为：

- 未提供问题集或路径不存在：`reference_issue_set_status=not_present`，该项 `skipped`，不得报错。
- 问题集存在但格式不可解析：`reference_issue_set_status=invalid`，该项 `degraded`，报告解析失败原因。
- 问题集存在且可解析：逐项检查，并输出 `pass` / `fail` / `unknown`。

推荐 JSON 格式：

```json
{
  "schema_version": "1.0",
  "issues": [
    {
      "id": "kernel.numa_balancing",
      "description": "NUMA 自动迁移会破坏绑核/绑内存约束",
      "check": {
        "type": "file_equals",
        "path": "/proc/sys/kernel/numa_balancing",
        "expected": "0"
      },
      "severity": "medium"
    }
  ]
}
```

支持的只读检查类型：

- `file_equals`
- `file_contains`
- `sysctl_equals`
- `command_contains`
- `backup_file_contains`
- `backup_file_not_contains`

危险检查、修改命令、重启命令不得出现在 reference 问题集中。

### 2. BIOS 是否高性能配置

必须检查 BIOS/固件是否处于高性能取向。

环境备份前必须先询问用户是否采集 BIOS 配置。用户选择采集时，需要提供 BMC/IPMI/Redfish 地址、账号和密码/token 传递方式；用户不采集或不提供凭据时继续 OS 侧备份，但必须在诊断中记录 Redfish 缺失原因。

优先证据：

- BMC/Redfish 只读导出的 BIOS 属性。
- `dmidecode`、DMI/sysfs、厂商工具的只读输出。
- 人工提供的 BIOS Setup 截图或摘录。

推荐关注项：

- Power Profile / Workload Profile 是否为 `Performance`、`Maximum Performance`、`HPC` 或厂商等价项。
- CPU C-State / Package C-State 是否关闭或按性能场景设置。
- CPU 频率调节策略是否为性能优先。
- NUMA / Node Interleaving 是否符合 workload 目标。
- 内存频率、通道和 RAS 选项是否存在明显降频或节能配置。

输出字段：

- `bios_performance_status`: `passed` / `failed` / `degraded` / `unknown`
- `bios_performance_evidence`
- `bios_performance_findings`
- `bios_performance_next_steps`

如果 OS 侧证据只能看到 BIOS 版本而看不到 BIOS 设置，必须输出 `degraded`，列出需要补充的 BMC/Redfish 或人工 BIOS 截图，不得臆测“已高性能”。

### 3. Perf/PMU 采集能力是否可用

必须检查当前环境是否具备性能采集能力，尤其是 perf 和 PMU 事件。实际登录环境可能是宿主机、虚拟机、容器或非 root 用户；这些环境会影响 `perf`、PMU 硬件事件、内核符号、系统命令和容器内观测范围。

最低检查：

- `perf` 命令是否存在。
- 当前用户是否为 root，或是否具备 perf 所需 capability。
- `/proc/sys/kernel/perf_event_paranoid` 是否允许当前用户采集所需事件。
- `/proc/sys/kernel/kptr_restrict` 是否影响内核符号解析。
- 当前环境类型：baremetal / VM / container / unknown。
- 容器场景是否存在可见的 cgroup、capability、host pid/perf_event 映射线索。
- 虚拟机场景是否可能暴露硬件 PMU；若 PMU 未映射，应标记降级。
- `perf list` 是否能看到硬件事件，例如 `cycles`、`instructions`、`cache-misses`。
- 最小 `perf stat` smoke test 是否能运行；失败时记录错误输出。

输出字段：

- `perf_pmu_status`: `passed` / `failed` / `degraded` / `unknown`
- `perf_command_status`
- `perf_permission_status`
- `perf_event_paranoid`
- `kptr_restrict`
- `runtime_environment`
- `pmu_event_status`
- `perf_smoke_test_status`
- `perf_pmu_findings`
- `perf_pmu_next_steps`

判定规则：

- `perf` 不存在：`perf_pmu_status=failed`，后续 perf/topdown/flamegraph 能力阻断。
- 非 root 且 `perf_event_paranoid` 过高导致 smoke test 失败：`failed` 或 `degraded`，需提示降低 paranoid、切换 root 或授予 capability。
- 容器内未映射 perf/PMU/capability：`degraded` 或 `failed`，需提示在宿主机采集，或以 `--privileged`、`CAP_PERFMON`/`CAP_SYS_ADMIN`、host pid/perf_event 映射等方式运行。
- 虚拟机未暴露 PMU：`degraded`，需提示开启虚拟化 PMU/perf event passthrough 或在宿主机采集。
- 只能采软件事件、不能采硬件 PMU：`degraded`，后续 topdown、cache miss、cycles 等结论置信度降低。

### 4. 内核补丁是否齐全

必须检查目标场景要求的内核补丁是否齐全。

输入字段：

- `kernel_patch_manifest_path`
- `kernel_patch_status`
- `kernel_patch_checks`

默认行为：

- 未提供补丁清单：`kernel_patch_status=not_applicable_or_unknown`，该项 `skipped`，并提示需要补丁清单才能判断齐全性。
- 清单存在但检查失败：输出缺失补丁、失败证据和影响范围。
- 清单存在且全部通过：`kernel_patch_status=passed`。

推荐 JSON 格式：

```json
{
  "schema_version": "1.0",
  "patches": [
    {
      "id": "vendor-net-stack-fix",
      "description": "网络栈性能修复补丁",
      "checks": [
        {
          "type": "uname_contains",
          "expected": "153.56.0.134"
        },
        {
          "type": "kernel_config_enabled",
          "name": "CONFIG_ARM64_LSE_ATOMICS"
        }
      ],
      "severity": "high"
    }
  ]
}
```

支持的只读检查类型：

- `uname_contains`
- `os_release_contains`
- `kernel_config_enabled`
- `kernel_config_equals`
- `file_exists`
- `file_contains`
- `command_contains`
- `backup_file_contains`

不得把“内核版本较新”直接等同于“补丁齐全”；必须有清单或厂商证据支撑。

## 输出要求

环境诊断输出必须写入 `environment_diagnosis`，至少包含：

```json
{
  "status": "passed|failed|blocked|degraded",
  "reference_issue_set_status": "not_present|passed|failed|degraded|invalid",
  "bios_performance_status": "passed|failed|degraded|unknown",
  "perf_pmu_status": "passed|failed|degraded|unknown",
  "kernel_patch_status": "passed|failed|skipped|not_applicable_or_unknown",
  "findings": [],
  "blocked_items": [],
  "degraded_items": [],
  "evidence_paths": [],
  "next_steps": []
}
```

报告和案例归档必须保留该诊断结果。若诊断失败或降级，后续报告必须说明风险和最小补充证据。
