#!/usr/bin/env bash

# MySQL 场景 OS 优化：默认 dry-run；执行必须 --execute --approved-change-id。
#
# 说明：只处理 sysctl（net/io/memory/scheduler 四组）/THP/大页/网卡队列与 IRQ；
#       不碰内核启动参数/BIOS/文件系统格式化；任何情况下都不停止宿主防火墙。
#       备份按每次运行独占创建（mktemp），重试不会截断上一次的备份。
# 退出码：0=成功/已打印计划；1=拒绝/部分失败；2=用法错误。

set -u
set -o pipefail

EXECUTE=0
CHANGE_ID=""
PROFILE="read"
ETH=""
IRQ_CORES=""
QUEUES=""
THP=""
HUGEPAGES=""
OUTDIR="${OUTDIR:-./os_change_$(date +%Y%m%d_%H%M%S)}"
CATEGORY=""
ROLLBACK_FILE=""
ACTIVE=""

VALID_CATEGORIES="net io memory scheduler sysctl thp hugepages irq all"
THP_PATH="/sys/kernel/mm/transparent_hugepage/enabled"

# sysctl 四组基线：IO(vm.dirty*)、内存(overcommit/swappiness/min_free)、
# 调度(numa_balancing) 与网络；网络 write 画像追加 tcp_timestamps。
# 取值为参考基线，需按实测调整。
SYSCTL_NET=(
  "net.core.somaxconn=8192"
  "net.ipv4.tcp_max_syn_backlog=16384"
  "net.core.netdev_max_backlog=250000"
  "net.ipv4.tcp_sack=1"
  "net.ipv4.tcp_tw_reuse=1"
  "net.ipv4.tcp_adv_win_scale=1"
)
SYSCTL_NET_WRITE=(
  "net.ipv4.tcp_timestamps=1"
)
SYSCTL_IO=(
  "fs.aio-max-nr=1048576"
  "vm.dirty_background_ratio=5"
  "vm.dirty_ratio=20"
)
SYSCTL_MEMORY=(
  "vm.overcommit_memory=0"
  "vm.swappiness=1"
  "vm.min_free_kbytes=262144"
)
SYSCTL_SCHEDULER=(
  "kernel.numa_balancing=0"
)

usage() {
  cat <<'EOF'
用法：
  bash scripts/optimize_os.sh --profile read --category net
  bash scripts/optimize_os.sh --execute --approved-change-id CHG-001 --profile write \
    --category net,irq --eth eth0 --irq-cores 80-85 --queues 8
  bash scripts/optimize_os.sh --execute --approved-change-id CHG-001 \
    --category thp --thp never
  bash scripts/optimize_os.sh --rollback /path/to/os-before.XXXXXX

选项：
  --execute                 真正执行（需同时给 --approved-change-id）
  --approved-change-id ID   变更单号
  --profile read|write      read 为只读画像口径，write 追加 tcp_timestamps
  --category LIST           逗号分隔：net/io/memory/scheduler/sysctl/thp/hugepages/irq/all
                             不指定时按所给参数自动推导；一次只改一类时显式给出
  --eth NAME                业务网卡（irq 类别必需）
  --irq-cores LIST          十进制 CPU 编号/有序区间，逗号分隔（如 80-85,120）
  --queues N                网卡合并队列数（正整数）
  --thp never|always|madvise
  --hugepages N             非负整数；容器环境下禁止
  --outdir DIR              备份/日志目录
  --rollback FILE           按备份回滚（队列→重定位 IRQ→亲和性→irqbalance→sysctl→内存页）
EOF
}

# $1=选项名, $2=取值；缺值立即 exit 2，避免 shift 2 失败造成解析死循环。
need_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then
    echo "[FAIL] $1 缺少取值" >&2
    exit 2
  fi
  # 备份文件是回滚阶段的特权输入：任何选项值都不得含空白/控制字符（换行注入）。
  case "$2" in
    *[[:space:]]*|*[[:cntrl:]]*)
      echo "[FAIL] $1 的取值含空白或控制字符，拒绝" >&2
      exit 2
      ;;
  esac
}

# 自由文本选项额外收窄字符集，禁止换行、分号、引号等进入备份/回滚链路。
validate_free_text() {
  local name="$1" value="$2"
  if [[ ! "${value}" =~ ^[A-Za-z0-9._:/-]+$ ]]; then
    echo "[FAIL] ${name} 含非法字符（仅允许字母、数字、._:/-）" >&2
    return 1
  fi
  return 0
}

