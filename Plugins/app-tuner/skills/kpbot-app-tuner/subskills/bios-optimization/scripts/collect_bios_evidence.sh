#!/usr/bin/env bash
set -euo pipefail

# collect_bios_evidence.sh
# BIOS 调优证据采集脚本,支持三种模式:
#   --supplement  补采模式: 基于已有 environment_backup_dir 补采缺失项 + 解析 Redfish JSON
#   --os-only     OS 侧推断模式: 无 Redfish,仅从 OS 侧推断 BIOS 配置
#   --check-only  检查模式: 只检查已有证据完整性,不采集
#
# 本脚本不重新实现 Redfish 采集,而是复用 backup_environment.sh 已采集的 Redfish JSON。
# 不接受 Redfish 凭据参数（--redfish-host/user/pass）,避免凭据泄露。

usage() {
  cat <<'EOF'
Usage:
  collect_bios_evidence.sh --supplement --output-dir <dir> --existing-backup-dir <dir>
  collect_bios_evidence.sh --os-only --output-dir <dir>
  collect_bios_evidence.sh --check-only --existing-backup-dir <dir>

Modes:
  --supplement  Parse existing Redfish JSON from backup dir + collect missing OS-side items
  --os-only     No Redfish; infer BIOS config from OS-side commands only
  --check-only  Check completeness of existing evidence, output missing list

Options:
  --output-dir <dir>              Output directory for collected evidence
  --existing-backup-dir <dir>     Existing environment_backup_dir (supplement/check mode)
  -h, --help                      Show help

Requirements:
  dmidecode  (root)    - BIOS vendor/version/model/memory speed
  cpupower   (root)    - C-State info
  lspci      (root)    - PCIe ASPM info
  lscpu                - CPU topology (SMT/Turbo inference)
  numactl              - NUMA topology (Node Interleaving inference)
  python3              - JSON parsing and manifest generation

Environment Variables (standalone mode, for backup_environment.sh):
  REDFISH_BMC_HOST    BMC host for Redfish access
  REDFISH_BMC_USER    BMC username
  REDFISH_BMC_PASS    BMC password (read by backup_environment.sh, not this script)

Examples:
  # Supplement: parse existing Redfish + collect missing OS-side items
  collect_bios_evidence.sh --supplement \
    --existing-backup-dir /path/to/environment_backup \
    --output-dir ./bios-evidence

  # OS-only: no Redfish, infer from OS
  collect_bios_evidence.sh --os-only --output-dir ./bios-evidence

  # Check only
  collect_bios_evidence.sh --check-only \
    --existing-backup-dir /path/to/environment_backup
EOF
}

MODE=""
OUTPUT_DIR=""
EXISTING_BACKUP_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --supplement) MODE="supplement"; shift ;;
    --os-only) MODE="os_only"; shift ;;
    --check-only) MODE="check_only"; shift ;;
    --output-dir) OUTPUT_DIR="${2:?}"; shift 2 ;;
    --existing-backup-dir) EXISTING_BACKUP_DIR="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "${MODE}" ]] || { echo "ERROR: mode is required" >&2; usage >&2; exit 1; }

if [[ "${MODE}" != "check_only" ]]; then
  [[ -n "${OUTPUT_DIR}" ]] || { echo "ERROR: --output-dir is required for ${MODE} mode" >&2; exit 1; }
fi

if [[ "${MODE}" == "supplement" || "${MODE}" == "check_only" ]]; then
  [[ -n "${EXISTING_BACKUP_DIR}" ]] || { echo "ERROR: --existing-backup-dir is required for ${MODE} mode" >&2; exit 1; }
  [[ -d "${EXISTING_BACKUP_DIR}" ]] || { echo "ERROR: backup dir not found: ${EXISTING_BACKUP_DIR}" >&2; exit 1; }
fi

# ── Helper functions ──────────────────────────────────────────

has_cmd() { command -v "$1" >/dev/null 2>&1; }

is_root() { [[ "$(id -u)" -eq 0 ]]; }

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

collect_cmd() {
  local label="$1"; shift
  local outfile="$1"; shift
  if "$@" >> "${outfile}" 2>/dev/null; then
    log "OK  ${label}"
  else
    log "SKIP ${label} (command failed or not available)"
  fi
}

# Check if a file exists in backup dir
backup_has() {
  [[ -n "${EXISTING_BACKUP_DIR}" && -f "${EXISTING_BACKUP_DIR}/$1" ]]
}

