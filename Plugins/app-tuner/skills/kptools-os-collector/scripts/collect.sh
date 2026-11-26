#!/bin/bash
# os-collector collect — OS/内核级只读采集统一入口
set -euo pipefail

MODES=()

print_help() {
    cat <<'EOF'
kptools-os-collector collect — Collect OS/kernel configuration and state (read-only)

Modes:
  --kernel           内核/OS 版本 + GRUB 启动参数（uname / os-release / /proc/cmdline）
  --sysctl           sysctl 全量 + 重点参数白名单
  --thp              THP enabled/defrag
  --hugepages        HugePages 总量/空闲
  --ulimit           ulimit -a（shell 视角；进程级用 kptools-proc-collector）
  --services         tuned / A-Tune / irqbalance 状态
  --iosched          per-device I/O scheduler（nvme/sd/vd/xvd）
  --numa-balancing   /proc/sys/kernel/numa_balancing
  --virt             虚拟化/容器检测（systemd-detect-virt 多级降级）
  --cgroup           cgroup 资源边界（v2 优先 v1 回退）
  --perf             perf/PMU 能力（perf_event_paranoid + kptr_restrict + 事件枚举）
  --all              依次执行全部模式

Notes:
  stdout: 原始输出（=== 分节）；stderr: 权限/缺失诊断
  exit 0: 成功；3: 参数错误
  只读约束：无任何写操作；进程级采集归 kptools-proc-collector
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --kernel|--sysctl|--thp|--hugepages|--ulimit|--services|--iosched|--numa-balancing|--virt|--cgroup|--perf|--all)
            MODES+=("$1"); shift ;;
        --help) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

