#!/usr/bin/env bash
# Read-only AgentENV environment collection; writes only a new evidence directory.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash collect_environment.sh --output NEW_DIRECTORY [--role host|guest|container|unknown] [--pid PID]

Collect kernel configuration, virtualization, CPU topology and ublk visibility.
No package installation, device creation, kernel changes or reboot.
Exit: 0 = ARM Linux collection completed, 2 = unsupported platform, 1 = input/output error.
Missing evidence is recorded as unknown, not as a disabled feature.
EOF
}

fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

output=""
role="unknown"
target_pid=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --output|--role|--pid)
            [[ $# -ge 2 && -n "$2" ]] || fail "$1 requires a value"
            case "$1" in
                --output) output="$2" ;;
                --role) role="$2" ;;
                --pid) target_pid="$2" ;;
            esac
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done
[[ -n "$output" ]] || fail "--output is required"
case "$role" in host|guest|container|unknown) ;; *) fail "invalid role: $role" ;; esac
if [[ -n "$target_pid" && ! "$target_pid" =~ ^[1-9][0-9]*$ ]]; then
    fail "--pid must be a positive integer"
fi
[[ ! -e "$output" && ! -L "$output" ]] || fail "output already exists: $output"
umask 077
mkdir -- "$output" || fail "cannot create output directory (parent must exist): $output"

capture() {
    local name="$1" rc=0 state="available"
    shift
    "$@" > "$output/$name.txt" 2>&1 || rc=$?
    if [[ "$rc" -ne 0 ]]; then state="unknown"; fi
    printf '%s\t%s\t%s\n' "$name.txt" "$state" "$rc" >> "$output/manifest.tsv"
}

read_file() {
    if [[ -r "$1" ]]; then cat -- "$1"; else printf 'unknown: cannot read %s\n' "$1"; return 1; fi
}

kernel_config() {
    local source="" rc=0
    if [[ -r /proc/config.gz ]] && command -v gzip >/dev/null 2>&1; then
        source="/proc/config.gz"
        gzip -cd "$source" > "$output/.kernel-config.raw" || rc=$?
    elif [[ -r "/boot/config-$kernel_release" ]]; then
        source="/boot/config-$kernel_release"
        cat -- "$source" > "$output/.kernel-config.raw" || rc=$?
    else
        printf 'unknown: no readable configuration for running kernel %s\n' "$kernel_release"
        return 1
    fi
    printf 'source=%s\n' "$source"
    if [[ "$rc" -ne 0 ]]; then
        rm -f -- "$output/.kernel-config.raw"
        printf 'unknown: failed to read kernel configuration\n'
        return 1
    fi
    grep -E '^(# )?CONFIG_(BLK_DEV_UBLK|HZ[^ =]*|PREEMPT[^ =]*|NO_HZ[^ =]*|PARAVIRT[^ =]*|KVM[^ =]*)(=| is not set)' \
        "$output/.kernel-config.raw" || rc=$?
    rm -f -- "$output/.kernel-config.raw"
    if [[ "$rc" -ne 0 ]]; then
        printf 'unknown: selected configuration keys not found\n'
        return 1
    fi
}

topology() {
    local cpu file found=false
    for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
        [[ -d "$cpu" ]] || continue
        for file in "$cpu"/topology/{physical_package_id,die_id,cluster_id,core_id,thread_siblings_list}; do
            if [[ ! -r "$file" ]]; then
                printf '%s=unknown\n' "$file"
                continue
            fi
            found=true
            printf '%s=' "$file"
            cat -- "$file"
        done
    done
    if [[ "$found" == false ]]; then printf 'unknown: CPU topology unavailable\n'; return 1; fi
    printf 'Note: missing cluster_id is unknown; NUMA/L3 IDs are not TLBI broadcast domains.\n'
}

virtualization() {
    local value rc=0
    if ! command -v systemd-detect-virt >/dev/null 2>&1; then
        printf 'unknown: systemd-detect-virt unavailable\n'
        return 1
    fi
    value="$(systemd-detect-virt 2>&1)" || rc=$?
    printf '%s\n' "$value"
    # systemd-detect-virt returns 1 for a successful "none" detection.
    [[ "$rc" -eq 0 || ( "$rc" -eq 1 && "$value" == none ) ]]
}