# Read Redfish BIOS attribute value with normalization (3-level fallback)
# Args: attr_names_csv  keyword_csv  json_file
# Returns: "value|matched_property|match_method" or empty
redfish_match_attr() {
  local attr_names="$1"
  local keywords="$1"
  local json_file="$1"
  # This function is implemented in Python below for JSON parsing
  :
}

# ── Redfish attribute normalization (Python) ──────────────────

normalize_redfish_attrs() {
  local json_file="$1"
  local output_json="$2"
  python3 - "$json_file" "$output_json" <<'PYEOF'
import json, sys, re

json_file = sys.argv[1]
output_json = sys.argv[2]

# Attribute mapping table (from bios-playbook.md Item 3)
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

    # Level 3: no match → will be handled by OS-side inference

with open(output_json, "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print(f"Normalized {len(results)} BIOS attributes from Redfish")
PYEOF
}

# ── OS-side BIOS inference ────────────────────────────────────

infer_smt() {
  local smt_file="$1"
  if [[ -f /sys/devices/system/cpu/smt/active ]]; then
    local val
    val=$(cat /sys/devices/system/cpu/smt/active 2>/dev/null || echo "unknown")
    printf 'smt=%s\nsource=os_inferred\n' "${val}" > "${smt_file}"
    log "OK  smt-inferred (active=${val})"
  else
    printf 'smt=unavailable\nsource=unavailable\n' > "${smt_file}"
    log "SKIP smt-inferred (no smt/active sysfs)"
  fi
}

infer_numa() {
  local numa_file="$1"
  if has_cmd numactl; then
    local node_count
    node_count=$(numactl -H 2>/dev/null | grep -c '^node [0-9]* cpus:' || echo "0")
    if [[ "${node_count}" -le 1 ]]; then
      printf 'numa_interleaving=on\nnode_count=%s\nsource=os_inferred\n' "${node_count}" > "${numa_file}"
    else
      printf 'numa_interleaving=off\nnode_count=%s\nsource=os_inferred\n' "${node_count}" > "${numa_file}"
    fi
    log "OK  numa-inferred (nodes=${node_count})"
  else
    printf 'numa_interleaving=unavailable\nsource=unavailable\n' > "${numa_file}"
    log "SKIP numa-inferred (numactl not found)"
  fi
}

infer_cstate() {
  local cstate_file="$1"
  if has_cmd cpupower && is_root; then
    {
      echo "=== cpupower idle-info ==="
      cpupower idle-info 2>/dev/null || echo "(cpupower failed)"
      echo ""
      echo "=== cpuidle states ==="
      for cpu_dir in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
        [[ -d "${cpu_dir}" ]] || continue
        local name disable latency
        name=$(cat "${cpu_dir}/name" 2>/dev/null || echo "?")
        disable=$(cat "${cpu_dir}/disable" 2>/dev/null || echo "?")
        latency=$(cat "${cpu_dir}/latency" 2>/dev/null || echo "?")
        echo "state=$(basename "${cpu_dir}") name=${name} disable=${disable} latency=${latency}"
      done
      printf 'source=os_inferred\n'
    } > "${cstate_file}"
    log "OK  cstate-inferred"
  else
    {
      echo "=== cpuidle states (sysfs only) ==="
      for cpu_dir in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
        [[ -d "${cpu_dir}" ]] || continue
        local name disable latency
        name=$(cat "${cpu_dir}/name" 2>/dev/null || echo "?")
        disable=$(cat "${cpu_dir}/disable" 2>/dev/null || echo "?")
        latency=$(cat "${cpu_dir}/latency" 2>/dev/null || echo "?")
        echo "state=$(basename "${cpu_dir}") name=${name} disable=${disable} latency=${latency}"
      done
      printf 'source=os_inferred\n'
    } > "${cstate_file}"
    log "OK  cstate-inferred (sysfs only, cpupower not available or non-root)"
  fi
}

infer_turbo() {
  local turbo_file="$1"
  if has_cmd lscpu; then
    {
      echo "=== lscpu frequency ==="
      lscpu | grep -iE 'MHz|GHz|max|min|boost' || true
      echo ""
      local max_freq base_freq
      max_freq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo "0")
      base_freq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq 2>/dev/null || echo "0")
      if [[ "${max_freq}" -gt 0 && "${base_freq}" -gt 0 && "${max_freq}" -ne "${base_freq}" ]]; then
        echo "turbo=inferred_on (max=${max_freq} min=${base_freq})"
      else
        echo "turbo=inferred_off_or_fixed (max=${max_freq} min=${base_freq})"
      fi
      printf 'source=os_inferred\n'
    } > "${turbo_file}"
    log "OK  turbo-inferred"
  else
    printf 'turbo=unavailable\nsource=unavailable\n' > "${turbo_file}"
    log "SKIP turbo-inferred (lscpu not found)"
  fi
}