if [ ${#MODES[@]} -eq 0 ]; then
    echo "ERROR: 至少需要一个模式参数（见 --help）" >&2
    exit 3
fi

section() { echo ""; echo "=== $1 ==="; }

mode_kernel() {
    section "uname -a"
    uname -a
    section "os-release"
    cat /etc/os-release 2>/dev/null || echo "(not available)"
    section "/proc/cmdline"
    cat /proc/cmdline 2>/dev/null || echo "(not available)"
}

mode_sysctl() {
    section "sysctl -a"
    sysctl -a 2>/dev/null || echo "(部分参数需 root，已跳过)"
    section "重点参数白名单"
    for key in vm.swappiness vm.dirty_ratio vm.dirty_background_ratio vm.max_map_count \
               net.core.somaxconn net.core.rmem_max net.core.wmem_max \
               net.ipv4.tcp_max_syn_backlog net.ipv4.tcp_tw_reuse \
               kernel.numa_balancing fs.file-max; do
        # "[ -r ] && printf" 在最后一个 key 不可读时会让函数返回 1，set -e 中止脚本
        if [ -r "/proc/sys/${key//./\/}" ]; then
            printf '%s = %s\n' "$key" "$(cat "/proc/sys/${key//./\/}" 2>/dev/null)"
        fi
    done
}

mode_thp() {
    section "THP enabled"
    cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo "(not available)"
    section "THP defrag"
    cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || echo "(not available)"
}

mode_hugepages() {
    section "HugePages"
    grep -E 'Huge|AnonHuge' /proc/meminfo 2>/dev/null || echo "(not available)"
}

mode_ulimit() {
    section "ulimit -a"
    sh -c "ulimit -a" 2>/dev/null || echo "(not available)"
}

mode_services() {
    if command -v tuned-adm &>/dev/null; then
        section "tuned profile"
        tuned-adm active 2>/dev/null || echo "(tuned-adm failed)"
    else
        echo "SKIP tuned: tuned-adm 不存在（OPTIONAL_ABSENT）" >&2
    fi
    if command -v systemctl &>/dev/null; then
        section "A-Tune status"
        systemctl status atuned --no-pager 2>/dev/null | head -5 || echo "(atuned not active)"
        section "irqbalance status"
        systemctl status irqbalance --no-pager 2>/dev/null | head -5 || echo "(irqbalance not active)"
    else
        echo "SKIP systemctl: 不存在（OPTIONAL_ABSENT）" >&2
    fi
}

mode_iosched() {
    section "I/O scheduler"
    local d f
    for d in /sys/block/nvme* /sys/block/sd* /sys/block/vd* /sys/block/xvd*; do
        [ -d "$d/queue" ] || continue
        echo "## $(basename "$d")"
        for f in rotational scheduler nr_requests read_ahead_kb logical_block_size; do
            # "[ -r ] && printf" 在最后一个文件不可读时会让循环返回 1，set -e 中止脚本
            if [ -r "$d/queue/$f" ]; then
                printf '%s=%s\n' "$f" "$(cat "$d/queue/$f")"
            fi
        done
    done
}

mode_numa_balancing() {
    section "numa_balancing"
    cat /proc/sys/kernel/numa_balancing 2>/dev/null || echo "(not available)"
}

mode_virt() {
    section "virtualization"
    local virt="none"
    if command -v systemd-detect-virt &>/dev/null; then
        virt=$(systemd-detect-virt 2>/dev/null || echo "none")
    fi
    if [ "$virt" = "none" ]; then
        if grep -qs 'hypervisor' /proc/cpuinfo; then
            virt="hypervisor (from /proc/cpuinfo)"
        elif [ -f /.dockerenv ] || [ -r /run/.containerenv ]; then
            virt="container (from runtime markers)"
        fi
    fi
    echo "virtualization: $virt"
    echo "--- /proc/1/cgroup ---"
    cat /proc/1/cgroup 2>/dev/null | head -5
}

mode_cgroup() {
    # cgroup v1/v2/hybrid（两者并存）均需覆盖。不能用 grep /proc/filesystems 判断：
    # 它只列出内核注册的 fs 类型，cgroup2 通常总是已注册，纯 v1 系统也会命中。
    # 以实际挂载为准：stat -fc %T 判根挂载类型；hybrid 时 v2 挂在 /sys/fs/cgroup/unified。
    local v2_base="" have_v1=0 ctl d f files
    if [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = "cgroup2fs" ]; then
        v2_base="/sys/fs/cgroup"
    elif [ "$(stat -fc %T /sys/fs/cgroup/unified 2>/dev/null)" = "cgroup2fs" ]; then
        v2_base="/sys/fs/cgroup/unified"
    fi
    if [ -d /sys/fs/cgroup/cpuset ] || [ -d /sys/fs/cgroup/cpu ] || [ -d /sys/fs/cgroup/memory ]; then
        have_v1=1
    fi
    if [ -n "$v2_base" ]; then
        section "cgroup v2 ($v2_base)"
        echo "cgroup: v2"
        for f in cgroup.controllers cpuset.cpus.effective cpu.max memory.max memory.high; do
            if [ -r "$v2_base/$f" ]; then
                printf '%s=%s\n' "$f" "$(cat "$v2_base/$f")"
            fi
        done
    fi
    if [ "$have_v1" = 1 ]; then
        section "cgroup v1"
        echo "cgroup: v1"
        for ctl in cpuset cpu memory; do
            d="/sys/fs/cgroup/$ctl"
            [ -d "$d" ] || continue
            echo "## $ctl"
            case "$ctl" in
                cpuset) files="cpuset.cpus cpuset.mems" ;;
                cpu)    files="cfs_quota_us cfs_period_us" ;;
                memory) files="memory.limit_in_bytes memory.usage_in_bytes" ;;
            esac
            for f in $files; do
                if [ -r "$d/$f" ]; then
                    printf '%s=%s\n' "$f" "$(cat "$d/$f")"
                fi
            done
        done
    fi
    if [ -z "$v2_base" ] && [ "$have_v1" = 0 ]; then
        echo "NOTICE: /sys/fs/cgroup 下未发现 cgroup v2 或 v1 挂载" >&2
        rc_overall=1
    fi
    if [ -r /proc/self/cgroup ]; then
        echo "--- /proc/self/cgroup ---"
        head -3 /proc/self/cgroup
    fi
}

mode_perf() {
    section "perf_event_paranoid"
    cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo "(not available)"
    section "kptr_restrict"
    cat /proc/sys/kernel/kptr_restrict 2>/dev/null || echo "(not available)"
    if command -v perf &>/dev/null; then
        section "perf hardware events（前 40 行）"
        perf list 2>/dev/null | grep -E '(^|[[:space:]])(cycles|instructions|cache-misses|branches)([[:space:]]|$)' | head -40
        echo "perf_events_count: $(perf list 2>/dev/null | grep -c .)"
    else
        echo "perf: OPTIONAL_ABSENT"
    fi
}

rc_overall=0
for m in "${MODES[@]}"; do
    case "$m" in
        --all)
            mode_kernel; mode_sysctl; mode_thp; mode_hugepages; mode_ulimit
            mode_services; mode_iosched; mode_numa_balancing; mode_virt; mode_cgroup; mode_perf
            ;;
        --kernel) mode_kernel ;;
        --sysctl) mode_sysctl ;;
        --thp) mode_thp ;;
        --hugepages) mode_hugepages ;;
        --ulimit) mode_ulimit ;;
        --services) mode_services ;;
        --iosched) mode_iosched ;;
        --numa-balancing) mode_numa_balancing ;;
        --virt) mode_virt ;;
        --cgroup) mode_cgroup ;;
        --perf) mode_perf ;;
    esac
done

exit "$rc_overall"