ublk_visibility() {
    if [[ -e /dev/ublk-control ]]; then
        ls -l /dev/ublk-control
        if [[ ! -c /dev/ublk-control ]]; then
            printf 'control_device_access=not_character_device\n'
        elif [[ -r /dev/ublk-control && -w /dev/ublk-control ]]; then
            printf 'control_device_access=read_write\n'
        else
            printf 'control_device_access=restricted\n'
        fi
    else
        printf '/dev/ublk-control not visible in this namespace\n'
        printf 'control_device_access=not_visible\n'
    fi
    if [[ -d /sys/module/ublk_drv ]]; then
        printf '/sys/module/ublk_drv visible\n'
        local key
        for key in initstate srcversion taint; do
            printf '%s=' "$key"
            read_file "/sys/module/ublk_drv/$key" || true
        done
    else
        printf '/sys/module/ublk_drv not visible (does not rule out built-in support)\n'
    fi
    if [[ -r /proc/modules ]]; then
        grep -E '^ublk_drv ' /proc/modules || printf 'ublk_drv not listed in /proc/modules\n'
    else
        printf 'unknown: cannot read /proc/modules\n'
    fi
    printf 'Access checks are not an ioctl or I/O test.\n'
    printf 'No module loaded and no device created by this collector.\n'
}

module_metadata() {
    printf 'Installed module metadata; does not establish loaded-module identity.\n'
    if ! command -v modinfo >/dev/null 2>&1; then
        printf 'unknown: modinfo unavailable\n'
        return 1
    fi
    modinfo ublk_drv
}

process_evidence() {
    local pid="$1" rc=0
    printf 'pid=%s\n' "$pid"
    if [[ -r "/proc/$pid/status" ]]; then
        grep -E '^(Name|Pid|PPid|Tgid|NSpid|CapPrm|CapEff|Cpus_allowed_list|Mems_allowed_list):' \
            "/proc/$pid/status" || rc=1
    else
        printf 'unknown: process status unavailable\n'
        rc=1
    fi
    read_file "/proc/$pid/cgroup" || rc=1
    printf 'Resolve the target cgroup mount and effective constraints before recommending cpuset changes.\n'
    return "$rc"
}

process_identity() {
    local pid="$1" rc=0
    printf 'pid=%s\nexe=' "$pid"
    if ! readlink "/proc/$pid/exe"; then
        printf 'unknown: executable identity unavailable; do not substitute another PID\n'
        rc=1
    fi
    printf 'stat='
    read_file "/proc/$pid/stat" || rc=1
    printf 'Executable access can be restricted even when process status is readable.\n'
    return "$rc"
}

kernel_os="$(uname -s)"
kernel_arch="$(uname -m)"
kernel_release="$(uname -r)"
printf 'file\tstate\texit_code\n' > "$output/manifest.tsv"
{
    printf 'collected_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'os=%s\narchitecture=%s\nkernel=%s\n' "$kernel_os" "$kernel_arch" "$kernel_release"
    printf 'hostname=%s\ncollector_uid=%s\n' "$(uname -n)" "$(id -u)"
    printf 'boot_id='
    read_file /proc/sys/kernel/random/boot_id || true
    printf 'declared_role=%s\nrole_verified=false\n' "$role"
    printf 'Feature availability and performance require separate verification.\n'
} > "$output/environment.txt"

if [[ "$kernel_os" != Linux || ! "$kernel_arch" =~ ^(aarch64|arm64)$ ]]; then
    printf 'unsupported_platform\n' > "$output/collection-status.txt"
    printf 'Unsupported platform: %s/%s; evidence: %s\n' "$kernel_os" "$kernel_arch" "$output"
    exit 2
fi

capture os-release read_file /etc/os-release
capture kernel-config kernel_config
capture virtualization virtualization
capture cpu lscpu
capture topology topology
capture online-cpus read_file /sys/devices/system/cpu/online
capture ublk ublk_visibility
capture ublk-module module_metadata
capture collector-process process_evidence "$$"
capture cgroup-mounts grep -E ' - cgroup2? ' /proc/self/mountinfo
capture cpu-pressure read_file /proc/pressure/cpu
capture perf-policy read_file /proc/sys/kernel/perf_event_paranoid
if [[ -n "$target_pid" ]]; then
    capture target-process process_evidence "$target_pid"
    capture target-identity process_identity "$target_pid"
fi
printf 'collected\n' > "$output/collection-status.txt"
printf 'Collected ARM Linux evidence: %s (feature/performance validation not performed)\n' "$output"