infer_ddr_speed() {
  local ddr_file="$1"
  if has_cmd dmidecode && is_root; then
    {
      echo "=== dmidecode -t memory ==="
      dmidecode -t memory 2>/dev/null | grep -iE 'Speed|Type|Size|Locator' || true
      printf 'source=os_inferred\n'
    } > "${ddr_file}"
    log "OK  ddr-speed-inferred"
  else
    printf 'ddr_speed=unavailable\nsource=unavailable\n' > "${ddr_file}"
    log "SKIP ddr-speed-inferred (dmidecode not available or non-root)"
  fi
}

infer_pcie_aspm() {
  local aspm_file="$1"
  if has_cmd lspci; then
    {
      echo "=== lspci -vvv (ASPM relevant) ==="
      lspci -vvv 2>/dev/null | grep -iE 'ASPM|LnkCtl|LnkCap' || true
      printf 'source=os_inferred\n'
    } > "${aspm_file}"
    log "OK  pcie-aspm-inferred"
  else
    printf 'pcie_aspm=unavailable\nsource=unavailable\n' > "${aspm_file}"
    log "SKIP pcie-aspm-inferred (lspci not found)"
  fi
}

collect_bios_info() {
  local info_file="$1"
  {
    echo "=== dmidecode -t bios ==="
    if has_cmd dmidecode && is_root; then
      dmidecode -t bios 2>/dev/null || echo "(dmidecode failed)"
    else
      echo "(dmidecode not available or non-root)"
    fi
    echo ""
    echo "=== dmidecode -t system ==="
    if has_cmd dmidecode && is_root; then
      dmidecode -t system 2>/dev/null || echo "(dmidecode failed)"
    else
      echo "(dmidecode not available or non-root)"
    fi
    echo ""
    echo "=== sysfs DMI ==="
    for f in /sys/class/dmi/id/bios_vendor /sys/class/dmi/id/bios_version /sys/class/dmi/id/bios_date \
             /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name /sys/class/dmi/id/product_version \
             /sys/class/dmi/id/board_vendor /sys/class/dmi/id/board_name; do
      [[ -r "${f}" ]] && printf '%s=%s\n' "$(basename "${f}")" "$(cat "${f}")"
    done
  } > "${info_file}"
  log "OK  bios-info"
}

# ── Supplement mode ───────────────────────────────────────────

run_supplement() {
  log "=== Supplement mode: parsing existing Redfish + collecting missing OS-side items ==="
  mkdir -p "${OUTPUT_DIR}"

  local redfish_parsed=false

  # Parse existing Redfish BIOS JSON
  if backup_has "bios-redfish-bios.json"; then
    log "Found bios-redfish-bios.json, normalizing attributes..."
    normalize_redfish_attrs \
      "${EXISTING_BACKUP_DIR}/bios-redfish-bios.json" \
      "${OUTPUT_DIR}/bios-redfish-normalized.json"
    redfish_parsed=true
  else
    log "WARN: bios-redfish-bios.json not found in backup dir"
    echo '{}' > "${OUTPUT_DIR}/bios-redfish-normalized.json"
  fi

  # Copy existing evidence files
  for f in bios-info.txt bios-redfish.txt bios-redfish-bios.json bios-redfish-bios-settings.json; do
    if backup_has "${f}"; then
      cp "${EXISTING_BACKUP_DIR}/${f}" "${OUTPUT_DIR}/${f}"
      log "COPY ${f}"
    fi
  done

  # Also copy OS-side evidence from backup if available
  for f in hardware-cpu.txt hardware-memory.txt numa-topology.txt; do
    if backup_has "${f}"; then
      cp "${EXISTING_BACKUP_DIR}/${f}" "${OUTPUT_DIR}/${f}"
      log "COPY ${f}"
    fi
  done

  # Collect missing OS-side items
  collect_bios_info "${OUTPUT_DIR}/bios-info.txt"
  infer_cstate "${OUTPUT_DIR}/cstate_info.txt"
  infer_pcie_aspm "${OUTPUT_DIR}/pcie_aspm_info.txt"

  # Generate manifest
  generate_manifest "${OUTPUT_DIR}/bios_evidence_manifest.json" "${redfish_parsed}"
}

# ── OS-only mode ──────────────────────────────────────────────

