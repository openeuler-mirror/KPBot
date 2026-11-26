#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PID=""
MODE=""
DURATION=5
TRACEPOINT=false
HELP=false

print_help() {
    cat <<'EOF'
kptools-proc-collector — Collect per-process/thread performance data

Usage:
  collect.sh -p <PID> --<mode> [options]

Modes:
  --threads      Thread-CPU distribution snapshot (ps -L: pid,tid,psr,pcpu,stat,comm,wchan)
  --cpu          Per-thread CPU usage over time (pidstat -t -u 1 D)
  --ctx          Per-thread context switches over time (pidstat -t -w 1 D)
  --migration    Context-switch/migration counters (perf stat, 1s intervals)
  --affinity     Main process + per-thread CPU affinity masks (taskset)
  --maps         Process memory mappings (/proc/PID/maps)
  --numa         Process NUMA memory mapping + numastat
  --cgroup       cgroup limits of the PID's own cgroup (v2 path-resolved with
                 nearest-limit ancestor walk, v1 fallback) + /proc/PID/cgroup
  --binary       Binary info: exe path, readelf -A/-d, ldd
  --env          Thread-pool env vars (OMP/MKL_NUM_THREADS etc.) from /proc/PID/environ
  --all          Run all modes in order

Options:
  -p <PID>       Target process PID (required)
  -d <sec>       Duration for timed modes (default: 5)
  --tracepoint   With --migration: add sched:sched_migrate_task tracepoint (needs root/tracefs)
  --help         Show this help

stdout: raw collected data (tool native format); stderr: diagnostics
exit 0: success; non-zero: failure
EOF
}

# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -p|-d) echo "ERROR: option ${!#} requires a value" >&2; exit 1 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="$2"; shift 2 ;;
        -d) DURATION="$2"; shift 2 ;;
        --threads|--cpu|--ctx|--migration|--affinity|--maps|--numa|--cgroup|--binary|--env|--all) MODE="$1"; shift ;;
        --tracepoint) TRACEPOINT=true; shift ;;
        --help) HELP=true; shift ;;
        *) echo "ERROR: unknown option: $1" >&2; print_help >&2; exit 1 ;;
    esac
done

if [ "$HELP" = true ]; then
    print_help
    exit 0
fi

if [ -z "$MODE" ]; then
    echo "ERROR: a --<mode> is required (see --help)" >&2
    exit 1
fi

if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    echo "ERROR: -p <PID> (numeric) is required" >&2
    exit 1
fi

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [ "$DURATION" -lt 1 ]; then
    echo "ERROR: -d must be a positive integer (got '$DURATION')" >&2
    exit 1
fi

check_pid() {
    if [ ! -d "/proc/$PID" ]; then
        echo "ERROR: process $PID does not exist" >&2
        exit 1
    fi
}

require_tool() {
    local tool="$1" pkg="$2"
    if ! command -v "$tool" &>/dev/null; then
        echo "ERROR: $tool not found (install: $pkg)" >&2
        exit 1
    fi
}

read_proc_or_deny() {
    local path="$1" what="$2"
    if ! cat "$path" > /dev/null 2>&1; then
        echo "ERROR: cannot read $path ($what). Cross-uid target: run as the same user or root." >&2
        exit 1
    fi
    cat "$path"
}

mode_threads() {
    check_pid
    echo "# ps -L -p $PID -o pid,tid,psr,pcpu,stat,comm,wchan:32"
    ps -L -p "$PID" -o pid,tid,psr,pcpu,stat,comm,wchan:32
}

mode_cpu() {
    check_pid
    require_tool pidstat "yum install sysstat or apt install sysstat"
    echo "# pidstat -p $PID -t -u 1 $DURATION"
    pidstat -p "$PID" -t -u 1 "$DURATION"
}

mode_ctx() {
    check_pid
    require_tool pidstat "yum install sysstat or apt install sysstat"
    echo "# pidstat -p $PID -t -w 1 $DURATION"
    pidstat -p "$PID" -t -w 1 "$DURATION"
}

mode_migration() {
    check_pid
    require_tool perf "yum install perf or apt install linux-tools"
    local events="context-switches,cpu-migrations"
    if [ "$TRACEPOINT" = true ]; then
        events="context-switches,cpu-migrations,sched:sched_migrate_task"
    fi
    echo "# perf stat -p $PID -e $events -I 1000 -- sleep $DURATION"
    perf stat -p "$PID" -e "$events" -I 1000 -- sleep "$DURATION" 2>&1
}