normalize_categories() {
  local raw="$1" item out=""
  local -a items=()
  [ -n "$raw" ] || return 0
  IFS=',' read -ra items <<< "$raw"
  for item in "${items[@]}"; do
    case "$item" in
      net|io|memory|scheduler|thp|hugepages|irq) out="${out} ${item}" ;;
      sysctl) out="${out} net io memory scheduler" ;;
      all) out="${out} net io memory scheduler thp hugepages irq" ;;
      *) echo "[FAIL] 未知类别 ${item}（可选：${VALID_CATEGORIES}）" >&2; exit 2 ;;
    esac
  done
  ACTIVE=""
  for item in $out; do
    case " ${ACTIVE} " in
      *" ${item} "*) ;;
      *) ACTIVE="${ACTIVE:+${ACTIVE} }${item}" ;;
    esac
  done
}

category_active() {
  case " ${ACTIVE} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

derive_categories() {
  ACTIVE=""
  [ -n "${THP}" ] && ACTIVE="${ACTIVE} thp"
  [ -n "${HUGEPAGES}" ] && ACTIVE="${ACTIVE} hugepages"
  if [ -n "${ETH}" ] && { [ -n "${QUEUES}" ] || [ -n "${IRQ_CORES}" ]; }; then
    ACTIVE="${ACTIVE} irq"
  fi
  [ -z "${ACTIVE}" ] && ACTIVE="net"
  ACTIVE="${ACTIVE# }"
}

sysctl_list() {
  case "$1" in
    net)
      printf '%s\n' "${SYSCTL_NET[@]}"
      [ "${PROFILE}" = write ] && printf '%s\n' "${SYSCTL_NET_WRITE[@]}"
      ;;
    io) printf '%s\n' "${SYSCTL_IO[@]}" ;;
    memory) printf '%s\n' "${SYSCTL_MEMORY[@]}" ;;
    scheduler) printf '%s\n' "${SYSCTL_SCHEDULER[@]}" ;;
  esac
}

# 回滚只接受脚本自身 SYSCTL_* 基线集合内的键；备份文件里的未知键不执行并计失败。
is_known_sysctl_key() {
  local want="$1" kv key
  local -a all=("${SYSCTL_NET[@]}" "${SYSCTL_NET_WRITE[@]}" "${SYSCTL_IO[@]}" "${SYSCTL_MEMORY[@]}" "${SYSCTL_SCHEDULER[@]}")
  for kv in "${all[@]}"; do
    key="${kv%%=*}"
    [ "${key}" = "${want}" ] && return 0
  done
  return 1
}

# BDF 只作为定位信息；空 BDF 不再用于 grep /proc/interrupts。
get_bdf() {
  local eth="$1" bdf=""
  bdf="$(ethtool -i "${eth}" 2>/dev/null | awk -F': ' '/^bus-info/{print $2; exit}')"
  if [ -z "${bdf}" ]; then
    bdf="$(basename "$(readlink -f "/sys/class/net/${eth}/device" 2>/dev/null)" 2>/dev/null)"
  fi
  printf '%s' "${bdf}"
}

# 从目标网卡自己的 sysfs 枚举 IRQ（msi_irqs 目录优先，legacy irq 兜底）。
# 不按 BDF 文本匹配 /proc/interrupts，避免空 BDF 匹配所有设备的中断。
enumerate_irqs() {
  local eth="$1" devdir="/sys/class/net/$1/device" f
  {
    if [ -d "${devdir}/msi_irqs" ]; then
      for f in "${devdir}"/msi_irqs/*; do
        [ -f "${f}" ] && printf '%s\n' "${f##*/}"
      done
    fi
    [ -r "${devdir}/irq" ] && cat "${devdir}/irq" 2>/dev/null
  } | awk '/^[0-9]+$/' | sort -n | tr '\n' ' ' | sed 's/[[:space:]]*$//'
}

current_queues() {
  ethtool -l "$1" 2>/dev/null | awk '/Current hardware settings:/{f=1; next} f && /^Combined:/{print $2; exit}'
}

detect_container() {
  [ -f /.dockerenv ] && return 0
  [ -f /run/.containerenv ] && return 0
  if grep -qE '(docker|containerd|crio|kubepods|lxc|systemd-nspawn)' /proc/1/cgroup 2>/dev/null; then
    return 0
  fi
  if command -v systemd-detect-virt >/dev/null 2>&1; then
    case "$(systemd-detect-virt 2>/dev/null)" in
      docker|podman|container|lxc|lxc-libvirt|systemd-nspawn) return 0 ;;
    esac
  fi
  [ -n "${container:-}" ] && return 0
  [ -n "${KUBERNETES_SERVICE_HOST:-}" ] && return 0
  return 1
}

