#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


SUBSKILL_CATALOG = {
    "cpu-affinity-optimization": "candidate-cpu-affinity",
    "network-optimization": "candidate-network",
    "performance-library-selection": "candidate-performance-library",
    "application-config-optimization": "candidate-application-config",
    "bios-optimization": "candidate-bios",
    "os-optimization": "candidate-os",
    "compiler-optimization": "candidate-compiler",
    "accelerator-optimization": "candidate-accelerator",
    "hardware-upgrade-analysis": "candidate-hardware-upgrade",
    "other-optimization": "candidate-other",
    "io-memory-network-bottleneck-analysis": "candidate-bottleneck-prescreen",
    "database-workload-analysis": "candidate-database-workload",
}

MAIN_OPTIMIZATION_SUBSKILLS = [
    "application-config-optimization",
    "performance-library-selection",
    "cpu-affinity-optimization",
    "network-optimization",
    "compiler-optimization",
    "os-optimization",
    "bios-optimization",
    "accelerator-optimization",
    "hardware-upgrade-analysis",
    "other-optimization",
]

CPU_DATABASE_ROUTE = [
    "application-config-optimization",
    "performance-library-selection",
    "cpu-affinity-optimization",
    "os-optimization",
    "bios-optimization",
    "compiler-optimization",
]

CPU_COMPUTE_ROUTE = [
    "compiler-optimization",
    "performance-library-selection",
    "cpu-affinity-optimization",
    "os-optimization",
    "bios-optimization",
    "application-config-optimization",
]

CPU_DEFAULT_ROUTE = [
    "application-config-optimization",
    "cpu-affinity-optimization",
    "performance-library-selection",
    "os-optimization",
    "bios-optimization",
    "compiler-optimization",
]


# 多卡 NPU 拓扑字段：追加到任务包 schema，供 accelerator 子代理读取
NPU_TOPOLOGY_FIELDS = {
    "npu_device_ids": "string, NPU 设备 ID 列表",
    "tensor_parallel_size": "int, TP 并行度",
    "hccl_comm_group": "string, HCCL 通信组配置",
}

# change_mode 枚举：候选动作的变更模式分类
# 原有默认值：no_change / config_tune / runtime_param / restart_required
# 追加 NPU/推理场景相关枚举
CHANGE_MODE_ENUM = {
    "no_change": "无变更",
    "config_tune": "运行时配置参数调整",
    "runtime_param": "运行时参数调整",
    "restart_required": "需要重启服务",
    "model_reload": "需要重新加载模型权重",
    "quantization_change": "量化方式变更（如 FP16→INT8）",
    "parallelism_reconfigure": "并行度重新配置（如 TP degree 变更）",
}


def parse_key_values(values):
    result = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"invalid key=value item: {item}")
        key, value = item.split("=", 1)
        result[key] = value
    return result


def load_optional_json(path):
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON file {file_path}: {exc}") from exc


def load_evidence_metadata(evidence_dir):
    metadata_path = Path(evidence_dir) / "snapshot_metadata.json"
    if not metadata_path.exists():
        raise SystemExit(f"missing evidence metadata: {metadata_path}")
    return load_optional_json(metadata_path)


def load_performance_summary(evidence_dir, summary_path):
    candidates = []
    if summary_path:
        candidates.append(Path(summary_path))
    candidates.append(Path(evidence_dir) / "performance_signal_summary.json")
    for path in candidates:
        if path.exists():
            return load_optional_json(path), str(path)
    return {}, ""