mode_affinity() {
    check_pid
    require_tool taskset "yum install util-linux or apt install util-linux"
    if ! taskset -cp "$PID" 2>/dev/null; then
        echo "ERROR: cannot read affinity of PID $PID (cross-uid target: run as the same user or root)" >&2
        exit 1
    fi
    echo "# per-thread affinity"
    local tids
    tids=$(ps -L -p "$PID" -o tid= 2>/dev/null || true)
    local tid
    for tid in $tids; do
        taskset -pc "$tid" 2>/dev/null || echo "tid $tid: affinity read failed (race: thread exited)" >&2
    done
}

mode_maps() {
    check_pid
    echo "# /proc/$PID/maps"
    read_proc_or_deny "/proc/$PID/maps" "memory mappings"
}

mode_numa() {
    check_pid
    echo "# /proc/$PID/numa_maps"
    read_proc_or_deny "/proc/$PID/numa_maps" "numa maps"
    if command -v numastat &>/dev/null; then
        echo "# numastat -p $PID"
        numastat -p "$PID" 2>&1 || true
    else
        echo "# numastat not found, skipped (optional)" >&2
    fi
}

# v2: 自 leaf 向上找最近设置限制的祖先，输出 "value @ 相对路径"
v2_nearest_limit() {
    local cur="$1" file="$2" v
    while [ "$cur" != "/sys/fs/cgroup" ] && [ -n "$cur" ]; do
        cur="${cur%/*}"
        v=$(cat "$cur/$file" 2>/dev/null) || v=""
        if [ -n "$v" ] && [ "${v%% *}" != "max" ]; then
            echo "$v @ ${cur#/sys/fs/cgroup}"
            return 0
        fi
    done
    return 1
}

# v1: 控制器挂载点候选（cpu 常与 cpuacct 合并挂载，两种顺序都尝试）
v1_base() {
    local subsys="$1" b
    for b in "/sys/fs/cgroup/${subsys},${subsys}acct" "/sys/fs/cgroup/${subsys}acct,${subsys}" "/sys/fs/cgroup/${subsys}"; do
        if [ -d "$b" ]; then
            echo "$b"
            return 0
        fi
    done
    return 1
}

mode_cgroup() {
    check_pid
    echo "# /proc/$PID/cgroup"
    cat "/proc/$PID/cgroup" 2>/dev/null || true

    local rel cg val anc base rel_cpu rel_mem rel_cpuset
    if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
        echo "# cgroup version: v2"
        # 解析 PID 自身所在 cgroup（此前直接读根层级，容器/限流场景下结论错误）
        rel=$(awk -F: '$1 == "0" {print $3; exit}' "/proc/$PID/cgroup" 2>/dev/null || true)
        rel=${rel:-/}
        cg="/sys/fs/cgroup$rel"
        if [ ! -d "$cg" ]; then
            echo "# cgroup dir $cg not visible (namespaced container view?), falling back to /sys/fs/cgroup" >&2
            cg="/sys/fs/cgroup"
            rel="/"
        fi
        echo "# cgroup path (v2): $rel"

        echo "# cpuset"
        val=$(cat "$cg/cpuset.cpus.effective" 2>/dev/null) || val="unknown"
        echo "cpuset.cpus.effective (v2, $rel): $val"

        echo "# cpu limit"
        val=$(cat "$cg/cpu.max" 2>/dev/null) || val="unknown"
        echo "cpu.max (v2, $rel): $val"
        # leaf 未设置（max）或未委托控制器（文件缺失）时，向上找最近设置限制的祖先
        if { [ "$val" = "unknown" ] || [ "${val%% *}" = "max" ]; } && anc=$(v2_nearest_limit "$cg" cpu.max); then
            echo "cpu.max (v2, nearest limit @ ${anc#* @ }): ${anc%% @ *}"
        fi

        echo "# memory limit"
        val=$(cat "$cg/memory.max" 2>/dev/null) || val="unknown"
        echo "memory.max (v2, $rel): $val"
        if { [ "$val" = "unknown" ] || [ "$val" = "max" ]; } && anc=$(v2_nearest_limit "$cg" memory.max); then
            echo "memory.max (v2, nearest limit @ ${anc#* @ }): ${anc%% @ *}"
        fi
    else
        echo "# cgroup version: v1"
        rel_cpu=$(awk -F: '$2 ~ /(^|,)cpu(,|$)/ {print $3; exit}' "/proc/$PID/cgroup" 2>/dev/null || true)
        rel_mem=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ {print $3; exit}' "/proc/$PID/cgroup" 2>/dev/null || true)
        rel_cpuset=$(awk -F: '$2 ~ /(^|,)cpuset(,|$)/ {print $3; exit}' "/proc/$PID/cgroup" 2>/dev/null || true)
        rel_cpu=${rel_cpu:-/}
        rel_mem=${rel_mem:-/}
        rel_cpuset=${rel_cpuset:-/}

        echo "# cpuset"
        if base=$(v1_base cpuset); then
            # 容器命名空间视图：控制器挂载根即本容器 cgroup 子树，宿主机相对路径不可见时
            # 回退读挂载根（与 v2 分支的处理一致）；目录存在但文件不可读属权限问题，不回退
            if [ ! -d "$base$rel_cpuset" ]; then
                echo "# cpuset path $rel_cpuset not visible (namespaced container view?), falling back to mount root" >&2
                rel_cpuset="/"
            fi
            if [ -r "$base$rel_cpuset/cpuset.cpus" ]; then
                echo "cpuset/cpuset.cpus (v1, $rel_cpuset): $(cat "$base$rel_cpuset/cpuset.cpus" 2>/dev/null)"
            else
                echo "cpuset: unknown"
            fi
        else
            echo "cpuset: unknown"
        fi

        echo "# cpu limit"
        if base=$(v1_base cpu); then
            if [ ! -d "$base$rel_cpu" ]; then
                echo "# cpu path $rel_cpu not visible (namespaced container view?), falling back to mount root" >&2
                rel_cpu="/"
            fi
            if [ -r "$base$rel_cpu/cpu.cfs_quota_us" ]; then
                echo "cpu.cfs_quota_us (v1, $rel_cpu): $(cat "$base$rel_cpu/cpu.cfs_quota_us" 2>/dev/null)"
                echo "cpu.cfs_period_us (v1, $rel_cpu): $(cat "$base$rel_cpu/cpu.cfs_period_us" 2>/dev/null)"
            else
                echo "cpu limit: unknown"
            fi
        else
            echo "cpu limit: unknown"
        fi

        echo "# memory limit"
        if base=$(v1_base memory); then
            if [ ! -d "$base$rel_mem" ]; then
                echo "# memory path $rel_mem not visible (namespaced container view?), falling back to mount root" >&2
                rel_mem="/"
            fi
            if [ -r "$base$rel_mem/memory.limit_in_bytes" ]; then
                echo "memory.limit_in_bytes (v1, $rel_mem): $(cat "$base$rel_mem/memory.limit_in_bytes" 2>/dev/null)"
            else
                echo "memory limit: unknown"
            fi
        else
            echo "memory limit: unknown"
        fi
    fi
}