# 只接受十进制 CPU 编号和有序区间，并逐项核对 online 状态；在进入任何 root 写入前调用。
validate_irq_cores() {
  local spec="$1" part start end cpu state
  local -a parts=()
  IFS=',' read -ra parts <<< "${spec}"
  [ "${#parts[@]}" -gt 0 ] || return 1
  for part in "${parts[@]}"; do
    if [[ "${part}" =~ ^[0-9]+$ ]]; then
      start="${part}"; end="${part}"
    elif [[ "${part}" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      start="${BASH_REMATCH[1]}"; end="${BASH_REMATCH[2]}"
      if [ "$((10#${start}))" -gt "$((10#${end}))" ]; then
        echo "[FAIL] --irq-cores 范围端点无序：${part}" >&2
        return 1
      fi
    else
      echo "[FAIL] --irq-cores 只接受十进制 CPU 编号或有序区间：${part}" >&2
      return 1
    fi
    for ((cpu=10#${start}; cpu<=10#${end}; cpu++)); do
      if [ ! -d "/sys/devices/system/cpu/cpu${cpu}" ]; then
        echo "[FAIL] --irq-cores 含不存在的 CPU：${cpu}" >&2
        return 1
      fi
      if [ -r "/sys/devices/system/cpu/cpu${cpu}/online" ]; then
        read -r state < "/sys/devices/system/cpu/cpu${cpu}/online" || state=""
        if [ "${state}" != "1" ]; then
          echo "[FAIL] --irq-cores 含离线 CPU：${cpu}" >&2
          return 1
        fi
      fi
    done
  done
  return 0
}

validate_inputs() {
  [ -z "${CHANGE_ID}" ] || validate_free_text --approved-change-id "${CHANGE_ID}" || return 1
  [ -z "${ETH}" ] || validate_free_text --eth "${ETH}" || return 1
  [ -z "${OUTDIR}" ] || validate_free_text --outdir "${OUTDIR}" || return 1
  case "${PROFILE}" in
    read|write) ;;
    *) echo "[FAIL] --profile 只能是 read|write" >&2; return 1 ;;
  esac
  if [ -n "${QUEUES}" ] && [[ ! "${QUEUES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "[FAIL] --queues 必须是正整数" >&2; return 1
  fi
  if [ -n "${HUGEPAGES}" ] && [[ ! "${HUGEPAGES}" =~ ^[0-9]+$ ]]; then
    echo "[FAIL] --hugepages 必须是非负整数" >&2; return 1
  fi
  if [ -n "${THP}" ]; then
    case "${THP}" in
      never|always|madvise) ;;
      *) echo "[FAIL] --thp 只能是 never|always|madvise" >&2; return 1 ;;
    esac
  fi
  [ -z "${IRQ_CORES}" ] || validate_irq_cores "${IRQ_CORES}" || return 1
  if category_active irq && [ -z "${ETH}" ]; then
    echo "[FAIL] 类别 irq 需要 --eth" >&2; return 1
  fi
  if { [ -n "${QUEUES}" ] || [ -n "${IRQ_CORES}" ]; } && [ -z "${ETH}" ]; then
    echo "[FAIL] --queues/--irq-cores 需要 --eth" >&2; return 1
  fi
  return 0
}

check_container_guard() {
  if { category_active thp || category_active hugepages; } && detect_container; then
    echo "[FAIL] 检测到容器环境：禁止从容器修改宿主 THP/大页内核接口（--thp/--hugepages）" >&2
    return 1
  fi
  return 0
}

write_backup() {
  local f="$1" group kv k idx irq_cur bdf_cur irqs_snapshot=""
  local -a IRQ_SNAPSHOT=()
  {
    echo "# change_id=${CHANGE_ID} profile=${PROFILE} categories=${ACTIVE} time=$(date -Is)"
    for group in net io memory scheduler; do
      category_active "${group}" || continue
      while IFS= read -r kv; do
        [ -n "${kv}" ] || continue
        k="${kv%%=*}"
        echo "sysctl.${k} = $(sysctl -n "${k}" 2>/dev/null || echo unknown)"
      done < <(sysctl_list "${group}")
    done
    if category_active thp; then
      echo "thp = $(cat "${THP_PATH}" 2>/dev/null || echo unknown)"
    fi
    if category_active hugepages; then
      echo "nr_hugepages = $(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo unknown)"
    fi
    if category_active irq; then
      bdf_cur="$(get_bdf "${ETH}")"
      echo "eth = ${ETH}"
      echo "bdf = ${bdf_cur}"
      echo "irqbalance = $(systemctl is-active irqbalance 2>/dev/null || echo unknown)"
      echo "queues = $(current_queues "${ETH}")"
      irqs_snapshot="$(enumerate_irqs "${ETH}")"
      [ -z "${irqs_snapshot}" ] || read -ra IRQ_SNAPSHOT <<< "${irqs_snapshot}"
      idx=0
      for irq_cur in "${IRQ_SNAPSHOT[@]}"; do
        echo "irq.${irq_cur}.smp_affinity_list = $(cat "/proc/irq/${irq_cur}/smp_affinity_list" 2>/dev/null || echo unknown)"
        echo "lane.${idx}.smp_affinity_list = $(cat "/proc/irq/${irq_cur}/smp_affinity_list" 2>/dev/null || echo unknown)"
        idx=$((idx + 1))
      done
    fi
  } > "${f}"
}

# 备份未完整保存前不做任何系统写入。
validate_backup() {
  local f="$1" group kv k key
  [ -s "${f}" ] || { echo "[FAIL] 备份为空，未做任何系统变更" >&2; return 1; }
  grep -qF '# change_id=' "${f}" || { echo "[FAIL] 备份缺少 change_id，未做任何系统变更" >&2; return 1; }
  for group in net io memory scheduler; do
    category_active "${group}" || continue
    while IFS= read -r kv; do
      [ -n "${kv}" ] || continue
      k="${kv%%=*}"
      grep -qF "sysctl.${k} = " "${f}" || { echo "[FAIL] 备份缺少 sysctl.${k}，未做任何系统变更" >&2; return 1; }
    done < <(sysctl_list "${group}")
  done
  if category_active thp; then
    grep -qF 'thp = ' "${f}" || { echo "[FAIL] 备份缺少 thp 原值，未做任何系统变更" >&2; return 1; }
  fi
  if category_active hugepages; then
    grep -qF 'nr_hugepages = ' "${f}" || { echo "[FAIL] 备份缺少 nr_hugepages 原值，未做任何系统变更" >&2; return 1; }
  fi
  if category_active irq; then
    for key in 'eth = ' 'bdf = ' 'irqbalance = ' 'queues = '; do
      grep -qF "${key}" "${f}" || { echo "[FAIL] 备份缺少 ${key}原值，未做任何系统变更" >&2; return 1; }
    done
  fi
  return 0
}

write_after() {
  local f="$1" group kv k idx irq_cur irqs_now=""
  local -a cur_irqs=()
  {
    echo "# after time=$(date -Is) change_id=${CHANGE_ID} categories=${ACTIVE}"
    for group in net io memory scheduler; do
      category_active "${group}" || continue
      while IFS= read -r kv; do
        [ -n "${kv}" ] || continue
        k="${kv%%=*}"
        echo "sysctl.${k} = $(sysctl -n "${k}" 2>/dev/null || echo unknown)"
      done < <(sysctl_list "${group}")
    done
    if category_active thp; then echo "thp = $(cat "${THP_PATH}" 2>/dev/null || echo unknown)"; fi
    if category_active hugepages; then echo "nr_hugepages = $(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo unknown)"; fi
    if category_active irq; then
      echo "eth = ${ETH}"
      echo "queues = $(current_queues "${ETH}")"
      echo "irqbalance = $(systemctl is-active irqbalance 2>/dev/null || echo unknown)"
      irqs_now="$(enumerate_irqs "${ETH}")"
      [ -z "${irqs_now}" ] || read -ra cur_irqs <<< "${irqs_now}"
      for irq_cur in "${cur_irqs[@]}"; do
        echo "irq.${irq_cur}.smp_affinity_list = $(cat "/proc/irq/${irq_cur}/smp_affinity_list" 2>/dev/null || echo unknown)"
      done
    fi
  } > "${f}" 2>&1
}

# 回滚顺序：恢复队列数 → 重新定位 IRQ → 恢复 smp_affinity_list →
#          恢复 irqbalance → 恢复 sysctl → 恢复 THP/大页。
do_rollback() {
  local f="$1" line key val eth="" bdf="" irqbalance="" queues="" thp_orig="" hp_orig=""
  local irqs_now="" irq_cur mask target idx=0 fail_count=0
  local -a cur_irqs=() UNKNOWN_KEYS=()
  local -A IRQ_MASK=() LANE_MASK=() SYSCTL_ORIG=()
  local rlog="${OUTDIR}/rollback-log.$(date +%s).$$"

  mkdir -p "${OUTDIR}" || { echo "[FAIL] 无法创建 ${OUTDIR}" >&2; return 1; }
  : > "${rlog}" || { echo "[FAIL] 无法写回滚日志 ${rlog}" >&2; return 1; }

  while IFS= read -r line; do
    case "${line}" in
      "eth = "*) eth="${line#eth = }" ;;
      "bdf = "*) bdf="${line#bdf = }" ;;
      "irqbalance = "*) irqbalance="${line#irqbalance = }" ;;
      "queues = "*) queues="${line#queues = }" ;;
      "thp = "*) thp_orig="${line#thp = }" ;;
      "nr_hugepages = "*) hp_orig="${line#nr_hugepages = }" ;;
      "sysctl."*" = "*)
        key="${line#sysctl.}"; key="${key%% = *}"
        if is_known_sysctl_key "${key}"; then
          SYSCTL_ORIG["${key}"]="${line#* = }"
        else
          echo "[WARN] 备份含非本脚本 SYSCTL_* 集合的键，拒绝恢复：${key}" >&2
          UNKNOWN_KEYS+=("${key}")
        fi
        ;;
      "irq."*".smp_affinity_list = "*) key="${line#irq.}"; key="${key%%.*}"; IRQ_MASK["${key}"]="${line#* = }" ;;
      "lane."*".smp_affinity_list = "*) key="${line#lane.}"; key="${key%%.*}"; LANE_MASK["${key}"]="${line#* = }" ;;
    esac
  done < "${f}"

  # 1) 队列数
  if [ -n "${eth}" ] && [ -n "${queues}" ] && [ "${queues}" != "unknown" ]; then
    if ethtool -L "${eth}" combined "${queues}" >>"${rlog}" 2>&1; then
      echo "[INFO] 已恢复 ${eth} 队列数=${queues}"
    else
      echo "[WARN] 恢复队列数失败：${eth} -> ${queues}" >&2
      fail_count=$((fail_count + 1))
    fi
  fi

  # 2) 队列变更会重建 IRQ，必须重新从 sysfs 定位
  if [ -n "${eth}" ]; then
    irqs_now="$(enumerate_irqs "${eth}")"
    [ -z "${irqs_now}" ] || read -ra cur_irqs <<< "${irqs_now}"
  fi

  # 3) 原 IRQ 号优先；编号变化时按队列序号套用原掩码
  for irq_cur in "${cur_irqs[@]}"; do
    mask="${IRQ_MASK[${irq_cur}]:-${LANE_MASK[${idx}]:-}}"
    if [ -n "${mask}" ] && [ "${mask}" != "unknown" ]; then
      if printf '%s\n' "${mask}" > "/proc/irq/${irq_cur}/smp_affinity_list" 2>>"${rlog}"; then
        echo "[INFO] 已恢复 IRQ ${irq_cur} 亲和性=${mask}"
      else
        echo "[WARN] 恢复 IRQ ${irq_cur} 亲和性失败" >&2
        fail_count=$((fail_count + 1))
      fi
    fi
    idx=$((idx + 1))
  done

  # 4) irqbalance 原状态
  case "${irqbalance}" in
    active)
      if systemctl start irqbalance >>"${rlog}" 2>&1; then echo "[INFO] 已启动 irqbalance"; else echo "[WARN] 启动 irqbalance 失败" >&2; fail_count=$((fail_count + 1)); fi
      ;;
    inactive)
      if systemctl stop irqbalance >>"${rlog}" 2>&1; then echo "[INFO] 已停止 irqbalance"; else echo "[WARN] 停止 irqbalance 失败" >&2; fail_count=$((fail_count + 1)); fi
      ;;
    *) echo "[INFO] irqbalance 原状态=${irqbalance:-unknown}，未改动" ;;
  esac

  # 5) sysctl 原值
  for key in "${!SYSCTL_ORIG[@]}"; do
    val="${SYSCTL_ORIG[${key}]}"
    [ "${val}" != "unknown" ] || continue
    if sysctl -w "${key}=${val}" >>"${rlog}" 2>&1; then
      echo "[INFO] 已恢复 sysctl.${key}=${val}"
    else
      echo "[WARN] 恢复 sysctl.${key} 失败" >&2
      fail_count=$((fail_count + 1))
    fi
  done

  # 6) THP/大页：容器内不回写宿主内核接口
  if detect_container; then
    echo "[INFO] 容器环境：跳过 THP/大页恢复（宿主接口不可从容器修改）"
  else
    if [ -n "${thp_orig}" ] && [ "${thp_orig}" != "unknown" ]; then
      target="${thp_orig#*\[}"; target="${target%%\]*}"
      if [ -n "${target}" ] && printf '%s\n' "${target}" > "${THP_PATH}" 2>>"${rlog}"; then
        echo "[INFO] 已恢复 THP=${target}"
      else
        echo "[WARN] 恢复 THP 失败" >&2; fail_count=$((fail_count + 1))
      fi
    fi
    if [ -n "${hp_orig}" ] && [ "${hp_orig}" != "unknown" ]; then
      if printf '%s\n' "${hp_orig}" > /proc/sys/vm/nr_hugepages 2>>"${rlog}"; then
        echo "[INFO] 已恢复 nr_hugepages=${hp_orig}"
      else
        echo "[WARN] 恢复 nr_hugepages 失败" >&2; fail_count=$((fail_count + 1))
      fi
    fi
  fi

  if [ "${#UNKNOWN_KEYS[@]}" -gt 0 ]; then
    echo "[FAIL] 备份含 ${#UNKNOWN_KEYS[@]} 个未知 sysctl 键，未执行：${UNKNOWN_KEYS[*]}" >&2
    fail_count=$((fail_count + ${#UNKNOWN_KEYS[@]}))
  fi

  if [ "${fail_count}" -gt 0 ]; then
    echo "[FAIL] 回滚有 ${fail_count} 项失败，详见 ${rlog}" >&2
    return 1
  fi
  echo "[INFO] 回滚完成，日志：${rlog}"
  return 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --approved-change-id) need_value "$1" "${2:-}"; CHANGE_ID="$2"; shift 2 ;;
    --profile) need_value "$1" "${2:-}"; PROFILE="$2"; shift 2 ;;
    --category) need_value "$1" "${2:-}"; CATEGORY="$2"; shift 2 ;;
    --eth) need_value "$1" "${2:-}"; ETH="$2"; shift 2 ;;
    --irq-cores) need_value "$1" "${2:-}"; IRQ_CORES="$2"; shift 2 ;;
    --queues) need_value "$1" "${2:-}"; QUEUES="$2"; shift 2 ;;
    --thp) need_value "$1" "${2:-}"; THP="$2"; shift 2 ;;
    --hugepages) need_value "$1" "${2:-}"; HUGEPAGES="$2"; shift 2 ;;
    --outdir) need_value "$1" "${2:-}"; OUTDIR="$2"; shift 2 ;;
    --rollback) need_value "$1" "${2:-}"; ROLLBACK_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -n "${ROLLBACK_FILE}" ]; then
  [ -f "${ROLLBACK_FILE}" ] || { echo "[FAIL] 备份文件不存在：${ROLLBACK_FILE}" >&2; exit 1; }
  validate_free_text --rollback "${ROLLBACK_FILE}" || exit 2
  do_rollback "${ROLLBACK_FILE}"
  exit $?
