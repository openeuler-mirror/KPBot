#!/usr/bin/env node
// analyze_rollback_profile.mjs - 回滚时序阶段统计与拓扑 CSV
//
// 用法:
//   node analyze_rollback_profile.mjs PROFILE_DIR \
//     [--details-out FILE] [--csv-out FILE]
//
// 输入: PROFILE_DIR/evidence/*-delta.log, PROFILE_DIR/rollback-latency/*.summary.json,
//       PROFILE_DIR/run.log（ROLLBACK_BEGIN/END 时间窗标记，可选）
// 输出: stdout 为 analysis.json（顶层对应第一组 case，cases 字段为各组分段统计）,
//       同时生成 stage-latency.csv 与 rollback-details.jsonl

import fs from "node:fs";
import path from "node:path";

const profileDir = process.argv[2];
if (!profileDir) {
  console.error(
    "usage: analyze_rollback_profile.mjs PROFILE_DIR [--details-out FILE] [--csv-out FILE]",
  );
  process.exit(2);
}

let detailsOut = null;
let csvOut = path.join(profileDir, "stage-latency.csv");
for (let index = 3; index < process.argv.length; index += 1) {
  if (process.argv[index] === "--details-out" && process.argv[index + 1]) {
    detailsOut = process.argv[++index];
  } else if (process.argv[index] === "--csv-out" && process.argv[index + 1]) {
    csvOut = process.argv[++index];
  } else {
    throw new Error(`unknown or incomplete argument: ${process.argv[index]}`);
  }
}

const evidenceDir = path.join(profileDir, "evidence");
const latencyDir = path.join(profileDir, "rollback-latency");
const caseSummaries = fs
  .readdirSync(latencyDir)
  .map((name) => (name.endsWith(".summary.json") ? name.slice(0, -".summary.json".length) : null))
  .filter(Boolean)
  .sort();

if (caseSummaries.length === 0) {
  throw new Error(`no case summary found under ${latencyDir}`);
}

// ── stats helpers ─────────────────────────────────────────────────────
function quantile(values, percentile) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.ceil((percentile / 100) * sorted.length) - 1;
  return sorted[Math.max(0, index)];
}

function stats(values) {
  if (values.length === 0) return null;
  const sum = values.reduce((total, value) => total + value, 0);
  return {
    count: values.length,
    avg: sum / values.length,
    min: Math.min(...values),
    p50: quantile(values, 50),
    p95: quantile(values, 95),
    max: Math.max(...values),
  };
}

function readJsonLines(file) {
  return fs
    .readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .flatMap((line) => {
      try {
        return [JSON.parse(line)];
      } catch {
        return [];
      }
    });
}

function groupStageStats(records, stageKey) {
  const groups = new Map();
  for (const record of records) {
    const stage = record[stageKey];
    const cost = Number(record.CostTime);
    if (!stage || !Number.isFinite(cost)) continue;
    if (!groups.has(stage)) groups.set(stage, []);
    groups.get(stage).push(cost);
  }
  return Object.fromEntries(
    [...groups.entries()]
      .map(([stage, values]) => [stage, stats(values)])
      .sort((left, right) => (right[1].avg ?? 0) - (left[1].avg ?? 0)),
  );
}

