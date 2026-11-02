#!/bin/bash
# bios-collector collect — BIOS/固件配置只读采集统一入口
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODES=()
BMC_HOST="${REDFISH_HOST:-}"
BMC_USER="${REDFISH_USER:-}"
BMC_PASS="${REDFISH_PASS:-}"
CURL_TIMEOUT=5

print_help() {
    cat <<'EOF'
kptools-bios-collector collect — Collect BIOS/firmware configuration (read-only)

Modes:
  --dmi          dmidecode bios/system/baseboard + sysfs DMI
  --redfish      BMC Redfish GET（/Systems → /Bios → /Bios/Settings）
  --smt          SMT 状态（Redfish → sysfs 推断）
  --cstate       C-State（cpupower idle-info → sysfs 降级）
  --turbo        Turbo（Redfish → lscpu + cpufreq 推断）
  --numa         NUMA Interleaving（numactl -H 推断）
  --ddr          DDR Speed（dmidecode -t memory）
  --aspm         PCIe ASPM（Redfish → lspci -vvv）
  --prefetcher   Hardware Prefetcher（仅 Redfish，无则 unavailable）
  --all          依次执行全部模式（无凭据/无 root 的模式记 SKIP 继续）

Redfish options:
  --bmc-host <host[:port]|URL>   或环境变量 REDFISH_HOST
  -u <user>                      或环境变量 REDFISH_USER
  -p <pass>                      或环境变量 REDFISH_PASS
                                 （host 支持 http:// https:// 前缀，默认 https://）

Notes:
  stdout: 各模式原始输出（=== 分节）
  stderr: SKIP/降级/错误原因
  exit 0: 成功（含 SKIP）；3: 参数错误；1: 单模式失败
  只读约束：仅 Redfish GET，禁止 PATCH/PUT；凭据不写入输出
EOF
    exit 0
}

# 值参数不能作为最后一个参数出现
if [ $# -gt 0 ]; then
    case "${!#}" in
        --bmc-host|-u|-p) echo "ERROR: option ${!#} requires a value" >&2; exit 3 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --dmi|--redfish|--smt|--cstate|--turbo|--numa|--ddr|--aspm|--prefetcher|--all)
            MODES+=("$1"); shift ;;
        --bmc-host) BMC_HOST="$2"; shift 2 ;;
        -u) BMC_USER="$2"; shift 2 ;;
        -p) BMC_PASS="$2"; shift 2 ;;
        --help) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

if [ ${#MODES[@]} -eq 0 ]; then
    echo "ERROR: 至少需要一个模式参数（见 --help）" >&2
    exit 3
fi

IS_ROOT=0
[ "$(id -u)" = "0" ] && IS_ROOT=1

section() { echo ""; echo "=== $1 ==="; }

# ── Redfish GET 底层 ─────────────────────────────────────────
redfish_get() {
    # $1=url-path  $2=outfile  stdout=HTTP code（000=连接失败）
    local path="$1" outfile="$2"
    local base="$BMC_HOST"
    case "$base" in
        http://*|https://*) : ;;
        *) base="https://$base" ;;
    esac
    local code
    code=$(curl -sk --max-time "$CURL_TIMEOUT" -u "${BMC_USER}:${BMC_PASS}" \
        -o "$outfile" -w '%{http_code}' "${base}${path}" 2>/dev/null) || code="000"
    echo "$code"
}

redfish_check_code() {
    # $1=step 描述  $2=code  非成功时分类报因并返回 1
    local step="$1" code="$2"
    case "$code" in
        200) return 0 ;;
        000) echo "ERROR: BMC 不可达（网络不通或 BMC 服务未启动）: $BMC_HOST" >&2; return 1 ;;
        401) echo "ERROR: BMC 凭据错误或权限不足（HTTP 401）" >&2; return 1 ;;
        *) echo "ERROR: Redfish 采集失败（HTTP $code）: $step" >&2; return 1 ;;
    esac
}

