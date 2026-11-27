#!/usr/bin/env bash

# 虚机部署前只读检查：libvirt、KVM、大页、NUMA、网桥、镜像、端口。

# 用法：

# VM_NAME=mysql-8u32g VM_CPUS=8 VM_MEM_GB=32 VM_NODE=1 BRIDGE=br0 \

  # VM_IMAGE=/data/vm/mysql-8u32g.qcow2 MYSQL_PORT=3306 \

  # bash scripts/preflight_vm.sh

# 退出码：0=无 FAIL；1=存在 FAIL；2=入参非法（端口/数字参数/范围/超长整数）。

set -u

VM_NAME="${VM_NAME:-mysql-8u32g}"
VM_CPUS="${VM_CPUS:-8}"
VM_MEM_GB="${VM_MEM_GB:-32}"
VM_NODE="${VM_NODE:-0}"
BRIDGE="${BRIDGE:-br0}"
VM_IMAGE="${VM_IMAGE:-}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
PAGE_KB="${PAGE_KB:-1048576}"    # 与 libvirt XML <page size> 一致（1048576=1 GiB）

# 数字入参校验：非数字/超长整数/越界一律 exit 2，不能吞掉比较错误后静默判 PASS。
require_uint() {
  local name="$1" value="$2" min="$3"
  case "${value}" in ''|*[!0-9]*) echo "FAIL  ${name} 须为非负整数: ${value}" >&2; exit 2 ;; esac
  if [ "${#value}" -gt 9 ]; then
    echo "FAIL  ${name} 数值过大: ${value}" >&2
    exit 2
  fi
  if [ "${value}" -lt "${min}" ]; then
    echo "FAIL  ${name} 须 ≥ ${min}: ${value}" >&2
    exit 2
  fi
}
require_uint VM_CPUS "${VM_CPUS}" 1
require_uint VM_NODE "${VM_NODE}" 0
require_uint VM_MEM_GB "${VM_MEM_GB}" 1
require_uint PAGE_KB "${PAGE_KB}" 1