mode_binary() {
    check_pid
    local exe
    exe=$(readlink -f "/proc/$PID/exe" 2>/dev/null || true)
    if [ -z "$exe" ]; then
        echo "ERROR: cannot resolve /proc/$PID/exe (cross-uid target: run as the same user or root)" >&2
        exit 1
    fi
    echo "# exe: $exe"
    if command -v readelf &>/dev/null; then
        echo "# readelf -A"
        readelf -A "$exe" 2>&1 || true
        echo "# readelf -d"
        readelf -d "$exe" 2>&1 || true
    else
        echo "readelf not found, skipped (install: binutils)" >&2
    fi
    if command -v ldd &>/dev/null; then
        echo "# ldd"
        ldd "$exe" 2>&1 || true
    else
        echo "ldd not found, skipped (glibc/glibc-common)" >&2
    fi
}

mode_env() {
    check_pid
    echo "# /proc/$PID/environ (thread-pool related)"
    if ! cat "/proc/$PID/environ" > /dev/null 2>&1; then
        echo "ERROR: cannot read /proc/$PID/environ. Cross-uid target: run as the same user or root." >&2
        exit 1
    fi
    if ! tr '\0' '\n' < "/proc/$PID/environ" | grep -E 'NUM_THREADS|OMP|MKL|OPENBLAS|NUMEXPR|RAYON|KMP_|JAVA_OPTS|JAVA_TOOL|GOMAXPROCS'; then
        echo "no thread-pool related env vars found" >&2
    fi
}

run_mode() {
    case "$1" in
        --threads)   mode_threads ;;
        --cpu)       mode_cpu ;;
        --ctx)       mode_ctx ;;
        --migration) mode_migration ;;
        --affinity)  mode_affinity ;;
        --maps)      mode_maps ;;
        --numa)      mode_numa ;;
        --cgroup)    mode_cgroup ;;
        --binary)    mode_binary ;;
        --env)       mode_env ;;
    esac
}

if [ "$MODE" = "--all" ]; then
    rc=0
    for m in --threads --cpu --ctx --migration --affinity --maps --numa --cgroup --binary --env; do
        echo "========== $m ==========" >&2
        # 子 shell 隔离：模式函数内的 exit（check_pid/require_tool 等）只终止本模式，
        # 不中断整个 --all 流程；否则首个失败模式会吞掉其后所有模式
        ( run_mode "$m" ) || rc=1
    done
    exit "$rc"
fi

run_mode "$MODE"
