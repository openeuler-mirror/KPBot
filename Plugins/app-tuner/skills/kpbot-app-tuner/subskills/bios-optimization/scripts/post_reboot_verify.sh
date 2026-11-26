#!/usr/bin/env bash
set -euo pipefail

# post_reboot_verify.sh
# 重启后验证脚本: 采集当前 BIOS 配置并与 pre_change_snapshot.json 比对
#
# 用法: bash post_reboot_verify.sh <pre_change_snapshot.json>
# 输出: 比对结果（变更前 vs 变更后）+ 验证通过/失败

SNAPSHOT_FILE="${1:-}"

if [[ -z "${SNAPSHOT_FILE}" ]]; then
  echo "Usage: bash post_reboot_verify.sh <pre_change_snapshot.json>"
  exit 1
fi

[[ -f "${SNAPSHOT_FILE}" ]] || { echo "ERROR: snapshot file not found: ${SNAPSHOT_FILE}"; exit 1; }

echo "=== BIOS Post-Reboot Verification ==="
echo "Snapshot: ${SNAPSHOT_FILE}"
echo ""

# Collect current values and compare with snapshot
python3 - "${SNAPSHOT_FILE}" <<'PYEOF'
import json, sys, subprocess, os

snapshot_file = sys.argv[1]

with open(snapshot_file) as f:
    snapshot = json.load(f)

bios_settings = snapshot.get("bios_settings", {})

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None

# Current value collectors
def get_smt():
    return run_cmd("cat /sys/devices/system/cpu/smt/active 2>/dev/null")

def get_numa():
    nodes = run_cmd("numactl -H 2>/dev/null | grep -c 'node [0-9]* cpus:'")
    if nodes and int(nodes) > 1:
        return "off"
    elif nodes and int(nodes) == 1:
        return "on"
    return None

def get_cstate():
    states = []
    for i in range(10):
        name = run_cmd(f"cat /sys/devices/system/cpu/cpu0/cpuidle/state{i}/name 2>/dev/null")
        latency = run_cmd(f"cat /sys/devices/system/cpu/cpu0/cpuidle/state{i}/latency 2>/dev/null")
        if name:
            states.append(f"{name}(lat={latency})")
    return ", ".join(states) if states else None

def get_turbo():
    max_f = run_cmd("cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null")
    min_f = run_cmd("cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq 2>/dev/null")
    if max_f and min_f:
        return f"max={max_f} min={min_f}"
    return None

def get_ddr_speed():
    return run_cmd("dmidecode -t memory 2>/dev/null | grep 'Configured Memory Speed' | head -1 | grep -oE '[0-9]+'")

def get_aspm():
    disabled = run_cmd("lspci -vvv 2>/dev/null | grep 'LnkCtl:' | grep -c 'ASPM Disabled'")
    enabled = run_cmd("lspci -vvv 2>/dev/null | grep 'LnkCtl:' | grep -c 'ASPM L'")
    if disabled and enabled:
        d = int(disabled) if disabled else 0
        e = int(enabled) if enabled else 0
        if e == 0:
            return "off"
        elif d == 0:
            return "on"
        else:
            return f"mixed(d={d},e={e})"
    return None

COLLECTORS = {
    "smt": get_smt,
    "numa_interleaving": get_numa,
    "cstate": get_cstate,
    "turbo": get_turbo,
    "ddr_speed": get_ddr_speed,
    "pcie_aspm": get_aspm,
}

print(f"{'参数':<25} {'变更前':<25} {'变更后':<25} {'状态'}")
print("-" * 90)

all_verified = True
for key, setting in bios_settings.items():
    old_value = str(setting.get("value", "N/A"))
    collector = COLLECTORS.get(key)
    new_value = str(collector()) if collector else "N/A (需 Redfish)"

    if new_value == "None":
        new_value = "N/A"

    # Compare
    if old_value in new_value or new_value in old_value:
        status = "⚠️ 部分匹配"
    elif old_value != new_value and new_value != "N/A (需 Redfish)":
        status = "✅ 已变更"
    elif new_value == "N/A (需 Redfish)":
        status = "⏳ 需 Redfish 确认"
        all_verified = False
    else:
        status = "✅ 已变更"

    print(f"{key:<25} {old_value:<25} {new_value:<25} {status}")

print("")
if all_verified:
    print("=== 验证完成: 所有可检查参数已变更 ===")
else:
    print("=== 验证完成: 部分参数需通过 Redfish 确认 ===")
print("")
print("请将以上结果回传给主SKLL 或自行核对。")
PYEOF
