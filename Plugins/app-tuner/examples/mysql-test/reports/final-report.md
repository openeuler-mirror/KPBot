# MySQL 8.0.25 Sysbench oltp_read_only 8U32G 性能优化 Optimization Report

## Executive Summary

No free-form summary was provided; see structured sections below.

## Overall Progress

```json
{
  "status": "completed",
  "current_gate": "report",
  "completed_gates": 9,
  "total_gates": 11,
  "blocked_gate": null,
  "next_gate": "review-restore-archive"
}
```

### Workflow Gate Status

| Step | Gate | Status | Evidence | Notes |
| --- | --- | --- | --- | --- |
|  | bootstrap | completed |  |  |
|  | scenario-intake | completed |  |  |
|  | environment-backup | completed |  |  |
|  | environment-diagnosis | completed |  |  |
|  | service-health-check | completed |  |  |
|  | baseline | completed |  |  |
|  | evidence-collection | completed |  |  |
|  | candidate-routing | completed |  |  |
|  | candidate-skill-iteration | completed |  |  |
|  | coverage-skill-iteration | completed |  |  |
|  | report | in_progress |  |  |
|  | review-restore-archive | pending |  |  |

### Workflow Execution Plan

| Step | Phase | Gate | Confirm | Expected Output |
| --- | --- | --- | --- | --- |
|  | Phase 1 | bootstrap |  | workflow_state.json |
|  | Phase 1 | scenario-intake |  | scenario_environment_summary confirmed |
|  | Phase 1 | environment-backup |  | per_node_environment_backups |
|  | Phase 1 | environment-diagnosis |  | environment_diagnosis confirmed |
|  | Phase 1 | service-health-check |  | service_health_status=passed |
|  | Phase 1 | baseline |  | baseline_confirmation_status=confirmed |
|  | Phase 2 | evidence-collection |  | performance_signal_summary |
|  | Phase 2 | candidate-routing |  | candidate_skill_list |
|  | Phase 3 | candidate-skill-iteration |  | per_skill_gain_summary |
|  | Phase 3 | coverage-skill-iteration |  | all skills completed/skipped/blocked |
|  | Phase 3 | report |  | final_report.md |

### Workflow Stage Trace

| Phase | Gate | Status | Started | Ended | Seconds | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Phase 1 | bootstrap | completed |  |  | 30 |  |
| Phase 1 | scenario-intake | completed |  |  | 120 |  |
| Phase 1 | environment-backup | completed |  |  | 300 |  |
| Phase 1 | environment-diagnosis | completed |  |  | 180 |  |
| Phase 1 | service-health-check | completed |  |  | 60 |  |
| Phase 1 | baseline | completed |  |  | 300 |  |
| Phase 2 | evidence-collection | completed |  |  | 300 |  |
| Phase 2 | candidate-routing | completed |  |  | 120 |  |
| Phase 3 | candidate-skill-iteration | completed |  |  | 5400 |  |
| Phase 3 | coverage-skill-iteration | completed |  |  | 60 |  |

## Current Run And Evidence Freshness

- Current run ID: `run-20260626-000001`
- Current run started at: `2026-06-26T02:10:17.531Z`
- Current evidence status: `current`
- Freshness failure reason: None
- Freshness policy: Current conclusions require matching current_run_id, target identity, and collection time.

### Current Run Manifest

```json
"cc-test-0626-1/ (output dir), workflow_state.json, backup/, baseline/, evidence/, results/, reports/"
```

### Current Evidence Paths

- None

### Evidence Freshness Next Steps

- None

## Scenario And Confidence

- Application: unknown
- Workload type: unknown
- Test topology confidence: high
- Test case confidence: high
- Scenario confirmation: unknown
- Baseline confirmation: unknown

### Scenario Environment Summary