run_os_only() {
  log "=== OS-only mode: inferring BIOS config from OS-side commands ==="
  mkdir -p "${OUTPUT_DIR}"

  collect_bios_info "${OUTPUT_DIR}/bios-info.txt"
  infer_smt "${OUTPUT_DIR}/smt_info.txt"
  infer_numa "${OUTPUT_DIR}/numa_info.txt"
  infer_cstate "${OUTPUT_DIR}/cstate_info.txt"
  infer_turbo "${OUTPUT_DIR}/turbo_info.txt"
  infer_ddr_speed "${OUTPUT_DIR}/ddr_speed_info.txt"
  infer_pcie_aspm "${OUTPUT_DIR}/pcie_aspm_info.txt"

  # No Redfish data
  echo '{}' > "${OUTPUT_DIR}/bios-redfish-normalized.json"

  # Generate manifest
  generate_manifest "${OUTPUT_DIR}/bios_evidence_manifest.json" false
}

# ── Check-only mode ───────────────────────────────────────────

run_check_only() {
  log "=== Check-only mode: verifying evidence completeness ==="
  local missing=()

  # Check Redfish
  if ! backup_has "bios-redfish-bios.json"; then
    missing+=("bios-redfish-bios.json (Redfish BIOS attributes)")
  fi

  # Check OS-side
  if ! backup_has "hardware-cpu.txt" && ! backup_has "bios-info.txt"; then
    missing+=("bios-info.txt or hardware-cpu.txt (BIOS vendor/version/CPU info)")
  fi
  if ! backup_has "hardware-memory.txt"; then
    missing+=("hardware-memory.txt (DDR speed)")
  fi
  if ! backup_has "numa-topology.txt"; then
    missing+=("numa-topology.txt (NUMA topology)")
  fi

  # C-State and PCIe ASPM are never in backup_environment.sh
  missing+=("cstate_info.txt (C-State — always needs supplement collection)")
  missing+=("pcie_aspm_info.txt (PCIe ASPM — always needs supplement collection)")

  echo ""
  echo "=== Evidence completeness check ==="
  echo "Backup dir: ${EXISTING_BACKUP_DIR}"
  echo ""

  local found=0 total=0
  for f in bios-redfish-bios.json bios-info.txt hardware-cpu.txt hardware-memory.txt numa-topology.txt; do
    total=$((total + 1))
    if backup_has "${f}"; then
      echo "  ✅ ${f}"
      found=$((found + 1))
    else
      echo "  ❌ ${f} (missing)"
    fi
  done
  echo "  ⚠️  cstate_info.txt (needs supplement)"
  echo "  ⚠️  pcie_aspm_info.txt (needs supplement)"

  echo ""
  echo "Found: ${found}/${total} in backup dir"
  echo "Missing items need supplement collection:"
  for m in "${missing[@]}"; do
    echo "  - ${m}"
  done

  if [[ ${found} -eq ${total} ]]; then
    echo ""
    echo "→ Run: collect_bios_evidence.sh --supplement --existing-backup-dir ${EXISTING_BACKUP_DIR} --output-dir <dir>"
  else
    echo ""
    echo "→ Some required evidence missing. Consider --os-only mode or re-run backup_environment.sh with BMC credentials."
  fi
}

# ── Manifest generation ───────────────────────────────────────

