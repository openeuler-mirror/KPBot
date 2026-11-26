#!/bin/bash
# discover.sh — 检查昇腾 NPU 环境与 hccn_tool 工具可用性
# 输出: <status> 到 stdout; 诊断与安装指引到 stderr; 退出码 0=READY 其余=不可用
set -uo pipefail

FIRMWARE_URL="https://www.hiascend.com/hdk/firmware"
CANDIDATE_PATHS=(/usr/bin/hccn_tool /usr/local/Ascend/driver/tools/hccn_tool)

help() {
    cat <<'EOF'
ascend-hccn discover — Check Ascend NPU environment and hccn_tool availability

Usage:
  discover.sh [options]

Options:
  --tool <path>   Explicit hccn_tool path (skip standard path probing)
  --path          Print hccn_tool binary path only (exit 1 if not found)
  --help          Show this help message

Status codes (stdout first line):
  READY              All checks passed
  DRIVER_MISSING     Driver not loaded (no /dev/davinci* nodes)
  TOOL_MISSING       hccn_tool binary not found (in container: mount host binary)
  DRIVER_LOADING     Driver loading in progress (wait 180s after boot)
  DRIVER_ERROR       hccn_tool probe query failed unexpectedly
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

in_container() {
    [ -r /proc/1/cgroup ] && grep -qE 'docker|kubepods|containerd|lxc' /proc/1/cgroup 2>/dev/null
    return $?
}

fail() { # $1=status 其余=stderr guidance
    local status="$1"; shift
    if [ "$PRINT_PATH" = true ]; then exit 1; fi
    echo "$status"
    for line in "$@"; do echo "  $line" >&2; done
    exit 1
}

# 1. 定位 hccn_tool（PATH → /usr/bin → /usr/local/Ascend/driver/tools）
HCCN_BIN=""
if [ -n "$TOOL_OVERRIDE" ]; then
    [ -x "$TOOL_OVERRIDE" ] && HCCN_BIN="$TOOL_OVERRIDE"
else
    if command -v hccn_tool &>/dev/null; then
        HCCN_BIN="$(command -v hccn_tool)"
    else
        for p in "${CANDIDATE_PATHS[@]}"; do
            [ -x "$p" ] && { HCCN_BIN="$p"; break; }
        done
    fi
fi

if [ "$PRINT_PATH" = true ]; then
    [ -n "$HCCN_BIN" ] && echo "$HCCN_BIN" || exit 1
    exit 0
fi

# 2. 工具存在性
if [ -z "$HCCN_BIN" ]; then
    extra=""
    if in_container; then
        extra="Running inside a container: mount the host binary /usr/bin/hccn_tool"
    fi
    fail TOOL_MISSING \
        "hccn_tool not found (PATH, /usr/bin, /usr/local/Ascend/driver/tools)." \
        "hccn_tool ships with the Ascend NPU driver (not CANN)." \
        "${extra}" \
        "Install driver from: ${FIRMWARE_URL}"
fi

echo "hccn_tool: ${HCCN_BIN}"

# 3. 驱动存在性（设备节点）
if ! ls /dev/davinci* &>/dev/null; then
    fail DRIVER_MISSING \
        "No /dev/davinci* nodes — driver not installed or not loaded." \
        "Check: lsmod | grep -i davinci ; install driver from: ${FIRMWARE_URL}"
fi

# 4. 就绪度（轮询芯片 0~MAX_CHIP 取首个成功；容器分卡场景芯片 0 可能不可见）
MAX_CHIP=${HCCN_MAX_CHIP:-7}
if ! [[ "$MAX_CHIP" =~ ^[0-9]+$ ]] || [ "$MAX_CHIP" -gt 63 ]; then
    echo "discover: HCCN_MAX_CHIP must be an integer 0~63 (got: ${MAX_CHIP})" >&2
    exit 3
fi
probe_output=""
first_error=""
probe_rc=1
PROBE_CHIP=""
for c in $(seq 0 "$MAX_CHIP"); do
    probe_output="$("$HCCN_BIN" -i "$c" -ip -g 2>&1)"
    probe_rc=$?
    if [ $probe_rc -eq 0 ]; then
        PROBE_CHIP="$c"
        break
    fi
    [ -z "$first_error" ] && first_error="$probe_output"
done
if [ $probe_rc -ne 0 ]; then
    probe_output="$first_error"
    if echo "$probe_output" | grep -qiE "permission|denied|not authorized"; then
        fail PERMISSION_DENIED \
            "Query denied for current user." \
            "Add user to the driver run group (typically HwHiAiUser):" \
            "  sudo usermod -aG HwHiAiUser \$USER  # then re-login"
    fi
    if echo "$probe_output" | grep -qiE "driver.*not.*(ready|load|install)|davinci.*not|load.*fail|initialize|device.*not.*exist"; then
        fail DRIVER_LOADING \
            "Driver or device not ready. Driver load can take up to 150s after OS boot." \
            "Wait 180 seconds and retry. Also verify chip Phy-ID: 910B uses 0-7."
    fi
    fail DRIVER_ERROR \
        "hccn_tool -i <0-${MAX_CHIP}> -ip -g all failed, first error (chip 0) rc=${probe_rc}:" \
        "  $(echo "$probe_output" | head -3 | tr '\n' ' ')" \
        "Check dmesg | grep -iE 'davinci|npu|hns' | tail -20"
fi

# 5. 芯片枚举（成功芯片数）
READY_CHIPS=0
for c in $(seq 0 "$MAX_CHIP"); do
    if "$HCCN_BIN" -i "$c" -link -g &>/dev/null; then
        READY_CHIPS=$((READY_CHIPS + 1))
    fi
done

echo "status: READY"
echo "chips_ready (0-${MAX_CHIP} probed): ${READY_CHIPS}"
echo "probe: chip ${PROBE_CHIP} IP query ok"
exit 0