redfish_fetch_bios() {
    # $1=tmp_prefix；拉取 /redfish/v1 → /Systems → member → /Bios（+ /Bios/Settings）
    # 产物写入 $1.bios.json / $1.bios_settings.json（Settings 不可读时不生成）；stdout 输出 member 路径
    local tmp_prefix="$1" code member

    code=$(redfish_get "/redfish/v1" "$tmp_prefix.root.json")
    redfish_check_code "/redfish/v1" "$code" || return 1

    code=$(redfish_get "/redfish/v1/Systems" "$tmp_prefix.systems.json")
    redfish_check_code "/Systems" "$code" || return 1

    member=$(python3 -c "
import json,sys
try:
    d=json.load(open('$tmp_prefix.systems.json'))
    print(d['Members'][0]['@odata.id'])
except Exception:
    print('')
" 2>/dev/null)
    if [ -z "$member" ]; then
        echo "ERROR: 未发现 Redfish System 成员" >&2
        return 1
    fi

    code=$(redfish_get "$member" "$tmp_prefix.system.json")
    redfish_check_code "$member" "$code" || return 1

    code=$(redfish_get "$member/Bios" "$tmp_prefix.bios.json")
    redfish_check_code "$member/Bios" "$code" || return 1

    code=$(redfish_get "$member/Bios/Settings" "$tmp_prefix.bios_settings.json")
    if [ "$code" != "200" ]; then
        echo "NOTICE: /Bios/Settings 不可读（HTTP $code），仅 /Bios" >&2
        rm -f "$tmp_prefix.bios_settings.json"
    fi
    echo "$member"
}

# ── 模式实现 ─────────────────────────────────────────────────
mode_dmi() {
    section "dmidecode -t bios"
    if command -v dmidecode &>/dev/null && [ "$IS_ROOT" = "1" ]; then
        dmidecode -t bios 2>/dev/null || echo "(dmidecode failed)"
    else
        echo "(dmidecode not available or non-root)"
    fi
    section "dmidecode -t system"
    if command -v dmidecode &>/dev/null && [ "$IS_ROOT" = "1" ]; then
        dmidecode -t system 2>/dev/null || echo "(dmidecode failed)"
    else
        echo "(dmidecode not available or non-root)"
    fi
    section "sysfs DMI"
    for f in /sys/class/dmi/id/bios_vendor /sys/class/dmi/id/bios_version /sys/class/dmi/id/bios_date \
             /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name /sys/class/dmi/id/product_version \
             /sys/class/dmi/id/board_vendor /sys/class/dmi/id/board_name; do
        # 不能用 "[ -r ] && printf"：最后一个文件不可读时 for 循环返回 1，
        # set -e 会在函数收尾处中止整个脚本
        if [ -r "$f" ]; then
            printf '%s=%s\n' "$(basename "$f")" "$(cat "$f")"
        fi
    done
}

mode_smt() {
    section "SMT"
    if [ -f /sys/devices/system/cpu/smt/active ]; then
        echo "smt=$(cat /sys/devices/system/cpu/smt/active 2>/dev/null || echo unknown)"
        echo "source=os_inferred"
    else
        echo "smt=unavailable"
        echo "source=unavailable"
        echo "NOTICE: 无 smt/active sysfs，且未提供 Redfish 归一化输入" >&2
    fi
}

mode_cstate() {
    section "C-State"
    if command -v cpupower &>/dev/null && [ "$IS_ROOT" = "1" ]; then
        echo "=== cpupower idle-info ==="
        cpupower idle-info 2>/dev/null || echo "(cpupower failed)"
    else
        echo "=== cpuidle states (sysfs only) ==="
    fi
    echo ""
    echo "=== cpuidle states ==="
    local cpu_dir
    for cpu_dir in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
        [ -d "$cpu_dir" ] || continue
        echo "state=$(basename "$cpu_dir") name=$(cat "$cpu_dir/name" 2>/dev/null || echo '?') disable=$(cat "$cpu_dir/disable" 2>/dev/null || echo '?') latency=$(cat "$cpu_dir/latency" 2>/dev/null || echo '?')"
    done
    echo "source=os_inferred"
    if ! command -v cpupower &>/dev/null || [ "$IS_ROOT" != "1" ]; then
        echo "NOTICE: cpupower 不可用或非 root，仅 sysfs（sysfs only）" >&2
    fi
}

mode_turbo() {
    section "Turbo"
    if command -v lscpu &>/dev/null; then
        echo "=== lscpu frequency ==="
        lscpu 2>/dev/null | grep -iE 'MHz|GHz|max|min|boost' || true
        echo ""
        local max_freq base_freq
        max_freq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo "0")
        base_freq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq 2>/dev/null || echo "0")
        if [ "$max_freq" -gt 0 ] && [ "$base_freq" -gt 0 ] && [ "$max_freq" -ne "$base_freq" ]; then
            echo "turbo=inferred_on (max=${max_freq} min=${base_freq})"
        else
            echo "turbo=inferred_off_or_fixed (max=${max_freq} min=${base_freq})"
        fi
        echo "source=os_inferred"
    else
        echo "turbo=unavailable"
        echo "source=unavailable"
    fi
}

mode_numa() {
    section "NUMA Interleaving"
    if command -v numactl &>/dev/null; then
        local node_count
        node_count=$(numactl -H 2>/dev/null | grep -c '^node [0-9]* cpus:' || echo "0")
        if [ "${node_count:-0}" -le 1 ]; then
            echo "numa_interleaving=on (single node)"
        else
            echo "numa_interleaving=off"
        fi
        echo "node_count=${node_count}"
        echo "source=os_inferred"
    else
        echo "numa_interleaving=unavailable"
        echo "source=unavailable"
    fi
}