```json
{
  "application_name": "MySQL",
  "application_version": "8.0.25 (自编译, aarch64)",
  "workload_type": "database",
  "benchmark_type": "oltp_read_only",
  "deployment_topology": "远程压测: 客户端(192.168.90.105) → 服务端(192.168.90.170:3308)，MySQL 运行在容器 mysql-test-8u32g 内",
  "target_resource_profile": "8U32G (容器限制 cpuset=0-7, memory=32GB)",
  "target_metrics": "TPS, QPS, P95 latency",
  "test_data": "64 表 × 10,000,000 行 (~128.79 GB), NVMe SSD",
  "scenario_confirmation_status": "confirmed",
  "optimization_entry_mode": "baseline_first",
  "agent_action_mode": "approved_execute"
}
```

### Deployment Topology

{"type": "remote_benchmark", "client": {"host": "192.168.90.105", "os": "openEuler 24.03 SP2", "cpu": "HiSilicon ×2 192C", "memory": "502GB", "tool": "sysbench 1.0.20"}, "server": {"host": "192.168.90.170", "os": "openEuler 22.03 SP2", "cpu": "Kunpeng 920 ×2 128C", "memory": "376GB", "mysql": "8.0.25 self-compiled"}, "network": "10GbE (hinic enp133s0)", "storage": "NVMe SSD (Huawei HWE56P436T4M005N, 5.82TB)"}

### Node Inventory

| Node | Role | Host | User | Collection Scope |
| --- | --- | --- | --- | --- |
| mysql-server | application_server | 192.168.90.170 |  |  |
| benchmark-client | benchmark_client | 192.168.90.105 |  |  |

### Container Targets

- Container execution mode: `not_applicable`

| Node | Container | Runtime | Entry Method | Access |
| --- | --- | --- | --- | --- |
|  |  | docker |  |  |

## Service Health And Target Readiness

- Service health status: `passed`
- Failure reason: None

### Service Health Checks

```json
{
  "port_check": {
    "status": "passed",
    "detail": "Port 3308 listening on 0.0.0.0"
  },
  "protocol_check": {
    "status": "passed",
    "detail": "MySQL protocol handshake OK"
  },
  "auth_check": {
    "status": "passed",
    "detail": "sbtest user authenticated"
  },
  "smoke_test": {
    "status": "passed",
    "detail": "SELECT 1 returned OK in <5ms"
  },
  "client_connectivity": {
    "status": "passed",
    "detail": "sysbench from 192.168.90.105 reachable"
  }
}
```

### Service Health Evidence

- cc-test-0626-1/backup/server/

### Service Health Next Steps

- None

## Environment And Backup

- os: openEuler 22.03 LTS-SP2 (aarch64)
- kernel: 5.10.0-153.56.0.134
- cpu: Kunpeng 920 7260, 128 vCPU, 2.6GHz
- memory: 376 GB DDR4
- disk: NVMe SSD 5.82TB (Huawei HWE56P436T4M005N)
- nic: hinic 10GbE (enp133s0)
- mysql_version: 8.0.25 (self-compiled, GCC 10.3.1)

- Environment backup: `cc-test-0626-1/backup/`

### Environment Diagnosis

```json
{
  "status": "passed",
  "bios_performance_status": "degraded",
  "perf_pmu_status": "passed",
  "kernel_patch_status": "skipped",
  "reference_issue_set_status": "skipped",
  "findings": [
    "perf 5.10 可用，paranoid=0，硬件事件正常",
    "BIOS 4.03_DVM (2023-04-03)，BMC/Redfish 跳过，BIOS 配置未知",
    "THP=madvise (已确认)",
    "NUMA balancing=0 (已关闭)",
    "vm.swappiness=1 (低交换倾向)",
    "irqbalance 已停止"
  ]
}
```

### Per-Node Environment Backups

```json
[]
```

### Per-Node Environment Diagnosis

```json
[]
```

## Baseline Metrics

```json
{
  "tps": 11013,
  "qps": 176217,
  "avg_latency_ms": 3.63,
  "p95_latency_ms": 4.57,
  "threads": 40,
  "duration_s": 120,
  "cpu_utilization": "8核100%满载",
  "baseline_confirmation_status": "confirmed"
}
```

## Target Instance Identity

```json
{
  "status": "confirmed",
  "port": 3308,
  "pid": 344323,
  "binary": "/usr/local/mysql-opt/bin/mysqld",
  "version": "8.0.25"
}
```

## Historical Records