function parseTimestamp(value) {
  if (typeof value !== "string") return null;
  const precise = value.match(
    /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/,
  );
  if (precise) {
    const seconds = Date.parse(`${precise[1]}${precise[3]}`);
    const fractionalMs = Number(`0.${precise[2] ?? "0"}`) * 1000;
    return Number.isFinite(seconds) && Number.isFinite(fractionalMs)
      ? seconds + fractionalMs
      : null;
  }
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function parseEmbeddedLog(content) {
  if (typeof content !== "string" || !content.startsWith("{")) return null;
  try {
    return JSON.parse(content.trim());
  } catch {
    return null;
  }
}

function setFirst(target, key, value) {
  if (target[key] == null && value != null) target[key] = value;
}

function difference(end, start) {
  return Number.isFinite(end) && Number.isFinite(start) ? end - start : null;
}

function numericStats(records, key) {
  return stats(records.map((record) => record[key]).filter(Number.isFinite));
}

function csvValue(value) {
  if (value == null) return "";
  const text = typeof value === "number" && !Number.isInteger(value)
    ? value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")
    : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

// ── 回滚阶段拓扑定义 ───────────────────────────────────────────────────
// 与 sandbox-timeline-profile 的 create 拓扑对应，回滚链：
//   api.rollback → master.snapshot-rollback → cubelet.rollback-service
//     → shim.RollbackSnapshot → {DeleteVm, RestoreVm(热点), ResetVm, ConnectAgent}
//   hypervisor.restore-memory 嵌套在 RestoreVm 内。
// 字段含义与创建版本一致；source 为 result JSON 中的取值路径。
function buildStageRelationshipCsv(result) {
  const definitions = [
    {
      order: 10, depth: 0, component: "API", id: "api.rollback", name: "Rollback API latency",
      parent: "", containment: "root", group: "request", index: 1, previous: "",
      related: "", kind: "direct_outer", additivity: "outer_total",
      source: "benchmark.wall_ms",
      note: "客户端观测边界；一轮 = concurrency 个并发回滚的 batch wall time。",
    },
    {
      order: 20, depth: 1, component: "CubeMaster", id: "master.snapshot-rollback", name: "snapshot-rollback",
      parent: "api.rollback", containment: "observed_within", group: "request", index: 2,
      previous: "api.rollback", related: "", kind: "direct_outer", additivity: "do_not_sum",
      source: "cubemaster_stages.snapshot-rollback",
      note: "SNAPSHOT_ROLLBACK Job 主计时（PREPARING→READY）；跨进程，不与 API 边界逐样本相减。",
    },
    {
      order: 25, depth: 2, component: "CubeMaster", id: "master.job-validate", name: "job-validate",
      parent: "master.snapshot-rollback", containment: "inside", group: "master-phases", index: 1,
      previous: "", related: "", kind: "direct_inner", additivity: "do_not_sum",
      source: "cubemaster_stages.job-validate",
      note: "ROLLBACK_PREPARING：三把写锁/快照归属/节点亲和/gen 分配/存储水位。",
    },
    {
      order: 30, depth: 2, component: "Cubelet", id: "cubelet.rollback-service", name: "rollback-service",
      parent: "master.snapshot-rollback", containment: "remote_child", group: "master-rollback", index: 1,
      previous: "", related: "", kind: "direct_outer", additivity: "do_not_sum",
      source: "cubelet_stages.rollback-service",
      note: "CubeMaster 调用 Cubelet RollbackSandbox 的服务窗口；跨进程计时。",
    },
    {
      order: 35, depth: 3, component: "Cubelet", id: "cubelet.rollback-inner", name: "rollback-inner",
      parent: "cubelet.rollback-service", containment: "derived_from", group: "", index: "",
      previous: "", related: "cubelet.persist-rootfs", kind: "derived_residual", additivity: "do_not_sum",
      source: "cubelet_stages.rollback-inner",
      note: "源码按 service - 可观测子阶段 派生；不是独立连续时间段。",
    },
    {
      order: 40, depth: 3, component: "Cubelet", id: "cubelet.resolve-targets", name: "resolve-targets",
      parent: "cubelet.rollback-service", containment: "partial_observation", group: "", index: "",
      previous: "", related: "", kind: "thresholded_direct", additivity: "do_not_sum",
      source: "cubelet_stages.resolve-targets",
      note: "目标卷解析 + new_gen 校验；仅超过日志阈值的样本可见。",
    },
    {
      order: 41, depth: 3, component: "Cubelet", id: "cubelet.mount-metadata", name: "mount-metadata",
      parent: "cubelet.rollback-service", containment: "partial_observation", group: "", index: "",
      previous: "", related: "", kind: "thresholded_direct", additivity: "do_not_sum",
      source: "cubelet_stages.mount-metadata",
      note: "S3 后端克隆 sealed 快照元数据盘；XFS 为 no-op，多数样本无此记录。",
    },
    {
      order: 42, depth: 3, component: "Cubelet", id: "cubelet.derive-rootfs", name: "derive-rootfs",
      parent: "cubelet.rollback-service", containment: "inside", group: "cubelet-rollback", index: 1,
      previous: "", related: "", kind: "direct_inner", additivity: "do_not_sum",
      source: "cubelet_stages.derive-rootfs",
      note: "FICLONE 派生 sb-<id>-rootfs-genN；O(1) 元数据操作，通常极小。",
    },
    {
      order: 50, depth: 4, component: "CubeShim", id: "shim.RollbackSnapshot", name: "RollbackSnapshot",
      parent: "cubelet.rollback-service", containment: "cross_component_inside", group: "cubelet-rollback", index: 2,
      previous: "cubelet.derive-rootfs", related: "", kind: "direct_outer", additivity: "partial_children",
      source: "shim_stages.RollbackSnapshot",
      note: "task.Update(RollbackSnapshot) 的 Shim 主窗口；DeleteVm/RestoreVm/ResetVm/ConnectAgent 顺序执行但不覆盖连接残差。",
    },
    {
      order: 51, depth: 5, component: "CubeShim", id: "shim.DeleteVm", name: "DeleteVm",
      parent: "shim.RollbackSnapshot", containment: "inside", group: "shim-rollback", index: 1,
      previous: "", related: "", kind: "direct_inner", additivity: "partial_sequential_child",
      source: "shim_stages.DeleteVm", note: "VmDelete 删除旧 VM；VMM 进程保留。",
    },
    {
      order: 52, depth: 5, component: "CubeShim", id: "shim.RestoreVm", name: "RestoreVm",
      parent: "shim.RollbackSnapshot", containment: "inside", group: "shim-rollback", index: 2,
      previous: "shim.DeleteVm", related: "hypervisor.restore-memory", kind: "direct_inner", additivity: "partial_sequential_child",
      source: "shim_stages.RestoreVm", note: "VmResumeFromSnapshot；读 guest 内存镜像，回滚主热点。",
    },
    {
      order: 52.1, depth: 6, component: "Hypervisor", id: "hypervisor.restore-memory", name: "restore-memory",
      parent: "shim.RestoreVm", containment: "inside", group: "shim-restore-memory", index: 1,
      previous: "", related: "shim.RestoreVm", kind: "direct_inner", additivity: "partial_sequential_child",
      source: "shim_stages.RestoreMemory",
      note: "new_from_snapshot 读入 guest 内存镜像（fast mmap / slow fill_saved_regions）；成本 ∝ 快照内存大小。",
    },
    {
      order: 53, depth: 5, component: "CubeShim", id: "shim.ResetVm", name: "ResetVm",
      parent: "shim.RollbackSnapshot", containment: "inside", group: "shim-rollback", index: 3,
      previous: "shim.RestoreVm", related: "", kind: "direct_inner", additivity: "partial_sequential_child",
      source: "shim_stages.ResetVm", note: "reset_guest 重置 Guest 状态。",
    },
    {
      order: 54, depth: 5, component: "CubeShim", id: "shim.ConnectAgent", name: "ConnectAgent",
      parent: "shim.RollbackSnapshot", containment: "inside", group: "shim-rollback", index: 4,
      previous: "shim.ResetVm", related: "", kind: "direct_inner", additivity: "partial_sequential_child",
      source: "shim_stages.ConnectAgent", note: "guest agent 重连。",
    },
    {
      order: 55, depth: 5, component: "CubeShim", id: "shim.rollback-request-total", name: "rollback request total",
      parent: "shim.RollbackSnapshot", containment: "equivalent_window", group: "", index: "",
      previous: "", related: "shim.RollbackSnapshot", kind: "direct_boundary", additivity: "do_not_sum",
      source: "rollback_detail.direct_timings_ms.rollback_request_total",
      note: "rollback req 起止边界，与 RollbackSnapshot 近似同窗；不可相加。",
    },
    {
      order: 56, depth: 5, component: "CubeShim", id: "shim.vm-downtime", name: "VM downtime",
      parent: "shim.RollbackSnapshot", containment: "overlapping_window", group: "", index: "",
      previous: "", related: "shim.DeleteVm|shim.RestoreVm|shim.ResetVm|shim.ConnectAgent",
      kind: "direct_boundary", additivity: "do_not_sum",
      source: "rollback_detail.direct_timings_ms.vm_downtime",
      note: "delete_vm_start → agent_ready；guest 完全停机窗口，与多个 Shim 子阶段重叠。",
    },
    {
      order: 57, depth: 5, component: "CubeShim", id: "shim.memory-restore-cumulative", name: "memory restore cumulative",
      parent: "shim.RollbackSnapshot", containment: "cumulative_from_origin", group: "shim-cumulative-boundaries", index: 1,
      previous: "", related: "hypervisor.restore-memory", kind: "cumulative_marker", additivity: "do_not_sum",
      source: "rollback_detail.direct_timings_ms.memory_restore_cumulative",
      note: "从 req 起点累计到内存恢复完成；表示位置，不是独立可加阶段。",
    },
    {
      order: 60, depth: 3, component: "Cubelet", id: "cubelet.persist-rootfs", name: "persist-rootfs",
      parent: "cubelet.rollback-service", containment: "inside", group: "cubelet-rollback", index: 3,
      previous: "shim.RollbackSnapshot", related: "", kind: "direct_inner", additivity: "do_not_sum",
      source: "cubelet_stages.persist-rootfs",
      note: "PersistSandboxRootfs + ReleaseRollbackReplacedVolumes；回滚后清理，best-effort。",
    },
    {
      order: 61, depth: 3, component: "Cubelet", id: "cubelet.network-bump", name: "network-bump",
      parent: "cubelet.rollback-service", containment: "partial_observation", group: "", index: "",
      previous: "", related: "", kind: "thresholded_direct", additivity: "do_not_sum",
      source: "cubelet_stages.network-bump",
      note: "cubevs.BumpMvmVersion 重置陈旧 TCP 会话；best-effort，仅超阈值样本可见。",
    },
    {
      order: 70, depth: 2, component: "CubeMaster", id: "master.runtime-ref", name: "runtime-ref",
      parent: "master.snapshot-rollback", containment: "inside", group: "master-phases", index: 2,
      previous: "master.job-validate", related: "", kind: "direct_inner", additivity: "do_not_sum",
      source: "cubemaster_stages.runtime-ref",
      note: "ROLLBACK_RECOVERING：AcquireSnapshotRuntimeRef 绑定快照；失败→FAILED。",
    },
  ];

  const get = (object, dottedPath) => dottedPath
    .split(".")
    .reduce((value, key) => value?.[key], object);
  const knownSources = new Set(definitions.map((definition) => definition.source));
  const fallback = [];
  for (const [section, component, prefix] of [
    ["cubelet_stages", "Cubelet", "cubelet"],
    ["shim_stages", "CubeShim", "shim"],
    ["cubemaster_stages", "CubeMaster", "master"],
  ]) {
    for (const name of Object.keys(result[section] ?? {})) {
      const source = `${section}.${name}`;
      if (knownSources.has(source)) continue;
      fallback.push({
        order: 900 + fallback.length, depth: "", component, id: `${prefix}.${name}`,
        name, parent: "", containment: "unknown", group: "", index: "", previous: "",
        related: "", kind: "direct_unknown", additivity: "do_not_sum", source,
        note: "分析器尚未定义该阶段的顺序和包含关系，请结合源码补充拓扑。",
      });
    }
  }

  const allDefinitions = [...definitions, ...fallback];
  const includedDefinitions = allDefinitions.filter((definition) => get(result, definition.source));
  const definitionsById = new Map(allDefinitions.map((definition) => [definition.id, definition]));
  const effectiveSequence = new Map();
  for (const group of new Set(includedDefinitions.map((definition) => definition.group).filter(Boolean))) {
    const members = includedDefinitions
      .filter((definition) => definition.group === group)
      .sort((left, right) => Number(left.index) - Number(right.index));
    members.forEach((definition, index) => {
      effectiveSequence.set(definition.id, {
        index: index + 1,
        previous: index > 0 ? members[index - 1].id : "",
      });
    });
  }
  const hierarchyPath = (definition, visited = new Set()) => {
    if (!definition.parent || visited.has(definition.id)) return definition.id;
    const parent = definitionsById.get(definition.parent);
    if (!parent) return `${definition.parent} > ${definition.id}`;
    const nextVisited = new Set(visited).add(definition.id);
    return `${hierarchyPath(parent, nextVisited)} > ${definition.id}`;
  };
  const headers = [
    "flow_order", "depth", "component", "stage_id", "stage_name", "parent_stage_id",
    "hierarchy_path", "containment_relation", "sequence_group", "sequence_index", "previous_stage_id",
    "related_stage_ids", "timing_kind", "additivity", "source_field", "count", "avg_ms",
    "min_ms", "p50_ms", "p95_ms", "max_ms", "notes",
  ];
  const rows = includedDefinitions.flatMap((definition) => {
    const stageStats = get(result, definition.source);
    const sequence = effectiveSequence.get(definition.id);
    return [[
      definition.order, definition.depth, definition.component, definition.id, definition.name,
      definition.parent, hierarchyPath(definition), definition.containment, definition.group,
      sequence?.index ?? definition.index, sequence?.previous ?? definition.previous,
      definition.related, definition.kind, definition.additivity,
      definition.source, stageStats.count, stageStats.avg, stageStats.min, stageStats.p50,
      stageStats.p95, stageStats.max, definition.note,
    ]];
  });
  return `${[headers, ...rows].map((row) => row.map(csvValue).join(",")).join("\n")}\n`;
}

// ── 回滚详细子阶段（嵌入式时间戳解析）──────────────────────────────────
// 同一沙箱可能被多次回滚（多轮基准/重试）：明细按"每次回滚尝试"分段——同一实例
// 再次出现 "rollback req start" 即开启新的 attempt；RollbackSnapshot 耗时
// 按实例内第 N 次回滚与第 N 条 stat 记录配对，避免多次回滚漏记或错配。
function buildRollbackDetail(shimRequestRecords, shimRollbackRecords, ids, eventSource) {
  const requestsByInstance = new Map();
  for (const record of shimRequestRecords) {
    if (!ids.has(record.InstanceId) || typeof record.LogContent !== "string") continue;
    if (!requestsByInstance.has(record.InstanceId)) requestsByInstance.set(record.InstanceId, []);
    requestsByInstance.get(record.InstanceId).push(record);
  }

  const snapshotCostsByInstance = new Map();
  for (const record of shimRollbackRecords) {
    if (record.CalleeAction !== "RollbackSnapshot") continue;
    const cost = Number(record.CostTime);
    if (!Number.isFinite(cost)) continue;
    if (!snapshotCostsByInstance.has(record.InstanceId)) {
      snapshotCostsByInstance.set(record.InstanceId, []);
    }
    snapshotCostsByInstance.get(record.InstanceId).push(cost);
  }

  const details = [];
  for (const [instanceId, records] of requestsByInstance) {
    let attempt = null;
    let attemptIndex = 0;
    const openAttempt = () => {
      attemptIndex += 1;
      attempt = { instance_id: instanceId, rollback_attempt: attemptIndex, path: "unknown" };
      details.push(attempt);
    };
    for (const record of records) {
      const content = record.LogContent.trim();
      const hostTimestamp = parseTimestamp(record.Timestamp);
      if (content === "rollback req start" || !attempt) openAttempt();

      if (content === "rollback req start") setFirst(attempt, "rollback_req_start_host_ms", hostTimestamp);
      else if (content === "delete vm start") setFirst(attempt, "delete_vm_start_host_ms", hostTimestamp);
      else if (content === "delete vm finish") setFirst(attempt, "delete_vm_finish_host_ms", hostTimestamp);
      else if (content === "restore start") setFirst(attempt, "restore_start_host_ms", hostTimestamp);
      else if (content === "restore finish") setFirst(attempt, "restore_finish_host_ms", hostTimestamp);
      else if (content === "agent is ready") setFirst(attempt, "agent_ready_host_ms", hostTimestamp);
      else if (content === "rollback req finish") setFirst(attempt, "rollback_req_finish_host_ms", hostTimestamp);

      const memoryRestore = content.match(/^memory restore finish at:(\d+(?:\.\d+)?)$/);
      if (memoryRestore) attempt.memory_restore_cumulative_ms = Number(memoryRestore[1]);

      const embedded = parseEmbeddedLog(content);
      if (embedded?.msg === "[cube-strace]recv restore") {
        setFirst(attempt, "guest_restore_receive_ms", parseTimestamp(embedded.ts));
      } else if (embedded?.msg === "restore by snapshot") {
        attempt.path = "snapshot-restore";
        setFirst(attempt, "guest_restore_branch_ms", parseTimestamp(embedded.ts));
      }
    }
  }

  for (const detail of details) {
    detail.path ??= "unknown";
    const costs = snapshotCostsByInstance.get(detail.instance_id) ?? [];
    detail.shim_rollback_snapshot_ms = costs[detail.rollback_attempt - 1] ?? null;
    detail.rollback_request_total_ms = difference(
      detail.rollback_req_finish_host_ms,
      detail.rollback_req_start_host_ms,
    );
    detail.delete_vm_ms = difference(
      detail.delete_vm_finish_host_ms,
      detail.delete_vm_start_host_ms,
    );
    detail.restore_ms = difference(
      detail.restore_finish_host_ms,
      detail.restore_start_host_ms,
    );
    detail.vm_downtime_ms = difference(
      detail.agent_ready_host_ms,
      detail.delete_vm_start_host_ms,
    );
    detail.guest_receive_to_restore_branch_ms = difference(
      detail.guest_restore_branch_ms,
      detail.guest_restore_receive_ms,
    );
    detail.restore_path_unresolved_ms = difference(
      detail.restore_ms,
      detail.guest_receive_to_restore_branch_ms,
    );
  }

  const markerKeys = [
    "shim_rollback_snapshot_ms", "rollback_request_total_ms", "delete_vm_ms",
    "restore_ms", "memory_restore_cumulative_ms", "vm_downtime_ms",
    "guest_restore_receive_ms", "guest_restore_branch_ms", "guest_receive_to_restore_branch_ms",
  ];
  const coverage = Object.fromEntries(
    markerKeys.map((key) => [key, details.filter((detail) => detail[key] != null).length]),
  );
  const pathCounts = {};
  for (const detail of details) pathCounts[detail.path] = (pathCounts[detail.path] ?? 0) + 1;

  return {
    summary: {
      schema_version: 1,
      event_source: eventSource,
      raw_event_count: shimRequestRecords.length,
      instance_count: new Set(details.map((detail) => detail.instance_id)).size,
      rollback_attempt_count: details.length,
      path_counts: pathCounts,
      marker_coverage: coverage,
      direct_timings_ms: {
        rollback_request_total: numericStats(details, "rollback_request_total_ms"),
        delete_vm: numericStats(details, "delete_vm_ms"),
        restore_ms: numericStats(details, "restore_ms"),
        memory_restore_cumulative: numericStats(details, "memory_restore_cumulative_ms"),
        vm_downtime: numericStats(details, "vm_downtime_ms"),
        guest_receive_to_restore_branch: numericStats(
          details,
          "guest_receive_to_restore_branch_ms",
        ),
      },
      diagnostic_residual_ms: {
        restore_path_unresolved: numericStats(details, "restore_path_unresolved_ms"),
      },
      interpretation: {
        restore_ms: "Same-process timestamp difference; directly comparable with Shim RestoreVm.",
        vm_downtime: "delete_vm_start → agent_ready; guest fully unavailable, overlaps DeleteVm/RestoreVm/ResetVm/ConnectAgent.",
        rollback_request_total: "Equivalent window of RollbackSnapshot; do not add to sub-stages.",
        restore_path_unresolved: "Shim RestoreVm minus guest branch-selection time; includes request/RPC and restore dispatch.",
        rollback_attempt: "Details are segmented per rollback attempt (a new 'rollback req start' opens a new attempt); RollbackSnapshot cost is paired by attempt order.",
      },
    },
    details,
  };
}

// ── 主流程 ─────────────────────────────────────────────────────────────
// run.log 中 ROLLBACK_BEGIN/ROLLBACK_END 标记给出每组用例的采集时间窗，
// 阶段日志按窗口关联到对应 case（多组并发度互不混入；run.log 缺失时回退为不过滤）
const caseWindows = new Map();
const runLogFile = path.join(profileDir, "run.log");
if (fs.existsSync(runLogFile)) {
  for (const line of fs.readFileSync(runLogFile, "utf8").split("\n")) {
    let match = line.match(/^(\S+)\s+ROLLBACK_BEGIN name=(\S+)/);
    if (match) {
      const begin = parseTimestamp(match[1]);
      if (begin != null) caseWindows.set(match[2], { begin, end: null });
      continue;
    }
    match = line.match(/^(\S+)\s+ROLLBACK_END name=(\S+)/);
    if (match) {
      const rollbackWindow = caseWindows.get(match[2]);
      const end = parseTimestamp(match[1]);
      if (rollbackWindow && end != null) rollbackWindow.end = end;
    }
  }
}

function analyzeCase(currentCaseName) {
  const analysisWindow = caseWindows.get(currentCaseName) ?? null;
  const inWindow = analysisWindow && analysisWindow.begin != null
    ? (record) => {
        const timestamp = parseTimestamp(record.Timestamp);
        return timestamp != null && timestamp >= analysisWindow.begin &&
          (analysisWindow.end == null || timestamp <= analysisWindow.end);
      }
    : () => true;

  const summary = JSON.parse(
    fs.readFileSync(path.join(latencyDir, `${currentCaseName}.summary.json`), "utf8"),
  );
  const benchmarkRaw = JSON.parse(
    fs.readFileSync(path.join(latencyDir, `${currentCaseName}.json`), "utf8"),
  );
  const benchmark = {
    case: summary.case,
    concurrency: summary.concurrency,
    rounds: summary.rounds,
    dirty_mb: summary.dirty_mb,
    bench_exit: summary.bench_exit,
    successful: summary.successful,
    errors: summary.errors,
    success_rate: summary.success_rate,
    total_time_s: summary.total_time_s,
    throughput_rps: summary.throughput_rps,
    wall_ms: summary.wall_ms,
    per_rollback_ms: summary.per_rollback_ms,
  };
  const benchmarkRounds = benchmarkRaw.raw ?? [];

  const cubeletRecords = readJsonLines(path.join(evidenceDir, "cubelet_stat-delta.log"))
    .filter(inWindow);
  const rollbackRoots = cubeletRecords.filter(
    (record) => record.Action === "Rollback" && record.Callee === "rollback-service",
  );
  const instanceIds = new Set(rollbackRoots.map((record) => record.InstanceId));
  const cubeletRollback = cubeletRecords.filter(
    (record) => record.Action === "Rollback" && instanceIds.has(record.InstanceId),
  );

  const shimRecords = readJsonLines(path.join(evidenceDir, "shim_stat-delta.log"))
    .filter(inWindow);
  const shimRollback = shimRecords.filter(
    (record) => record.Action === "Rollback" && instanceIds.has(record.InstanceId),
  );
  const focusedRollbackFile = path.join(evidenceDir, "rollback-events.jsonl");
  const shimRequestFile = fs.existsSync(focusedRollbackFile)
    ? focusedRollbackFile
    : path.join(evidenceDir, "shim_req-delta.log");
  const shimRequestRecords = fs.existsSync(shimRequestFile)
    ? readJsonLines(shimRequestFile).filter(inWindow)
    : [];
  const rollbackDetail = buildRollbackDetail(
    shimRequestRecords,
    shimRollback,
    instanceIds,
    path.relative(profileDir, shimRequestFile),
  );

  const masterRecords = readJsonLines(path.join(evidenceDir, "master-delta.log"))
    .filter(inWindow);
  const masterExtensions = [];
  for (const record of masterRecords) {
    if (typeof record.LogContent !== "string") continue;
    if (!record.LogContent.startsWith("RollbackSnapshot_rsp:")) continue;
    try {
      const response = JSON.parse(record.LogContent.slice("RollbackSnapshot_rsp:".length));
      if (instanceIds.has(response.sandbox_id)) masterExtensions.push(response.ext_info ?? {});
    } catch {
      // Ignore incomplete or unrelated request log records.
    }
  }

  const masterStages = {};
  for (const stage of ["snapshot-rollback", "job-validate", "runtime-ref"]) {
    masterStages[stage] = stats(
      masterExtensions
        .map((extension) => Number(extension[stage]))
        .filter(Number.isFinite),
    );
  }

  return {
    section: {
      case: currentCaseName,
      benchmark,
      benchmark_rounds: benchmarkRounds,
      case_window: analysisWindow
        ? { begin: analysisWindow.begin, end: analysisWindow.end }
        : null,
      correlated_instances: instanceIds.size,
      cubelet_stages: groupStageStats(cubeletRollback, "Callee"),
      shim_stages: groupStageStats(shimRollback, "CalleeAction"),
      rollback_detail: rollbackDetail.summary,
      cubemaster_stages: masterStages,
    },
    details: rollbackDetail.details,
  };
}

const cases = {};
const detailsByCase = {};
for (const name of caseSummaries) {
  const analysis = analyzeCase(name);
  cases[name] = analysis.section;
  detailsByCase[name] = analysis.details;
}
// 顶层字段对第一组 case（沿用 stage-latency.csv / Excel 报表的口径）；
// cases 字段携带每组 case 的分段统计
const primaryCase = caseSummaries[0];
const primary = cases[primaryCase];

const hostSamplesFile = path.join(evidenceDir, "host-samples.csv");
const hostLines = fs.existsSync(hostSamplesFile)
  ? fs.readFileSync(hostSamplesFile, "utf8").trim().split("\n")
  : [];
const hostHeaders = hostLines.length > 0 ? hostLines[0].split(",") : [];
const hostRows = hostLines.slice(1).map((line) =>
  Object.fromEntries(
    line.split(",").map((value, index) => [hostHeaders[index], Number(value)]),
  ),
);

const cpuFields = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"];
const hostIntervals = [];
for (let index = 1; index < hostRows.length; index += 1) {
  const previous = hostRows[index - 1];
  const current = hostRows[index];
  const delta = Object.fromEntries(
    cpuFields.map((field) => [field, current[field] - previous[field]]),
  );
  const total = cpuFields.reduce((sum, field) => sum + delta[field], 0);
  const busy = total - delta.idle - delta.iowait - delta.steal;
  const elapsed = current.uptime_s - previous.uptime_s;
  hostIntervals.push({
    end_s: current.uptime_s - hostRows[0].uptime_s,
    elapsed_s: elapsed,
    busy_pct: (busy / total) * 100,
    system_pct: (delta.system / total) * 100,
    iowait_pct: (delta.iowait / total) * 100,
    idle_pct: (delta.idle / total) * 100,
    ctxt_per_s: (current.ctxt - previous.ctxt) / elapsed,
    processes_per_s: (current.processes - previous.processes) / elapsed,
    intr_per_s: (current.intr - previous.intr) / elapsed,
    softirq_per_s: (current.softirq_total - previous.softirq_total) / elapsed,
    runnable: current.runnable,
    procs_running: current.procs_running,
    procs_blocked: current.procs_blocked,
    threads: current.threads,
    shim_count: current.shim_count,
  });
}

const bpfFile = path.join(evidenceDir, "bpftrace-profile.txt");
const bpfText = fs.existsSync(bpfFile) ? fs.readFileSync(bpfFile, "utf8") : "";
const migrationSamples = [...bpfText.matchAll(/@(vm_kernel\[[\s\S]*?migration_entry_wait[\s\S]*?\]): (\d+)/g)]
  .map((match) => Number(match[2]))
  .reduce((sum, value) => sum + value, 0);

const maxHostValue = (field) =>
  hostRows.length > 0 ? Math.max(...hostRows.map((row) => row[field])) : null;
const maxIntervalValue = (field) =>
  hostIntervals.length > 0 ? Math.max(...hostIntervals.map((row) => row[field])) : null;

const result = {
  case: primary.case,
  benchmark: primary.benchmark,
  benchmark_rounds: primary.benchmark_rounds,
  case_window: primary.case_window,
  correlated_instances: primary.correlated_instances,
  cubelet_stages: primary.cubelet_stages,
  shim_stages: primary.shim_stages,
  rollback_detail: primary.rollback_detail,
  cubemaster_stages: primary.cubemaster_stages,
  host_summary:
    hostRows.length > 0
      ? {
          sample_count: hostRows.length,
          max_runnable: maxHostValue("runnable"),
          max_procs_running: maxHostValue("procs_running"),
          max_procs_blocked: maxHostValue("procs_blocked"),
          max_threads: maxHostValue("threads"),
          max_shims: maxHostValue("shim_count"),
          max_iowait_pct: maxIntervalValue("iowait_pct"),
          max_system_pct: maxIntervalValue("system_pct"),
          max_ctxt_per_s: maxIntervalValue("ctxt_per_s"),
          max_processes_per_s: maxIntervalValue("processes_per_s"),
        }
      : null,
  host_intervals: hostIntervals,
  bpftrace: { migration_entry_wait_samples: migrationSamples },
  cases,
  generated_files: {
    stage_latency_csv: path.relative(profileDir, csvOut),
    rollback_details_jsonl: detailsOut
      ? path.relative(profileDir, detailsOut)
      : null,
  },
};

if (detailsOut) {
  fs.mkdirSync(path.dirname(detailsOut), { recursive: true });
  fs.writeFileSync(
    detailsOut,
    `${detailsByCase[primaryCase].map((detail) => JSON.stringify(detail)).join("\n")}\n`,
  );
}

fs.mkdirSync(path.dirname(csvOut), { recursive: true });
fs.writeFileSync(csvOut, buildStageRelationshipCsv(result));

process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