generate_manifest() {
  local manifest_file="$1"
  local redfish_parsed="$2"

  python3 - "${manifest_file}" "${redfish_parsed}" "${OUTPUT_DIR}" <<'PYEOF'
import json, os, sys

manifest_file = sys.argv[1]
redfish_parsed = sys.argv[2] == "true"
output_dir = sys.argv[3]

# Load normalized Redfish data
redfish_file = os.path.join(output_dir, "bios-redfish-normalized.json")
try:
    with open(redfish_file) as f:
        redfish_data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    redfish_data = {}

# Define all BIOS evidence items
# (key, label, os_files, os_inferable, parse_fn)
# parse_fn returns (value_str, extra_dict) from file content, or None if unparseable
import re

def parse_smt(text):
    m = re.search(r'smt=(\S+)', text)
    if m:
        v = m.group(1)
        return v, {}
    return None

def parse_numa(text):
    m = re.search(r'numa_interleaving=(\S+)', text)
    if m:
        nc = re.search(r'node_count=(\d+)', text)
        return m.group(1), {"node_count": int(nc.group(1)) if nc else None}
    return None

def parse_cstate(text):
    states = re.findall(r'name=(\S+)\s+disable=(\S+)\s+latency=(\S+)', text)
    if states:
        deepest = max(states, key=lambda s: int(s[2]))
        return deepest[0], {"latency": int(deepest[2]), "disable": deepest[1]}
    return None

def parse_turbo(text):
    m = re.search(r'turbo=(\S+)', text)
    if m:
        return m.group(1), {}
    return None

def parse_ddr(text):
    m = re.search(r'Configured Memory Speed:\s*(\d+)\s*MT/s', text)
    if m:
        spec_m = re.search(r'Speed:\s*(\d+)\s*MT/s', text)
        return m.group(1), {"spec_speed": spec_m.group(1) if spec_m else None}
    return None

def parse_aspm(text):
    disabled = text.count('ASPM Disabled')
    enabled = text.count('ASPM L0s') + text.count('ASPM L1')
    if disabled > 0 or enabled > 0:
        if disabled > 0 and enabled == 0:
            return "off", {}
        elif enabled > 0 and disabled == 0:
            return "on", {}
        else:
            return "mixed", {"disabled_count": disabled, "enabled_count": enabled}
    return None

EVIDENCE_ITEMS = [
    ("power_profile",        "Power Profile",          [], False, None),
    ("smt",                  "SMT / Hyper-Threading",  ["smt_info.txt"], True, parse_smt),
    ("numa_interleaving",    "NUMA / Node Interleaving", ["numa_info.txt"], True, parse_numa),
    ("cstate",               "C-State Limit",          ["cstate_info.txt"], True, parse_cstate),
    ("turbo",                "Turbo Boost",            ["turbo_info.txt"], True, parse_turbo),
    ("ddr_speed",            "DDR Speed",              ["ddr_speed_info.txt", "hardware-memory.txt"], True, parse_ddr),
    ("hardware_prefetcher",  "Hardware Prefetcher",    [], False, None),
    ("pcie_aspm",            "PCIe ASPM",              ["pcie_aspm_info.txt"], True, parse_aspm),
]

items = []
collected_count = 0
missing_count = 0
inferred_count = 0

for key, label, os_files, os_inferable, parse_fn in EVIDENCE_ITEMS:
    item = {"name": key, "label": label, "status": "missing", "source": "unavailable", "value": None, "confidence": "low"}

    # Check Redfish first
    if key in redfish_data:
        rd = redfish_data[key]
        item["status"] = "collected"
        item["source"] = rd.get("source", "redfish")
        item["confidence"] = rd.get("confidence", "high")
        item["value"] = rd.get("value")
        item["matched_property_name"] = rd.get("matched_property_name", "")
        item["match_method"] = rd.get("match_method", "exact")
        collected_count += 1
    elif os_inferable:
        # Check OS-side files and parse value
        for f in os_files:
            fpath = os.path.join(output_dir, f)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                item["status"] = "inferred"
                item["source"] = "os_inferred"
                item["confidence"] = "medium"
                # Parse actual value from file content
                if parse_fn:
                    try:
                        with open(fpath) as fh:
                            content = fh.read()
                        parsed = parse_fn(content)
                        if parsed:
                            item["value"] = parsed[0]
                            if parsed[1]:
                                item.update(parsed[1])
                    except Exception:
                        pass
                inferred_count += 1
                break
        else:
            item["status"] = "missing"
            item["source"] = "unavailable"
            missing_count += 1
    else:
        # Not in Redfish and not OS-inferable → missing
        item["status"] = "missing"
        item["source"] = "unavailable"
        missing_count += 1

    items.append(item)

# Determine overall quality
if redfish_parsed and collected_count >= 5:
    quality = "redfish"
elif inferred_count >= 4:
    quality = "os_inferred"
elif collected_count + inferred_count >= 3:
    quality = "mixed"
else:
    quality = "insufficient"

completeness = "complete" if missing_count == 0 else ("partial" if missing_count <= 3 else "insufficient")

manifest = {
    "mode": "supplement" if redfish_parsed else "os_only",
    "collected_at": __import__("datetime").datetime.now().isoformat(),
    "evidence_source_quality": quality,
    "evidence_items": items,
    "completeness": completeness,
    "missing_required": [i["name"] for i in items if i["status"] == "missing"],
    "unable_to_determine": [i["name"] for i in items if i["status"] == "missing"],
}

with open(manifest_file, "w") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)

print(f"Manifest written: {manifest_file}")
print(f"  Quality: {quality}")
print(f"  Completeness: {completeness}")
print(f"  Collected (Redfish): {collected_count}")
print(f"  Inferred (OS-side): {inferred_count}")
print(f"  Missing: {missing_count}")
if missing_count > 0:
    print(f"  Missing items: {', '.join(manifest['missing_required'])}")
PYEOF
}

# ── Main ──────────────────────────────────────────────────────

log "collect_bios_evidence.sh starting (mode=${MODE})"

case "${MODE}" in
  supplement)  run_supplement ;;
  os_only)     run_os_only ;;
  check_only)  run_check_only ;;
esac

log "Done."
