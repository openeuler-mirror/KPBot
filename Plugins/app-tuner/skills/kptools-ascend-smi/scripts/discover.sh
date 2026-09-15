#!/bin/bash
# discover.sh — 检查昇腾 NPU 环境与 npu-smi 工具可用性
# 输出: <status> 到 stdout; 诊断与安装指引到 stderr; 退出码 0=READY 其余=不可用
set -uo pipefail

FIRMWARE_URL="https://www.hiascend.com/hdk/firmware"

help() {
    cat <<'EOF'
ascend-smi discover — Check Ascend NPU hardware, driver and npu-smi availability

Usage:
  discover.sh [options]

Options:
  --tool <path>   Explicit npu-smi path (skip PATH probing)
  --path          Print npu-smi binary path only (exit 1 if not found)
  --help          Show this help message

Status codes (stdout first line):
  READY              All checks passed
  NO_ASCEND_HARDWARE No Ascend PCIe device found (lspci -d 19e5:)
  DRIVER_MISSING     Driver not loaded (no /dev/davinci* nodes)
  TOOL_MISSING       npu-smi binary not found
  DRIVER_LOADING     Driver loading in progress (wait 180s after boot)
  DRIVER_ERROR       npu-smi info failed unexpectedly
  PERMISSION_DENIED  User lacks permission to query devices
EOF
}

TOOL_OVERRIDE=""
PRINT_PATH=false
while [ $# -gt 0 ]; do
    case "$1" in
        --tool)
            if [ $# -lt 2 ]; then
                echo "--tool requires a path argument" >&2
                exit 3
            fi
            TOOL_OVERRIDE="$2"; shift 2 ;;
        --path) PRINT_PATH=true; shift ;;
        --help) help; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 3 ;;
    esac
done

fail() { # $1=status $2..=stderr guidance
    local status="$1"; shift
    if [ "$PRINT_PATH" = true ]; then exit 1; fi
    echo "$status"
    for line in "$@"; do echo "  $line" >&2; done
    exit 1
}

# 1. 定位 npu-smi（PATH → /usr/local/sbin → /usr/local/bin）
NPU_SMI_BIN=""
if [ -n "$TOOL_OVERRIDE" ]; then
    if [ -x "$TOOL_OVERRIDE" ]; then
        NPU_SMI_BIN="$TOOL_OVERRIDE"
    fi
else
    if command -v npu-smi &>/dev/null; then
        NPU_SMI_BIN="$(command -v npu-smi)"
    elif [ -x /usr/local/sbin/npu-smi ]; then
        NPU_SMI_BIN=/usr/local/sbin/npu-smi
    elif [ -x /usr/local/bin/npu-smi ]; then
        NPU_SMI_BIN=/usr/local/bin/npu-smi
    fi
fi

if [ "$PRINT_PATH" = true ]; then
    [ -n "$NPU_SMI_BIN" ] && echo "$NPU_SMI_BIN" || exit 1
    exit 0
fi

# 2. 工具存在性（先于硬件检查：无工具时无法进一步诊断）
if [ -z "$NPU_SMI_BIN" ]; then
    # 尝试给出更精确的硬件线索
    if command -v lspci &>/dev/null && ! lspci -d 19e5: 2>/dev/null | grep -q .; then
        fail TOOL_MISSING \
            "npu-smi not found in PATH, /usr/local/sbin, /usr/local/bin." \
            "No Ascend PCIe device (lspci -d 19e5:) either — this host likely has no Ascend NPU."
    fi
    fail TOOL_MISSING \
        "npu-smi not found. It ships with the Ascend NPU driver (not CANN)." \
        "Install driver from: ${FIRMWARE_URL}" \
        "Standard locations: /usr/local/sbin/npu-smi, /usr/local/bin/npu-smi"
fi

echo "npu-smi: ${NPU_SMI_BIN}"

# 3. 硬件存在性（设备节点）
if ! ls /dev/davinci* &>/dev/null; then
    if command -v lspci &>/dev/null && lspci -d 19e5: 2>/dev/null | grep -q .; then
        fail DRIVER_MISSING \
            "Ascend PCIe device present but /dev/davinci* nodes missing." \
            "Driver not installed or not loaded. Check: lsmod | grep -i davinci" \
            "Reinstall driver from: ${FIRMWARE_URL}"
    else
        fail DRIVER_MISSING \
            "No /dev/davinci* nodes and no Ascend PCIe device (lspci -d 19e5:)." \
            "Install the Ascend NPU driver: ${FIRMWARE_URL}"
    fi
fi

# 4. 就绪度（实际执行一次查询）
probe_output="$("$NPU_SMI_BIN" info 2>&1)"
probe_rc=$?
if [ $probe_rc -ne 0 ]; then
    if echo "$probe_output" | grep -qiE "driver.*not.*(ready|load|install)|davinci.*not|load.*fail|initialize"; then
        fail DRIVER_LOADING \
            "Driver is loading. Driver load can take up to 150s after OS boot." \
            "Wait 180 seconds and retry."
    fi
    if echo "$probe_output" | grep -qiE "permission|denied|not authorized"; then
        fail PERMISSION_DENIED \
            "Query denied for current user." \
            "Add user to the driver run group (typically HwHiAiUser):" \
            "  sudo usermod -aG HwHiAiUser \$USER  # then re-login" \
            "Or run as root."
    fi
    fail DRIVER_ERROR \
        "npu-smi info exited rc=${probe_rc}:" \
        "  $(echo "$probe_output" | head -3 | tr '\n' ' ')" \
        "Check driver health: dmesg | grep -iE 'davinci|npu' | tail -20"
fi

# 5. 汇总（设备数优先取 info -l 的 NPU ID 行，兼容稀疏设备号；兜底 Total Count，大小写不敏感）
device_count=""
if l_out="$("$NPU_SMI_BIN" info -l 2>/dev/null)"; then
    device_count=$(echo "$l_out" | grep -cE 'NPU ID[[:space:]]*:[[:space:]]*[0-9]+' | tr -d ' ')
    [ "$device_count" -eq 0 ] && device_count=""
fi
if [ -z "$device_count" ]; then
    device_count=$(echo "$probe_output" | grep -oiE 'Total[[:space:]]+count[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$')
fi
health_summary=$(echo "$probe_output" | awk '/^\|/ && /OK|Alarm|Warning|Fault|Unknown/' | wc -l | tr -d ' ')
echo "status: READY"
echo "devices: ${device_count:-unknown}"
echo "rows_with_health_flag: ${health_summary}"
echo "exit_code: 0"
exit 0