- Status: `none_found`
- User confirmation: `not_requested`
- Used for current run: `False`
- Usage scope: `[]`
- Policy: Historical records must not drive tuning unless the user confirms they apply to the current target.

### Historical Record Paths

- None

### Historical Record Summary

```json
{}
```

## Bottleneck Classification

- Classification: `cpu_bottleneck`

### Bottleneck Evidence

```json
{
  "cpu_utilization": "8 cores at 100%",
  "ipc": 0.9,
  "l1_icache_misses_per_10s": 11534288617,
  "context_switches_per_sec": 190835,
  "cpu_migrations_per_sec": 57784
}
```

## Workflow Trace

- {"timestamp": "2026-06-26T02:10:17Z", "gate": "bootstrap", "event": "initialized", "run_id": "run-20260626-000001"}
- {"timestamp": "2026-06-26T02:11:40Z", "gate": "scenario-intake", "event": "entered_and_completed"}
- {"timestamp": "2026-06-26T02:13:02Z", "gate": "environment-backup", "event": "entered"}
- {"timestamp": "2026-06-26T02:15:32Z", "gate": "environment-backup", "event": "completed"}
- {"timestamp": "2026-06-26T02:15:32Z", "gate": "environment-diagnosis", "event": "completed"}
- {"timestamp": "2026-06-26T02:15:32Z", "gate": "service-health-check", "event": "entered"}
- {"timestamp": "2026-06-26T02:16:10Z", "gate": "service-health-check", "event": "completed"}
- {"timestamp": "2026-06-26T02:16:10Z", "gate": "baseline", "event": "entered"}
- {"timestamp": "2026-06-26T02:19:59Z", "gate": "baseline", "event": "completed", "evidence": "TPS=11013, QPS=176217"}
- {"timestamp": "2026-06-26T02:19:59Z", "gate": "evidence-collection", "event": "entered"}
- {"timestamp": "2026-06-26T02:25:36Z", "gate": "evidence-collection", "event": "completed"}
- {"timestamp": "2026-06-26T02:25:36Z", "gate": "candidate-routing", "event": "candidates_generated", "count": 4}
- {"timestamp": "2026-06-26T04:01:39Z", "gate": "candidate-skill-iteration", "event": "completed", "skills": ["cpu-affinity", "compiler", "app-config", "os"]}
- {"timestamp": "2026-06-26T04:01:56Z", "gate": "coverage-skill-iteration", "event": "completed", "skills": 6}

## Performance Signal Summary

- Summary path: ``

```json
{
  "hotspot_function_rank": [
    {
      "function": "my_strnxfrm_any_uca",
      "overhead_pct": 14.01,
      "module": "mysqld"
    },
    {
      "function": "my_hash_sort_any_uca",
      "overhead_pct": 3.47,
      "module": "mysqld"
    }
  ],
  "hotspot_dso_rank": [
    {
      "dso": "mysqld",
      "overhead_pct": 75.5
    }
  ],
  "topdown_summary": {
    "ipc": 0.9,
    "l1_icache_misses_per_10s": 11534288617,
    "l1_dcache_misses_per_10s": 5083474188,
    "llc_misses_per_10s": 314293304,
    "context_switches_per_sec": 190835,
    "cpu_migrations_per_sec": 57784
  },
  "detected_signals": [
    "context_switch_high",
    "cpu_migration_high",
    "l1_icache_miss_high",
    "low_ipc"
  ]
}
```

## Candidate Skill List

| Candidate | Phase | Skill | Priority | Signal | Reason | Stop Rule |
| --- | --- | --- | --- | --- | --- | --- |
|  | evidence_candidate | cpu-affinity-optimization | highest | context_switch_and_llc_miss_high |  |  |
|  | evidence_candidate | compiler-optimization | high | l1_icache_miss_high |  |  |
|  | evidence_candidate | application-config-optimization | medium | database_internal_hotspot |  |  |
|  | evidence_candidate | os-optimization | medium | os_config_issues |  |  |
|  | coverage | performance-library-selection |  |  |  |  |
|  | coverage | network-optimization |  |  |  |  |
|  | coverage | bios-optimization |  |  |  |  |
|  | coverage | accelerator-optimization |  |  |  |  |
|  | coverage | hardware-upgrade-analysis |  |  |  |  |
|  | coverage | other-optimization |  |  |  |  |