fi

if [ -n "${CATEGORY}" ]; then
  normalize_categories "${CATEGORY}"
else
  derive_categories
fi
# 显式选类时，忽略未选中类别的参数，避免“只想改 THP 却写了网络 sysctl”。
if [ -n "${CATEGORY}" ]; then
  if [ -n "${THP}" ] && ! category_active thp; then echo "[WARN] --thp 不在所选类别，已忽略" >&2; THP=""; fi
  if [ -n "${HUGEPAGES}" ] && ! category_active hugepages; then echo "[WARN] --hugepages 不在所选类别，已忽略" >&2; HUGEPAGES=""; fi
  if { [ -n "${QUEUES}" ] || [ -n "${IRQ_CORES}" ]; } && ! category_active irq; then
    echo "[WARN] --queues/--irq-cores 不在所选类别，已忽略" >&2; QUEUES=""; IRQ_CORES=""
  fi
fi

validate_inputs || exit 1
check_container_guard || exit 1

echo "== MySQL OS optimization =="
echo "PROFILE=${PROFILE} EXECUTE=${EXECUTE} CHANGE_ID=${CHANGE_ID:-<none>} CATEGORY=${ACTIVE}"
echo "ETH=${ETH:-<none>} IRQ_CORES=${IRQ_CORES:-<none>} QUEUES=${QUEUES:-<none>}"
echo "THP=${THP:-<keep>} HUGEPAGES=${HUGEPAGES:-<keep>} OUTDIR=${OUTDIR}"
echo

