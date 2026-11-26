#!/bin/bash
# redfish_attrs — Redfish BIOS 属性归一化（规则与 app-tuner collect_bios_evidence.sh 一致）
# 三级匹配：精确属性名 → 关键词模糊（唯一=high / 多候选=ambiguous+medium）→ 未匹配交由 OS 推断
set -euo pipefail

print_help() {
    cat <<'EOF'
redfish_attrs — Normalize Redfish BIOS attributes

Usage:
  redfish_attrs.sh -f <bios.json> [-o <out.json>]

Input JSON: Redfish /Bios 或 /Bios/Settings 响应（含 Attributes 键或直接为属性表）
Output:     stdout 输出归一化 JSON（无 -o 时）；-o 时写文件并在 stderr 报告状态

匹配规则（与 bios-optimization collect_bios_evidence.sh 保持一致）:
  power_profile / smt / cstate / turbo / numa_interleaving / ddr_speed /
  hardware_prefetcher / pcie_aspm
EOF
    exit 0
}

JSON_FILE=""
OUT_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        -f) JSON_FILE="$2"; shift 2 ;;
        -o) OUT_FILE="$2"; shift 2 ;;
        --help) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

if [ -z "$JSON_FILE" ] || [ ! -r "$JSON_FILE" ]; then
    echo "ERROR: -f <bios.json> 必填且需可读" >&2
    exit 3
fi

python3 - "$JSON_FILE" "${OUT_FILE:-}" <<'PYEOF'
import json, sys

json_file = sys.argv[1]
output_file = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

# Attribute mapping table（与 collect_bios_evidence.sh bios-playbook Item 3 一致）
ATTR_MAP = {
    "power_profile": {
        "names": ["WorkloadProfile", "SystemProfile", "PowerProfile"],
        "keywords": ["workload", "profile", "power_profile"],
    },
    "smt": {
        "names": ["ProcHyperthreading", "LogicalProc", "HyperThreading"],
        "keywords": ["hyperthread", "smt", "logical_proc"],
    },
    "cstate": {
        "names": ["ProcessorCstate", "CStateCtl", "CstateEnable"],
        "keywords": ["cstate", "c_state"],
    },
    "turbo": {
        "names": ["ProcTurbo", "TurboMode", "TurboBoost"],
        "keywords": ["turbo"],
    },
    "numa_interleaving": {
        "names": ["NumaGroupSizeOpt", "NodeInterleave", "NodeInterleaving"],
        "keywords": ["numa", "interleav"],
    },
    "ddr_speed": {
        "names": ["DDRSpeed", "MemFreq", "MemorySpeed"],
        "keywords": ["ddr", "memory_speed", "mem_freq"],
    },
    "hardware_prefetcher": {
        "names": ["HWPrefetcher", "Prefetcher", "HwPrefetch"],
        "keywords": ["prefetch"],
    },
    "pcie_aspm": {
        "names": ["PcieAspmSupport", "AspmControl", "PCIeASPM"],
        "keywords": ["aspm", "pcie_power"],
    },
}

try:
    with open(json_file) as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}

attrs = data.get("Attributes", data)

results = {}
for param, spec in ATTR_MAP.items():
    matched = False

    # Level 1: exact match
    for name in spec["names"]:
        if name in attrs:
            results[param] = {
                "value": attrs[name],
                "matched_property_name": name,
                "match_method": "exact",
                "source": "redfish",
                "confidence": "high",
            }
            matched = True
            break

    if matched:
        continue

    # Level 2: keyword fuzzy match
    attr_keys_lower = {k.lower(): k for k in attrs.keys()}
    candidates = []
    for kw in spec["keywords"]:
        for lower_key, orig_key in attr_keys_lower.items():
            if kw in lower_key:
                candidates.append(orig_key)

    if len(candidates) == 1:
        results[param] = {
            "value": attrs[candidates[0]],
            "matched_property_name": candidates[0],
            "match_method": "keyword",
            "source": "redfish",
            "confidence": "high",
        }
    elif len(candidates) > 1:
        results[param] = {
            "value": attrs[candidates[0]],
            "matched_property_name": candidates[0],
            "match_method": "ambiguous",
            "source": "redfish",
            "confidence": "medium",
            "ambiguous_candidates": candidates,
        }

    # Level 3: no match → OS 侧推断处理

if output_file:
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Normalized {len(results)} BIOS attributes from Redfish -> {output_file}", file=sys.stderr)
else:
    print(json.dumps(results, indent=2, ensure_ascii=False))
PYEOF