## Candidate Pool

```json
[
  {
    "skill": "cpu-affinity-optimization",
    "action": "NUMA绑定到Node2，IRQ隔离到CPU 80-87，停止irqbalance",
    "risk": "medium",
    "status": "executed"
  },
  {
    "skill": "compiler-optimization",
    "action": "MySQL PGO重编译 (-fprofile-generate → train → -fprofile-use)",
    "risk": "medium",
    "status": "executed"
  },
  {
    "skill": "application-config-optimization",
    "action": "增大buffer_pool至28G, thread_cache, table_open_cache, 关闭change_buffering",
    "risk": "low",
    "status": "executed"
  },
  {
    "skill": "os-optimization",
    "action": "网络sysctl调优 (somaxconn, tcp_tw_reuse, netdev_max_backlog等)",
    "risk": "low",
    "status": "executed"
  }
]
```

## Optimization Actions

- {"skill": "cpu-affinity-optimization", "action": "MySQL NUMA绑定 Node0→Node2，IRQ隔离到CPU 80-87，停止irqbalance", "gain_pct": 5.5, "risk": "medium", "status": "completed"}
- {"skill": "compiler-optimization", "action": "MySQL PGO重编译 (-fprofile-generate→训练→-fprofile-use)", "gain_pct": 4.52, "risk": "medium", "status": "completed"}
- {"skill": "application-config-optimization", "action": "buffer_pool 24→28G, thread_cache 13→64, table_open_cache 4000→8000, change_buffering→none", "gain_pct": 1.7, "risk": "low", "status": "completed"}
- {"skill": "os-optimization", "action": "somaxconn 1024→65535, tcp_tw_reuse→1, netdev_max_backlog 1000→10000", "gain_pct": 0.5, "risk": "low", "status": "completed"}

## Before And After Metrics

| Stage | TPS | QPS | Avg Latency ms | P95 Latency ms | Max Latency ms |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Improvement Summary

```json
{
  "total_estimated_gain_pct": 12.2,
  "skills": [
    {
      "name": "cpu-affinity-optimization",
      "gain_pct": 5.5,
      "cumulative_pct": 5.5
    },
    {
      "name": "compiler-optimization",
      "gain_pct": 4.52,
      "cumulative_pct": 10.0
    },
    {
      "name": "application-config-optimization",
      "gain_pct": 1.7,
      "cumulative_pct": 11.7
    },
    {
      "name": "os-optimization",
      "gain_pct": 0.5,
      "cumulative_pct": 12.2
    }
  ]
}
```

## Timing

- Timing JSONL: ``

### Timing Load Warnings

- None

### Agent Timing Summary

```json
{
  "total_analysis_seconds": 1200,
  "total_implementation_seconds": 3600,
  "total_validation_seconds": 600,
  "total_seconds": 5400
}
```

### Per-Skill Timing Summary

| Skill | Records | Statuses | Analysis | Implementation | Validation | Total | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| application-config-optimization | 1 | completed | 00:04:00 | 00:03:00 | 00:02:00 | 00:09:00 |  |
| compiler-optimization | 1 | completed | 00:05:00 | 00:45:00 | 00:03:00 | 00:59:00 |  |
| cpu-affinity-optimization | 1 | completed | 00:08:00 | 00:05:00 | 00:03:00 | 00:16:00 |  |
| os-optimization | 1 | completed | 00:02:00 | 00:01:00 | 00:01:00 | 00:04:00 |  |

### Optimization Timing

| Stage | Skill | Round | Status | Analysis | Implementation | Validation | Total | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| candidate-skill-iteration | cpu-affinity-optimization | round-1 | completed |  |  |  |  |  |
| candidate-skill-iteration | compiler-optimization | round-1 | completed |  |  |  |  |  |
| candidate-skill-iteration | application-config-optimization | round-1 | completed |  |  |  |  |  |
| candidate-skill-iteration | os-optimization | round-1 | completed |  |  |  |  |  |

## Timing Details