echo "计划（仅类别：${ACTIVE}；不停止宿主防火墙）："
for group in net io memory scheduler; do
  category_active "${group}" || continue
  while IFS= read -r kv; do
    [ -n "${kv}" ] && echo "  sysctl -w ${kv}"
  done < <(sysctl_list "${group}")
done
[ -n "${THP}" ] && echo "  THP -> ${THP}"
[ -n "${HUGEPAGES}" ] && echo "  nr_hugepages -> ${HUGEPAGES}"
if category_active irq && [ -n "${ETH}" ] && [ -n "${QUEUES}" ]; then echo "  ethtool -L ${ETH} combined ${QUEUES}"; fi
if category_active irq && [ -n "${ETH}" ] && [ -n "${IRQ_CORES}" ]; then
  echo "  从 /sys/class/net/${ETH}/device 枚举 IRQ，核实 BDF 与 IRQ 集合非空"
  echo "  确认后才停 irqbalance；逐队列写 smp_affinity_list"
fi
echo

if [ "${EXECUTE}" -eq 1 ] && [ -z "${CHANGE_ID}" ]; then
  echo "[FAIL] --execute 需要 --approved-change-id CHG-001" >&2
  exit 1
fi

if [ "${EXECUTE}" -eq 0 ]; then
  echo "Dry-run 结束。真正执行：--execute --approved-change-id CHG-001"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "[FAIL] 执行需要 root" >&2
  exit 1