def parse_iso_timestamp(value, label):
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(f"invalid {label} timestamp: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_subskills(value):
    if not value:
        return None
    selected = []
    for item in value.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in SUBSKILL_CATALOG:
            allowed = ", ".join(sorted(SUBSKILL_CATALOG))
            raise SystemExit(f"unknown subskill: {name}. Allowed: {allowed}")
        selected.append(name)
    if not selected:
        raise SystemExit("--subskills did not contain any valid subskill names")
    return selected


def as_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return default


def is_third_party_dso(item):
    classification = str(item.get("classification", item.get("library_type", ""))).lower()
    dso = str(item.get("dso", item.get("so", item.get("library", "")))).lower()
    if classification in {"third_party", "external", "external_library", "third-party"}:
        return True
    if ".so" not in dso:
        return False
    if any(token in dso for token in ("linux-vdso", "ld-linux", "libpthread", "libc.so", "libm.so", "librt.so")):
        return False
    return True


NETWORK_TOKENS = (
    "tcp", "udp", "socket", "sock", "epoll", "poll", "select", "sendmsg", "recvmsg",
    "sendto", "recvfrom", "softirq", "net_rx", "net_tx", "napi", "skb", "xmit",
    "gro", "gso", "iptables", "iptable", "nft_", "netfilter", "mlx", "ixgbe", "i40e",
    "ena_", "virtio_net", "irq",
)


def is_network_hotspot(item):
    category = str(item.get("category", item.get("domain", ""))).lower()
    if category in {"network", "net", "network_io"}:
        return True
    haystack = " ".join(
        str(item.get(key, "")).lower()
        for key in ("symbol", "function", "name", "dso", "module")
    )
    return any(token in haystack for token in NETWORK_TOKENS)


def add_candidate(candidates, subskill_name, priority, reason, source_signal, evidence_path):
    existing = candidates.get(subskill_name)
    entry = {
        "subskill_name": subskill_name,
        "priority": priority,
        "reason": reason,
        "source_signal": source_signal,
        "required_evidence": [evidence_path] if evidence_path else [],
    }
    if existing is None:
        candidates[subskill_name] = entry
        return
    if existing.get("priority") != "high" and priority == "high":
        existing["priority"] = priority
    existing["reason"] = existing.get("reason", "") + "; " + reason
    if evidence_path and evidence_path not in existing.get("required_evidence", []):
        existing.setdefault("required_evidence", []).append(evidence_path)


def parse_npu_signals(evidence_dir):
    """解析 NPU 采集信号，生成 accelerator 候选所需的信号摘要。

    解析 evidence_dir 下的 NPU 采集产物：
      - npu_utilization.log: npu-smi info -t usages 周期性采样
      - npu_topology.json: 多卡拓扑（npu_device_ids / tensor_parallel_size / hccl_comm_group）
      - npu_health.log: npu-smi info 健康检查输出

    返回 dict，关键字段：
      cpu_feed_bottleneck: bool  CPU 端数据准备瓶颈
      npu_compute_bottleneck: bool  NPU 计算瓶颈
      hbm_pressure: bool  HBM 使用率高
      npu_device_ids: str  NPU 设备 ID 列表（逗号分隔）
      tensor_parallel_size: int  TP 并行度
      hccl_comm_group: str  HCCL 通信组配置
      npu_count: int  NPU 卡数
      raw_evidence: list  命中的证据文件路径
      signals: dict  各信号量值
    """
    signals = {
        "cpu_feed_bottleneck": False,
        "npu_compute_bottleneck": False,
        "hbm_pressure": False,
        "npu_device_ids": "",
        "tensor_parallel_size": 0,
        "hccl_comm_group": "",
        "npu_count": 0,
        "raw_evidence": [],
        "signals": {},
    }

    evidence_dir_path = Path(evidence_dir)

    # 1. npu_utilization.log 解析
    npu_log = evidence_dir_path / "npu_utilization.log"
    if npu_log.exists():
        signals["raw_evidence"].append(str(npu_log))
        try:
            text = npu_log.read_text(encoding="utf-8", errors="ignore")
            util_values = []
            hbm_values = []
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                low = stripped.lower()
                # 形如 "NPU Util : 12.3%" 或 "AICore Util : 12.3%"
                if "util" in low and "%" in stripped:
                    try:
                        pct_str = stripped.split("%", 1)[0].rsplit(":", 1)[-1].strip()
                        util_values.append(float(pct_str))
                    except (ValueError, IndexError):
                        continue
                # HBM 使用率
                if "hbm" in low and ("%" in stripped or "gb" in low):
                    if "%" in stripped:
                        try:
                            pct_str = stripped.split("%", 1)[0].rsplit(":", 1)[-1].strip()
                            hbm_values.append(float(pct_str))
                        except (ValueError, IndexError):
                            continue
            if util_values:
                avg_util = sum(util_values) / len(util_values)
                max_util = max(util_values)
                signals["signals"]["npu_util_avg_pct"] = round(avg_util, 2)
                signals["signals"]["npu_util_max_pct"] = round(max_util, 2)
                # NPU 利用率持续低位 + CPU 高负载 → cpu_feed_bottleneck
                # NPU 利用率持续高位 → npu_compute_bottleneck
                if max_util < 30.0:
                    signals["cpu_feed_bottleneck"] = True
                if avg_util > 85.0:
                    signals["npu_compute_bottleneck"] = True
            if hbm_values:
                avg_hbm = sum(hbm_values) / len(hbm_values)
                signals["signals"]["hbm_usage_avg_pct"] = round(avg_hbm, 2)
                if avg_hbm > 90.0:
                    signals["hbm_pressure"] = True
        except OSError as exc:
            signals["signals"]["npu_utilization_parse_error"] = str(exc)

    # 2. npu_topology.json 多卡拓扑字段
    topo_path = evidence_dir_path / "npu_topology.json"
    if topo_path.exists():
        signals["raw_evidence"].append(str(topo_path))
        try:
            topo = json.loads(topo_path.read_text(encoding="utf-8"))
            if isinstance(topo, dict):
                signals["npu_device_ids"] = str(topo.get("npu_device_ids", ""))
                tp_size = topo.get("tensor_parallel_size")
                if tp_size is not None:
                    try:
                        signals["tensor_parallel_size"] = int(tp_size)
                    except (TypeError, ValueError):
                        pass
                signals["hccl_comm_group"] = str(topo.get("hccl_comm_group", ""))
                npu_count = topo.get("npu_count")
                if npu_count is not None:
                    try:
                        signals["npu_count"] = int(npu_count)
                    except (TypeError, ValueError):
                        pass
                # 备份：从 npu_device_ids 反推 count
                if not signals["npu_count"] and signals["npu_device_ids"]:
                    signals["npu_count"] = len(
                        [x for x in signals["npu_device_ids"].split(",") if x.strip()]
                    )
        except (OSError, json.JSONDecodeError) as exc:
            signals["signals"]["npu_topology_parse_error"] = str(exc)

    # 3. npu_health.log 健康检查（辅助判断 NPU 是否可用）
    health_log = evidence_dir_path / "npu_health.log"
    if health_log.exists():
        signals["raw_evidence"].append(str(health_log))
        try:
            health_text = health_log.read_text(encoding="utf-8", errors="ignore").lower()
            # 简单计数 OK/Alarm 标记
            ok_count = health_text.count("| ok")
            alarm_count = health_text.count("alarm") + health_text.count("error")
            signals["signals"]["npu_health_ok"] = ok_count
            signals["signals"]["npu_health_alarm"] = alarm_count
            if alarm_count > 0 and ok_count == 0:
                signals["signals"]["npu_health_blocked"] = True
        except OSError as exc:
            signals["signals"]["npu_health_parse_error"] = str(exc)

    return signals


def evidence_driven_candidates(summary, summary_path, args):
    candidates = {}
    dso_rank = summary.get("hotspot_dso_rank", [])
    function_rank = summary.get("hotspot_function_rank", [])
    topdown = summary.get("topdown", summary.get("topdown_summary", {}))
    threading = summary.get("threading", summary.get("threading_summary", {}))
    detected = summary.get("detected_signals", {})

    third_party_threshold = args.third_party_hotspot_threshold
    for item in dso_rank:
        percent = as_float(item.get("percent", item.get("samples_pct", item.get("overhead_pct"))))
        if percent >= third_party_threshold and is_third_party_dso(item):
            dso = item.get("dso", item.get("so", item.get("library", "unknown")))
            add_candidate(
                candidates,
                "performance-library-selection",
                "high",
                f"hotspot DSO {dso} reached {percent:.2f}%",
                "third_party_library_hotspot",
                summary_path,
            )
            break
    if detected.get("third_party_library_hotspot"):
        add_candidate(
            candidates,
            "performance-library-selection",
            "high",
            "performance summary marked third_party_library_hotspot",
            "third_party_library_hotspot",
            summary_path,
        )

    network_percent = sum(
        as_float(item.get("percent", item.get("samples_pct", item.get("overhead_pct"))))
        for item in function_rank
        if is_network_hotspot(item)
    )
    if network_percent >= args.network_hotspot_threshold or detected.get("network_hotspot_high"):
        add_candidate(
            candidates,
            "network-optimization",
            "high",
            f"network-related hotspot total reached {network_percent:.2f}%",
            "network_hotspot_high",
            summary_path,
        )

    icache_miss_pct = as_float(topdown.get("l1_icache_miss_pct", topdown.get("icache_miss_pct")))
    icache_high = bool(topdown.get("icache_miss_high") or topdown.get("l1_icache_miss_high") or detected.get("l1_icache_miss_high"))
    if icache_miss_pct >= args.icache_miss_threshold or icache_high:
        add_candidate(
            candidates,
            "compiler-optimization",
            "high",
            f"L1 icache miss is high ({icache_miss_pct:.2f}%); analyze PGO/LTO/code layout",
            "l1_icache_miss_high",
            summary_path,
        )

    l3_miss_pct = as_float(topdown.get("l3_cache_miss_pct", topdown.get("llc_miss_pct")))
    context_switch_rate = as_float(threading.get("context_switch_rate_per_sec", threading.get("ctx_switch_rate_per_sec")))
    context_switch_high = bool(threading.get("context_switch_high") or detected.get("context_switch_high"))
    l3_high = bool(topdown.get("l3_cache_miss_high") or topdown.get("llc_miss_high") or detected.get("l3_cache_miss_high"))
    if (context_switch_rate >= args.context_switch_threshold or context_switch_high) and (
        l3_miss_pct >= args.l3_miss_threshold or l3_high
    ):
        add_candidate(
            candidates,
            "cpu-affinity-optimization",
            "high",
            f"context switches ({context_switch_rate:.2f}/s) and L3/LLC miss ({l3_miss_pct:.2f}%) are high",
            "context_switch_high_and_l3_miss_high",
            summary_path,
        )

    if detected.get("application_config_signal"):
        add_candidate(candidates, "application-config-optimization", "medium", "application config signal detected", "application_config_signal", summary_path)
    if detected.get("os_signal"):
        add_candidate(candidates, "os-optimization", "medium", "OS tuning signal detected", "os_signal", summary_path)
    if detected.get("bios_signal"):
        add_candidate(candidates, "bios-optimization", "medium", "BIOS tuning signal detected", "bios_signal", summary_path)
    if detected.get("accelerator_signal"):
        add_candidate(candidates, "accelerator-optimization", "medium", "accelerator signal detected", "accelerator_signal", summary_path)
    if detected.get("hardware_capacity_signal"):
        add_candidate(candidates, "hardware-upgrade-analysis", "medium", "hardware capacity signal detected", "hardware_capacity_signal", summary_path)
    if detected.get("other_signal"):
        add_candidate(candidates, "other-optimization", "low", "unclassified optimization signal detected", "other_signal", summary_path)

    # NPU 信号路由：解析 evidence_dir 下的 NPU 采集产物
    evidence_dir = getattr(args, "evidence_dir", "")
    if evidence_dir:
        npu_signals = parse_npu_signals(evidence_dir)
        if npu_signals.get("npu_count") > 0 or npu_signals.get("raw_evidence"):
            accelerator_reason = []
            if npu_signals.get("cpu_feed_bottleneck"):
                accelerator_reason.append("NPU utilization low (cpu_feed_bottleneck suspected)")
            if npu_signals.get("npu_compute_bottleneck"):
                accelerator_reason.append("NPU utilization saturated (npu_compute_bottleneck)")
            if npu_signals.get("hbm_pressure"):
                accelerator_reason.append("HBM usage high (hbm_pressure)")
            if not accelerator_reason:
                accelerator_reason.append("NPU device present; verify accelerator subskill for topology/throughput")
            add_candidate(
                candidates,
                "accelerator-optimization",
                "high",
                "; ".join(accelerator_reason),
                "npu_signal",
                npu_signals["raw_evidence"][0] if npu_signals["raw_evidence"] else summary_path,
            )
            # 多卡 TP 场景提示应用层重新配置并行度
            if npu_signals.get("tensor_parallel_size", 0) > 1:
                add_candidate(
                    candidates,
                    "application-config-optimization",
                    "medium",
                    f"tensor_parallel_size={npu_signals['tensor_parallel_size']}; review batch/pipeline/TP balance",
                    "npu_tp_topology",
                    npu_signals["raw_evidence"][0] if npu_signals["raw_evidence"] else summary_path,
                )
            # HBM 压力提示硬件升级分析
            if npu_signals.get("hbm_pressure"):
                add_candidate(
                    candidates,
                    "hardware-upgrade-analysis",
                    "medium",
                    "HBM pressure detected; consider higher HBM capacity device",
                    "npu_hbm_pressure",
                    npu_signals["raw_evidence"][0] if npu_signals["raw_evidence"] else summary_path,
                )

    return list(candidates.values())


def default_subskills_for_route(bottleneck, workload_type, database_workload):
    normalized = (bottleneck or "unknown_bottleneck").strip().lower()
    workload = (workload_type or "unknown").strip().lower()

    if normalized == "network_bottleneck":
        return ["network-optimization", "os-optimization", "hardware-upgrade-analysis"]
    if normalized == "disk_bottleneck":
        return ["application-config-optimization", "os-optimization", "hardware-upgrade-analysis"]
    if normalized == "memory_capacity_bottleneck":
        return ["application-config-optimization", "os-optimization", "hardware-upgrade-analysis"]
    if normalized == "memory_bandwidth_bottleneck":
        return ["cpu-affinity-optimization", "application-config-optimization", "hardware-upgrade-analysis"]
    if normalized == "gpu_npu_bottleneck":
        return ["accelerator-optimization", "application-config-optimization", "hardware-upgrade-analysis"]
    if normalized == "hardware_capacity_limit":
        return ["hardware-upgrade-analysis"]
    if normalized in {"unknown_bottleneck", "no_active_bottleneck"}:
        return []
    if normalized == "cpu_bottleneck":
        if database_workload or any(token in workload for token in ("database", "mysql", "postgres", "redis")):
            return list(CPU_DATABASE_ROUTE)
        if any(token in workload for token in ("compute", "hpc", "batch", "codec", "crypto")):
            return list(CPU_COMPUTE_ROUTE)
        return list(CPU_DEFAULT_ROUTE)
    return []


def build_candidate_skill_list(args, performance_summary, performance_summary_path):
    explicit = parse_subskills(args.subskills)
    if explicit is not None:
        selected = [
            {
                "subskill_name": name,
                "priority": "high" if index == 0 else "medium",
                "reason": args.candidate_reason or args.route_reason or "explicitly requested candidate skill",
                "source_signal": "manual_candidate_list",
                "required_evidence": [performance_summary_path] if performance_summary_path else [],
            }
            for index, name in enumerate(explicit)
        ]
    else:
        selected = evidence_driven_candidates(performance_summary, performance_summary_path, args)
        if not selected:
            selected = [
                {
                    "subskill_name": name,
                    "priority": "medium",
                    "reason": "fallback from bottleneck classification because no specific performance signal matched",
                    "source_signal": "bottleneck_fallback",
                    "required_evidence": [performance_summary_path] if performance_summary_path else [],
                }
                for name in default_subskills_for_route(args.bottleneck, args.workload_type, args.database_workload)
            ]

    deduped = []
    seen = set()
    for item in selected:
        name = item["subskill_name"]
        if name in seen:
            continue
        seen.add(name)
        item["phase"] = "evidence_candidate"
        deduped.append(item)

    coverage_subskills = list(MAIN_OPTIMIZATION_SUBSKILLS)
    if args.database_workload and "database-workload-analysis" not in coverage_subskills:
        coverage_subskills.append("database-workload-analysis")

    if args.include_coverage_skills:
        for name in coverage_subskills:
            if name in seen:
                continue
            seen.add(name)
            deduped.append({
                "subskill_name": name,
                "phase": "coverage",
                "priority": "low",
                "reason": "coverage pass after evidence candidates complete; all main optimization skills must produce a conclusion",
                "source_signal": "coverage_after_candidate_list",
                "required_evidence": [performance_summary_path] if performance_summary_path else [],
            })

    # ── cpu-affinity-optimization mandatory-first rule ──
    # Per references/candidate-skill-list.md: cpu-affinity-optimization must
    # always be first in candidate_skill_list with priority=highest, regardless
    # of whether evidence signals match. It is the only signal-threshold-exempt
    # mandatory candidate skill.
    cpu_affinity_entry = None
    remaining = []
    for item in deduped:
        if item["subskill_name"] == "cpu-affinity-optimization":
            cpu_affinity_entry = item
        else:
            remaining.append(item)

    if cpu_affinity_entry is not None:
        # Promote existing entry to highest priority
        cpu_affinity_entry["priority"] = "highest"
        cpu_affinity_entry["phase"] = "evidence_candidate"
        existing_reason = cpu_affinity_entry.get("reason", "")
        if "mandatory_baseline_check" not in existing_reason:
            cpu_affinity_entry["reason"] = (
                existing_reason + "; mandatory first-priority candidate (signal-threshold-exempt)"
                if existing_reason
                else "mandatory first-priority candidate (signal-threshold-exempt)"
            )
        cpu_affinity_entry["source_signal"] = (
            cpu_affinity_entry.get("source_signal", "") + "+mandatory_first"
            if cpu_affinity_entry.get("source_signal")
            else "mandatory_first"
        )
    else:
        # Insert a new mandatory entry even if no signal matched
        cpu_affinity_entry = {
            "subskill_name": "cpu-affinity-optimization",
            "priority": "highest",
            "reason": "mandatory first-priority candidate (signal-threshold-exempt)",
            "source_signal": "mandatory_first",
            "required_evidence": [performance_summary_path] if performance_summary_path else [],
            "phase": "evidence_candidate",
        }

    deduped = [cpu_affinity_entry] + remaining

    for index, item in enumerate(deduped, start=1):
        item["candidate_id"] = f"candidate-skill-{index:03d}"
        item["stop_rule"] = "stop this subskill after all subskill recommendations are verified"
    return deduped


def main():
    parser = argparse.ArgumentParser(description="Create candidate skill analysis task packages from current performance evidence.")
    parser.add_argument("--scenario", required=True, help="Scenario name")
    parser.add_argument("--baseline", default="", help="Path to baseline JSON")
    parser.add_argument("--evidence-dir", required=True, help="Directory containing baseline and profiling evidence")
    parser.add_argument("--current-run-id", required=True, help="Current run id that evidence and task outputs must match")
    parser.add_argument("--current-run-started-at", default="", help="Current run start timestamp")
    parser.add_argument("--current-run-manifest", default="", help="Path to current run manifest JSON")
    parser.add_argument("--current-evidence-status", default="", help="Override evidence status; defaults to snapshot metadata")
    parser.add_argument("--target-identity-path", default="", help="Path to target instance identity JSON")
    parser.add_argument("--target-pid", default="", help="Target application PID")
    parser.add_argument("--output-dir", default="output/candidate-skill-tasks", help="Directory for generated candidate skill task JSON files")
    parser.add_argument("--results-dir", default="output/candidate-skill-results", help="Expected directory for candidate skill result JSON files")
    parser.add_argument("--cpu-set", default="", help="Applied target CPU set, for example 0-7")
    parser.add_argument("--memory-limit", default="", help="Applied target memory limit, for example 32G")
    parser.add_argument("--workload-type", default="unknown", help="Workload type hint, for example database or compute")
    parser.add_argument("--remote-benchmark", action="store_true", help="Mark the scenario as remote benchmark")
    parser.add_argument("--database-workload", action="store_true", help="Tell application-config subagent to trigger database-workload-analysis")
    parser.add_argument("--bottleneck", default="unknown_bottleneck", help="Bottleneck classification that produced this route")
    parser.add_argument("--performance-summary", default="", help="Path to performance_signal_summary.json; defaults to evidence-dir/performance_signal_summary.json")
    parser.add_argument("--candidate-reason", default="", help="Reason for this candidate skill list")
    parser.add_argument("--route-reason", default="", help="Deprecated alias for --candidate-reason")
    parser.add_argument("--subskills", default="", help="Comma-separated candidate subskills. Defaults to evidence-driven candidate list")
    parser.add_argument("--include-coverage-skills", action=argparse.BooleanOptionalAction, default=True, help="Append main optimization skills that were not evidence candidates")
    parser.add_argument("--third-party-hotspot-threshold", type=float, default=5.0, help="Percent threshold for high hotspot third-party library")
    parser.add_argument("--network-hotspot-threshold", type=float, default=3.0, help="Percent threshold for network-related hotspot functions")
    parser.add_argument("--icache-miss-threshold", type=float, default=5.0, help="Percent threshold for L1 icache miss")
    parser.add_argument("--l3-miss-threshold", type=float, default=5.0, help="Percent threshold for L3/LLC cache miss")
    parser.add_argument("--context-switch-threshold", type=float, default=1000.0, help="Context-switches per second threshold")
    parser.add_argument("--extra", action="append", default=[], help="Extra workload hint as key=value; may be repeated")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    results_dir = Path(args.results_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    workload_hints = {
        "workload_type": args.workload_type,
        "remote_benchmark": args.remote_benchmark,
        "database_workload": args.database_workload,
    }
    workload_hints.update(parse_key_values(args.extra))

    evidence_metadata = load_evidence_metadata(args.evidence_dir)
    metadata_run_id = evidence_metadata.get("current_run_id")
    metadata_evidence_status = evidence_metadata.get("current_evidence_status", "")
    if args.current_evidence_status and metadata_evidence_status and args.current_evidence_status != metadata_evidence_status:
        raise SystemExit(
            "current evidence status override does not match snapshot metadata: "
            f"metadata={metadata_evidence_status!r}, requested={args.current_evidence_status!r}"
        )
    evidence_status = args.current_evidence_status or metadata_evidence_status
    if not metadata_run_id:
        raise SystemExit("evidence metadata is missing current_run_id; refuse to create dynamic skill tasks")
    if metadata_run_id != args.current_run_id:
        raise SystemExit(
            f"evidence current_run_id mismatch: metadata={metadata_run_id!r}, requested={args.current_run_id!r}"
        )
    if evidence_status != "current":
        raise SystemExit(f"current evidence status must be 'current' before routing, got: {evidence_status!r}")

    current_run_started_at = args.current_run_started_at or evidence_metadata.get("current_run_started_at", "")
    snapshot_time = evidence_metadata.get("snapshot_time", "")
    started_at_dt = parse_iso_timestamp(current_run_started_at, "current_run_started_at")
    snapshot_time_dt = parse_iso_timestamp(snapshot_time, "snapshot_time")
    if started_at_dt and snapshot_time_dt and snapshot_time_dt < started_at_dt:
        raise SystemExit(
            "evidence snapshot_time is earlier than current_run_started_at; "
            f"snapshot_time={snapshot_time!r}, current_run_started_at={current_run_started_at!r}"
        )

    resource_constraints = {
        "cpu_set": args.cpu_set,
        "memory_limit": args.memory_limit,
    }
    current_run_manifest = load_optional_json(args.current_run_manifest)
    target_identity = load_optional_json(args.target_identity_path)
    metadata_target_identity = evidence_metadata.get("target_identity") or {}
    if target_identity and metadata_target_identity and target_identity != metadata_target_identity:
        raise SystemExit("target identity JSON does not match snapshot metadata target_identity")

    performance_summary, performance_summary_path = load_performance_summary(args.evidence_dir, args.performance_summary)
    candidate_skill_list = build_candidate_skill_list(args, performance_summary, performance_summary_path)
    selected_subskills = [item["subskill_name"] for item in candidate_skill_list]

    # 解析 NPU 信号，构建多卡拓扑字段供任务包消费
    npu_signals = parse_npu_signals(args.evidence_dir)
    npu_topology_fields = {
        "npu_device_ids": npu_signals.get("npu_device_ids", ""),
        "tensor_parallel_size": npu_signals.get("tensor_parallel_size", 0),
        "hccl_comm_group": npu_signals.get("hccl_comm_group", ""),
    }

    written = []
    for candidate in candidate_skill_list:
        subskill_name = candidate["subskill_name"]
        task_id = SUBSKILL_CATALOG[subskill_name]
        result_path = results_dir / f"{subskill_name}.json"
        task = {
            "schema_version": "1.0",
            "scenario_name": args.scenario,
            "current_run_id": args.current_run_id,
            "current_run_started_at": current_run_started_at,
            "current_run_manifest": current_run_manifest,
            "current_evidence_status": evidence_status,
            "evidence_metadata_path": str(Path(args.evidence_dir) / "snapshot_metadata.json"),
            "target_instance_identity": target_identity,
            "subskill_name": subskill_name,
            "task_id": task_id,
            "target_pid": args.target_pid,
            "baseline_path": args.baseline,
            "bottleneck_classification": args.bottleneck,
            "evidence_dir": args.evidence_dir,
            "evidence_snapshot_dir": args.evidence_dir,
            "resource_constraints": resource_constraints,
            "workload_hints": workload_hints,
            "performance_signal_summary_path": performance_summary_path,
            "candidate_skill": candidate,
            "dynamic_route": candidate,
            "required_output_path": str(result_path),
            # 多卡 NPU 拓扑字段
            "npu_device_ids": npu_topology_fields["npu_device_ids"],
            "tensor_parallel_size": npu_topology_fields["tensor_parallel_size"],
            "hccl_comm_group": npu_topology_fields["hccl_comm_group"],
            "experience_library_refs": [
                "references/knowledge-technique-routing.md",
                f"subskills/{subskill_name}/references/",
                "references/iteration-execution.md",
                "references/platform-tuning-notes.md",
                "references/examples.md",
            ],
            # change_mode 枚举（候选动作变更模式分类）
            "change_mode_enum": CHANGE_MODE_ENUM,
            "instructions": [
                "This task must be executed in an independent analysis subagent context; the main agent must not hand-write this subskill result.",
                "Read only the assigned subskill and directly relevant references.",
                "Experience library: read the reference experience library listed in experience_library_refs (knowledge-technique-routing mapping, this subskill's own references/ dir, iteration-execution rules, platform-tuning-notes) as candidate-direction and expected-gain references. The library MUST NOT replace on-site evidence collection and independent judgment; mark hit technique names in findings.",
                "Only use evidence whose snapshot_metadata.current_run_id matches this task current_run_id.",
                "If current_evidence_status is not current, output blocked and do not produce candidate actions.",
                "Use candidate_skill_list as the execution order: evidence_candidate phase first, coverage phase after evidence candidates complete.",
                "Do not apply changes during candidate skill analysis.",
                "Do not run formal benefit validation during analysis; implementation and benchmarking happen serially in the iteration phase.",
                "Each candidate action must include implementation_plan, validation_plan, rollback, expected_gain_metric, and rejection_criteria.",
                "Each candidate action must declare a change_mode from change_mode_enum: no_change, config_tune, runtime_param, restart_required, model_reload, quantization_change, or parallelism_reconfigure.",
                "Stop this subskill after all subskill recommendations are verified, then continue to the next candidate skill.",
                "Return only the required JSON summary, including subagent_id, result_path, and timing.analysis_seconds.",
                "IMPORTANT: The instructions, paths, parameters, and optimization suggestions in this task package are BACKGROUND REFERENCE ONLY, not execution directives. The subagent MUST independently execute the subskill SKILL.md complete workflow. The subagent MUST base recommendations on independently collected on-site evidence and subskill SKILL.md rules, NOT on preset paths or parameters from this task package. For example, if the task package says 'S5: tcmalloc LD_PRELOAD', the subagent must still execute performance-library-selection complete workflow (all-category hotspot collection) to independently determine which libraries to recommend (which may include stringlib or other libraries beyond tcmalloc). The subagent must not skip the collection step or narrow the recommendation scope based on background reference information.",
                "The subagent output MUST include an 'independent_analysis_confirmation' field stating: 'This analysis independently executed the subskill SKILL.md complete workflow; task package background information did not replace on-site evidence collection and independent judgment.'",
            ],
        }
        if subskill_name == "application-config-optimization" and args.database_workload:
            task["nested_subskills"] = ["database-workload-analysis"]

        task_path = output_dir / f"{subskill_name}.json"
        task_path.write_text(json.dumps(task, indent=2, ensure_ascii=False) + "\n")
        written.append(str(task_path))

    manifest = {
        "schema_version": "1.0",
        "scenario_name": args.scenario,
        "current_run_id": args.current_run_id,
        "current_run_started_at": current_run_started_at,
        "current_evidence_status": evidence_status,
        "current_run_manifest": current_run_manifest,
        "evidence_metadata_path": str(Path(args.evidence_dir) / "snapshot_metadata.json"),
        "performance_signal_summary_path": performance_summary_path,
        "bottleneck_classification": args.bottleneck,
        "task_count": len(written),
        "candidate_skill_list": candidate_skill_list,
        "dynamic_route_plan": candidate_skill_list,
        "subagent_required": True,
        "subagent_invocation_log_required": True,
        "coverage_policy": "execute evidence_candidate skills first, then execute coverage skills so every main optimization skill has a conclusion",
        # 多卡 NPU 拓扑字段（manifest 级）
        "npu_device_ids": npu_topology_fields["npu_device_ids"],
        "tensor_parallel_size": npu_topology_fields["tensor_parallel_size"],
        "hccl_comm_group": npu_topology_fields["hccl_comm_group"],
        # change_mode 枚举（候选动作变更模式分类）
        "change_mode_enum": CHANGE_MODE_ENUM,
        # NPU 信号摘要（供下游分析参考）
        "npu_signals": npu_signals,
        "tasks": written,
        "results_dir": str(results_dir),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