# 10# 前缀避免 08 这类前导零被当八进制导致算术展开报错。
HUGEPAGE_COUNT=$(( (10#${VM_MEM_GB} * 1024 * 1024 + 10#${PAGE_KB} - 1) / 10#${PAGE_KB} ))

# 端口范围校验：65536 等能过纯数字校验但实例无法绑定，探测无意义——按用法错误拒绝
case "${MYSQL_PORT}" in ''|*[!0-9]*) echo "FAIL  MYSQL_PORT 须为数字: ${MYSQL_PORT}" >&2; exit 2 ;; esac
if [ "${MYSQL_PORT}" -lt 1 ] || [ "${MYSQL_PORT}" -gt 65535 ]; then
  echo "FAIL  MYSQL_PORT 超出合法范围 1-65535: ${MYSQL_PORT}" >&2
  exit 2
fi

fail=0
warn=0
pass() { printf '[PASS] %s\n' "$*"; }
note() { printf '[WARN] %s\n' "$*"; warn=$((warn + 1)); }
bad()  { printf '[FAIL] %s\n' "$*"; fail=$((fail + 1)); }

echo "== MySQL VM preflight (read-only) =="
echo "VM_NAME=${VM_NAME} vCPU=${VM_CPUS} Mem=${VM_MEM_GB}G NUMA=${VM_NODE} BRIDGE=${BRIDGE} PAGE_KB=${PAGE_KB} need=${HUGEPAGE_COUNT}"
echo

if command -v virsh >/dev/null 2>&1; then
  pass "virsh 存在"
  if virsh list --all >/dev/null 2>&1; then pass "libvirtd 可访问"; else bad "libvirtd 不可访问（未启动/权限）"; fi
else
  bad "缺少 virsh/libvirt"
fi

if [ -e /dev/kvm ]; then pass "/dev/kvm 存在"; else note "/dev/kvm 不存在，可能无硬件加速"; fi

if command -v qemu-kvm >/dev/null 2>&1 || [ -x /usr/libexec/qemu-kvm ]; then
  pass "qemu-kvm 存在"
else
  note "未找到 qemu-kvm"
fi

# CPU / NUMA

cores="$(nproc 2>/dev/null || echo 0)"
nodes="$(ls -d /sys/devices/system/node/node* 2>/dev/null | wc -l | tr -d ' ')"
echo "[INFO] host logical CPU=${cores} NUMA nodes=${nodes}"
if [ "${VM_CPUS}" -gt "${cores}" ] 2>/dev/null; then
  bad "VM_CPUS=${VM_CPUS} 超过宿主逻辑 CPU ${cores}"
else
  pass "vCPU ${VM_CPUS} ≤ 宿主 ${cores}"
fi
if [ "${VM_NODE}" -ge "${nodes}" ] 2>/dev/null; then
  bad "VM_NODE=${VM_NODE} 超出 NUMA 节点数 ${nodes}"
else
  pass "VM_NODE=${VM_NODE} 有效"
fi

# 内存

mem_kb="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
mem_gb=$((mem_kb / 1024 / 1024))
if [ "${VM_MEM_GB}" -gt "${mem_gb}" ]; then
  bad "VM_MEM_GB=${VM_MEM_GB} 超过宿主内存 ${mem_gb} GiB"
else
  pass "VM 内存 ${VM_MEM_GB} GiB ≤ 宿主 ${mem_gb} GiB"
fi

# 大页

# 大页必须按 XML 的页大小读节点 sysfs：/proc/meminfo 的 HugePages_Total 只统计
# 内核默认 hstate，1 GiB 节点级大页不计入，不能用它判断本部署的大页是否足够。
hp_node_dir="/sys/devices/system/node/node${VM_NODE}/hugepages/hugepages-${PAGE_KB}kB"
hp_nr="$(cat "${hp_node_dir}/nr_hugepages" 2>/dev/null || echo 0)"
hp_free="$(cat "${hp_node_dir}/free_hugepages" 2>/dev/null || echo 0)"
echo "[INFO] node${VM_NODE} hugepages-${PAGE_KB}kB: nr=${hp_nr} free=${hp_free} need=${HUGEPAGE_COUNT}"
if [ ! -f "${hp_node_dir}/nr_hugepages" ]; then
  bad "宿主 node${VM_NODE} 不支持 ${PAGE_KB} KiB 大页（无 ${hp_node_dir}/nr_hugepages）"
elif [ "${hp_nr}" -lt "${HUGEPAGE_COUNT}" ]; then
  bad "宿主 node${VM_NODE} 仅预留 ${hp_nr} 个 ${PAGE_KB} KiB 大页，需 ≥ ${HUGEPAGE_COUNT}；按 vm-deploy.md §4 写 nr_hugepages"
elif [ "${hp_free}" -lt "${HUGEPAGE_COUNT}" ]; then
  bad "宿主 node${VM_NODE} 空闲 ${PAGE_KB} KiB 大页仅 ${hp_free}，需 ≥ ${HUGEPAGE_COUNT}（可能被其它虚机占用）"
else
  pass "宿主 node${VM_NODE} ${PAGE_KB} KiB 大页 free=${hp_free} nr=${hp_nr} ≥ ${HUGEPAGE_COUNT}"
fi

# 网桥

if command -v ip >/dev/null 2>&1; then
  if ip link show "${BRIDGE}" >/dev/null 2>&1; then pass "网桥存在：${BRIDGE}"; else note "网桥不存在：${BRIDGE}"; fi
fi

# 镜像

if [ -n "${VM_IMAGE}" ]; then
  if [ -f "${VM_IMAGE}" ]; then
    pass "镜像存在：${VM_IMAGE}"
    avail_gb="$(df -Pk "$(dirname "${VM_IMAGE}")" 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}')"
    [ "${avail_gb:-0}" -ge 50 ] && pass "镜像目录可用 ${avail_gb} GiB" || note "镜像目录可用 ${avail_gb:-0} GiB"
  else
    note "镜像不存在（需下载/创建）：${VM_IMAGE}"
  fi
else
  note "未设置 VM_IMAGE"
fi

# 端口

if command -v ss >/dev/null 2>&1; then
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -q ":${MYSQL_PORT}$"; then
    note "宿主已有 ${MYSQL_PORT} 监听；若 guest 桥接同端口需确认隔离"
  else
    pass "宿主端口 ${MYSQL_PORT} 空闲"
  fi
fi

# 已有同名虚机

if command -v virsh >/dev/null 2>&1 && virsh list --all 2>/dev/null | grep -qw "${VM_NAME}"; then
  note "同名虚机已存在：${VM_NAME}（将复用或需先 undefine）"
else
  pass "虚机名可用：${VM_NAME}"
fi

echo
echo "== summary: FAIL=${fail} WARN=${warn} =="
[ "$fail" -gt 0 ] && { echo "存在 FAIL，禁止创建/启动虚机。"; exit 1; }
echo "无 FAIL，可继续；请确认 WARN（尤其大页与网桥）。"
exit 0