fi

# 任何写入前先核实网卡 BDF 与 IRQ 集合：空集合直接退出，不碰 irqbalance/亲和性。
if category_active irq; then
  BDF_PRE="$(get_bdf "${ETH}")"
  [ -n "${BDF_PRE}" ] || { echo "[FAIL] ${ETH} 的 BDF 为空，退出（不修改任何 IRQ）" >&2; exit 1; }
  IRQS_PRE="$(enumerate_irqs "${ETH}")"
  [ -n "${IRQS_PRE}" ] || { echo "[FAIL] ${ETH} 的 sysfs 未枚举到 IRQ，退出（不停止 irqbalance/不写亲和性）" >&2; exit 1; }
fi

mkdir -p "${OUTDIR}" || { echo "[FAIL] 无法创建 ${OUTDIR}" >&2; exit 1; }
# 每次运行独占备份；重试不会截断首次备份。
BACKUP_FILE="$(mktemp "${OUTDIR}/os-before.XXXXXX")" || { echo "[FAIL] 无法创建备份文件" >&2; exit 1; }
write_backup "${BACKUP_FILE}" || { rm -f "${BACKUP_FILE}"; echo "[FAIL] 保存原值失败，未做任何系统变更" >&2; exit 1; }
validate_backup "${BACKUP_FILE}" || { rm -f "${BACKUP_FILE}"; echo "[FAIL] 原值未完整保存，未做任何系统变更" >&2; exit 1; }
echo "[INFO] 原值已完整备份到 ${BACKUP_FILE}"