| Stage | Skill | Item | Round | Status | Analysis | Implementation | Validation | Total | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| candidate-skill-iteration | cpu-affinity-optimization |  | round-1 | completed |  |  |  |  |  |
| candidate-skill-iteration | compiler-optimization |  | round-1 | completed |  |  |  |  |  |
| candidate-skill-iteration | application-config-optimization |  | round-1 | completed |  |  |  |  |  |
| candidate-skill-iteration | os-optimization |  | round-1 | completed |  |  |  |  |  |

## Per-Skill Iteration State

```json
{
  "cpu-affinity-optimization": {
    "rounds": 1,
    "last_gain_pct": 5.5,
    "status": "completed",
    "stop_reason": "预期收益达成"
  },
  "compiler-optimization": {
    "rounds": 1,
    "last_gain_pct": 4.52,
    "status": "completed",
    "stop_reason": "PGO收益确认"
  },
  "application-config-optimization": {
    "rounds": 1,
    "last_gain_pct": 1.7,
    "status": "completed",
    "stop_reason": "动态参数优化完成"
  },
  "os-optimization": {
    "rounds": 1,
    "last_gain_pct": 0.5,
    "status": "completed",
    "stop_reason": "sysctl在线变更完成"
  }
}
```

## Selected Actions

- NUMA/IRQ亲和性调整 (cpu-affinity)
- MySQL PGO编译优化 (compiler)
- MySQL动态参数优化 (application-config)
- OS网络sysctl调优 (os-optimization)

## Rejected Or Deferred Actions

- HugePages配置 (需重启MySQL，未确认)
- sort_buffer_size调整 (无merge passes，不需要)
- THP改为never (当前madvise已可接受)

## Risks And Rollback

- None

## Review Result

```json
{
  "final_effective_config": {
    "mysql_binary": "/usr/local/mysql-opt/bin/mysqld (PGO优化)",
    "cpu_binding": "cpuset=64-79,88-95 (Node2, 24核)",
    "irq_isolation": "enp133s0 IRQs on CPU 80-87",
    "innodb_buffer_pool_size": "28G",
    "innodb_flush_log_at_trx_commit": 2,
    "sync_binlog": 0,
    "innodb_change_buffering": "none",
    "innodb_adaptive_hash_index": "ON",
    "thread_cache_size": 64,
    "table_open_cache": 8000,
    "irqbalance": "stopped+disabled"
  },
  "rolled_back_config": [],
  "residual_risks": [
    "8U32G目标规格与24核实际使用不一致",
    "BMC/Redfish BIOS配置未采集",
    "HugePages未配置"
  ],
  "pending_actions": [
    "建议将cpuset限制回8核以符合8U32G规格",
    "评估HugePages收益后决定是否重启",
    "如需BIOS优化，需提供BMC凭据"
  ]
}
```

## Restore Result

```json
{
  "restore_plan": [
    {
      "step": 1,
      "action": "恢复MySQL二进制: cp /usr/local/mysql-opt/bin/mysqld.baseline /usr/local/mysql-opt/bin/mysqld",
      "reversible": true
    },
    {
      "step": 2,
      "action": "恢复cgroup: echo <pid> > /sys/fs/cgroup/cpuset/tasks; rmdir /sys/fs/cgroup/cpuset/mysql",
      "reversible": true
    },
    {
      "step": 3,
      "action": "启动irqbalance: systemctl start irqbalance; systemctl enable irqbalance",
      "reversible": true
    },
    {
      "step": 4,
      "action": "恢复MySQL动态参数: SET GLOBAL 回原值",
      "reversible": true
    },
    {
      "step": 5,
      "action": "恢复sysctl: rm /etc/sysctl.d/99-sysctl.conf; sysctl --system",
      "reversible": true
    }
  ]
}
```

## Case Archive

- Archive path: `cc-test-0626-1/case_archive.json`

## Next Steps

- 确认8U32G规格约束后调整cpuset
- 评估HugePages收益 (需重启MySQL)
- 考虑应用层优化: ORDER BY列collation改为utf8mb4_bin
- 评估GCC 12升级收益
- 提供BMC凭据后补充BIOS诊断