mode_ddr() {
    section "DDR Speed"
    if command -v dmidecode &>/dev/null && [ "$IS_ROOT" = "1" ]; then
        echo "=== dmidecode -t memory ==="
        dmidecode -t memory 2>/dev/null | grep -iE 'Speed|Type|Size|Locator' || true
        echo "source=os_inferred"
    else
        echo "ddr_speed=unavailable"
        echo "source=unavailable"
        echo "NOTICE: dmidecode 不可用或非 root" >&2
    fi
}

mode_aspm() {
    section "PCIe ASPM"
    if command -v lspci &>/dev/null && [ "$IS_ROOT" = "1" ]; then
        lspci -vvv 2>/dev/null | grep -iE 'ASPM|LnkCtl|LnkCap' || true
        echo "source=os_inferred"
    else
        echo "pcie_aspm=unavailable"
        echo "source=unavailable"
        echo "NOTICE: lspci 不可用或非 root（lspci -vvv 需 root 读 LnkCtl）" >&2
    fi
}

mode_prefetcher() {
    section "Hardware Prefetcher"
    if [ -z "$BMC_HOST" ] || [ -z "$BMC_USER" ]; then
        echo "hardware_prefetcher=unavailable"
        echo "source=unavailable"
        echo "NOTICE: Prefetcher 仅 Redfish BIOS Settings 可查；未提供 BMC 地址或凭据（--bmc-host/-u/-p 或 REDFISH_HOST/REDFISH_USER/REDFISH_PASS）" >&2
        return 0
    fi
    local tmpd pf_src pf_out
    tmpd=$(mktemp -d /tmp/bios_pf.XXXXXX)
    if ! redfish_fetch_bios "$tmpd/redfish" >/dev/null; then
        echo "hardware_prefetcher=unavailable"
        echo "source=unavailable"
        rc_overall=1
        rm -rf "$tmpd"
        return 0
    fi
    # /Bios/Settings（含待应用配置）优先，其次 /Bios（生效配置）
    if [ -f "$tmpd/redfish.bios_settings.json" ]; then
        pf_src="$tmpd/redfish.bios_settings.json"
    else
        pf_src="$tmpd/redfish.bios.json"
    fi
    pf_out=$(bash "$SCRIPT_DIR/redfish_attrs.sh" -f "$pf_src" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
pf = d.get("hardware_prefetcher")
if pf:
    print("hardware_prefetcher=%s" % (pf.get("value"),))
    print("matched_property_name=%s (%s/%s)" % (pf.get("matched_property_name"), pf.get("match_method"), pf.get("confidence")))
    print("source=%s" % (pf.get("source"),))
')
    if [ -n "$pf_out" ]; then
        echo "$pf_out"
    else
        echo "hardware_prefetcher=unavailable"
        echo "source=redfish_no_match"
        echo "NOTICE: BIOS Attributes 未匹配到 prefetcher 关键词（redfish_attrs.sh 三级匹配未命中）" >&2
    fi
    rm -rf "$tmpd"
}

# ── 执行 ─────────────────────────────────────────────────────
rc_overall=0

run_redfish() {
    section "Redfish BIOS"
    TMPD=$(mktemp -d /tmp/bios_redfish.XXXXXX)
    if member=$(redfish_fetch_bios "$TMPD/redfish"); then
        echo "--- $member/Bios ---"
        cat "$TMPD/redfish.bios.json"
        if [ -f "$TMPD/redfish.bios_settings.json" ]; then
            echo "--- $member/Bios/Settings ---"
            cat "$TMPD/redfish.bios_settings.json"
        fi
    else
        rc_overall=1
    fi
    rm -rf "$TMPD"
}

for m in "${MODES[@]}"; do
    case "$m" in
        --all)
            mode_dmi
            if [ -n "$BMC_HOST" ] && [ -n "$BMC_USER" ]; then
                run_redfish
            else
                echo "SKIP redfish: 未提供 BMC 凭据" >&2
            fi
            mode_smt
            mode_cstate
            mode_turbo
            mode_numa
            mode_ddr
            mode_aspm
            mode_prefetcher
            ;;
        --dmi) mode_dmi ;;
        --redfish)
            if [ -z "$BMC_HOST" ] || [ -z "$BMC_USER" ]; then
                echo "SKIP redfish: 未提供 BMC 地址或凭据（--bmc-host/-u/-p 或 REDFISH_HOST/REDFISH_USER/REDFISH_PASS）" >&2
            else
                run_redfish
            fi
            ;;
        --smt) mode_smt ;;
        --cstate) mode_cstate ;;
        --turbo) mode_turbo ;;
        --numa) mode_numa ;;
        --ddr) mode_ddr ;;
        --aspm) mode_aspm ;;
        --prefetcher) mode_prefetcher ;;
    esac
done

exit "$rc_overall"