APPLY_LOG="$(mktemp "${OUTDIR}/apply-log.XXXXXX")" || { echo "[FAIL] 无法创建 apply 日志" >&2; exit 1; }
AFTER_FILE="$(mktemp "${OUTDIR}/os-after.XXXXXX")" || { echo "[FAIL] 无法创建 after 文件" >&2; exit 1; }

declare -a APPLIED_ITEMS=() FAILED_ITEMS=() RESTORE_ITEMS=()

# 1) sysctl：逐项写入并回读目标值
for group in net io memory scheduler; do
  category_active "${group}" || continue
  while IFS= read -r kv; do
    [ -n "${kv}" ] || continue
    key="${kv%%=*}"; target="${kv#*=}"
    if sysctl -w "${kv}" >>"${APPLY_LOG}" 2>&1; then
      actual="$(sysctl -n "${key}" 2>/dev/null || echo unknown)"
      if [ "${actual}" = "${target}" ]; then
        APPLIED_ITEMS+=("sysctl.${key}=${actual}")
        RESTORE_ITEMS+=("sysctl.${key}（原值见备份）")
      else
        FAILED_ITEMS+=("sysctl.${key} 回读=${actual} 期望=${target}")
      fi
    else
      FAILED_ITEMS+=("sysctl.${key} 写入失败（内核可能不支持）")
    fi
  done < <(sysctl_list "${group}")
done

# 2) THP
if category_active thp && [ -n "${THP}" ]; then
  if printf '%s\n' "${THP}" > "${THP_PATH}" 2>>"${APPLY_LOG}"; then
    actual="$(cat "${THP_PATH}" 2>/dev/null || echo unknown)"
    if printf '%s' "${actual}" | grep -qF "[${THP}]"; then
      APPLIED_ITEMS+=("THP=${THP}")
      RESTORE_ITEMS+=("THP（原值见备份）")
    else
      FAILED_ITEMS+=("THP 回读=${actual} 期望=[${THP}]")
    fi
  else
    FAILED_ITEMS+=("THP 写入失败")
  fi
fi

# 3) 大页
if category_active hugepages && [ -n "${HUGEPAGES}" ]; then
  if printf '%s\n' "${HUGEPAGES}" > /proc/sys/vm/nr_hugepages 2>>"${APPLY_LOG}"; then
    actual="$(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo unknown)"
    if [ "${actual}" = "${HUGEPAGES}" ]; then
      APPLIED_ITEMS+=("nr_hugepages=${actual}")
      RESTORE_ITEMS+=("nr_hugepages（原值见备份）")
    else
      FAILED_ITEMS+=("nr_hugepages 回读=${actual} 期望=${HUGEPAGES}")
    fi
  else
    FAILED_ITEMS+=("nr_hugepages 写入失败")
  fi
fi

# 4) 队列数（改队列会重建 IRQ，之后必须重新枚举）
if category_active irq && [ -n "${QUEUES}" ]; then
  if ethtool -L "${ETH}" combined "${QUEUES}" >>"${APPLY_LOG}" 2>&1; then
    actual="$(current_queues "${ETH}")"
    if [ "${actual}" = "${QUEUES}" ]; then
      APPLIED_ITEMS+=("${ETH} 队列数=${actual}")
      RESTORE_ITEMS+=("${ETH} 队列数（原值见备份）")
    else
      FAILED_ITEMS+=("${ETH} 队列数回读=${actual:-unknown} 期望=${QUEUES}")
    fi
  else
    FAILED_ITEMS+=("${ETH} 改队列失败（驱动可能不支持/需 down-up）")
  fi
fi

# 5) IRQ 亲和性：先重枚举，集合为空则退出这一段；确认后才停 irqbalance
if category_active irq && [ -n "${IRQ_CORES}" ]; then
  IRQS_NOW="$(enumerate_irqs "${ETH}")"
  if [ -z "${IRQS_NOW}" ]; then
    FAILED_ITEMS+=("${ETH} 重枚举 IRQ 集合为空：未停止 irqbalance、未写亲和性")
  else
    if systemctl stop irqbalance >>"${APPLY_LOG}" 2>&1; then
      APPLIED_ITEMS+=("irqbalance 已停止")
      RESTORE_ITEMS+=("irqbalance 状态（原值见备份）")
      read -ra IRQ_ARR <<< "${IRQS_NOW}"
      IFS=',' read -ra CORE_PARTS <<< "${IRQ_CORES}"
      for ((i=0; i<${#IRQ_ARR[@]}; i++)); do
        irq="${IRQ_ARR[$i]}"
        part="${CORE_PARTS[$((i % ${#CORE_PARTS[@]}))]}"
        if [[ "${part}" == *-* ]]; then
          s="${part%-*}"; e="${part#*-}"
          core=$((10#${s} + (i % (10#${e} - 10#${s} + 1))))
        else
          core=$((10#${part}))
        fi
        if printf '%s\n' "${core}" > "/proc/irq/${irq}/smp_affinity_list" 2>>"${APPLY_LOG}"; then
          actual="$(cat "/proc/irq/${irq}/smp_affinity_list" 2>/dev/null || echo unknown)"
          if [ "${actual}" = "${core}" ]; then
            APPLIED_ITEMS+=("IRQ ${irq} -> CPU ${core}")
            RESTORE_ITEMS+=("IRQ ${irq} 亲和性（原值见备份）")
          else
            FAILED_ITEMS+=("IRQ ${irq} 回读=${actual} 期望=${core}")
          fi
        else
          FAILED_ITEMS+=("IRQ ${irq} 写入失败")
        fi
      done
    else
      FAILED_ITEMS+=("停止 irqbalance 失败：跳过 IRQ 亲和性写入")
    fi
  fi
fi

write_after "${AFTER_FILE}"

echo "[INFO] 已生效项（${#APPLIED_ITEMS[@]}）："
for item in "${APPLIED_ITEMS[@]}"; do echo "  - ${item}"; done
echo "[INFO] 待恢复项（原值见 ${BACKUP_FILE}）："
for item in "${RESTORE_ITEMS[@]}"; do echo "  - ${item}"; done

if [ "${#FAILED_ITEMS[@]}" -gt 0 ]; then
  echo "[FAIL] 部分项目未达目标（${#FAILED_ITEMS[@]} 项）：" >&2
  for item in "${FAILED_ITEMS[@]}"; do echo "  - ${item}" >&2; done
  echo "[INFO] 回滚命令：bash $0 --rollback ${BACKUP_FILE}" >&2
  echo "[INFO] 备份/日志：${BACKUP_FILE} ${APPLY_LOG} ${AFTER_FILE}" >&2
  exit 1
fi

echo "[INFO] 全部目标值回读一致。备份/日志：${BACKUP_FILE} ${APPLY_LOG} ${AFTER_FILE}"
echo "[INFO] 回滚：bash $0 --rollback ${BACKUP_FILE}（队列→重定位 IRQ→原掩码→irqbalance→sysctl→内存页）"
exit 0
