#!/usr/bin/env bash

# 容器部署前只读检查：docker、cpuset/mems、内存、端口、镜像、挂载、nofile。

# 用法：

# CPUSET=80-87 MEMS=1 MEM_LIMIT_GB=32 MYSQL_PORT=3306 IMAGE_NAME=mysql-base \

  # MYSQL_HOME=/opt/mysql-8.0.25 DATA_ROOT=/data/mysql \

  # bash scripts/preflight_container.sh

# 退出码：0=无 FAIL；1=存在 FAIL。

set -u

CPUSET="${CPUSET:-}"
MEMS="${MEMS:-}"
MEM_LIMIT_GB="${MEM_LIMIT_GB:-0}"
MYSQL_PORT="${MYSQL_PORT:-3306}"

# 端口范围校验：65536 等能过纯数字校验但实例无法绑定，探测无意义——按用法错误拒绝
case "${MYSQL_PORT}" in ''|*[!0-9]*) echo "FAIL  MYSQL_PORT 须为数字: ${MYSQL_PORT}" >&2; exit 2 ;; esac
if [ "${MYSQL_PORT}" -lt 1 ] || [ "${MYSQL_PORT}" -gt 65535 ]; then
  echo "FAIL  MYSQL_PORT 超出合法范围 1-65535: ${MYSQL_PORT}" >&2
  exit 2
fi
IMAGE_NAME="${IMAGE_NAME:-}"
MYSQL_HOME="${MYSQL_HOME:-}"
DATA_ROOT="${DATA_ROOT:-}"
CONTAINER_NAME="${CONTAINER_NAME:-mysql}"

fail=0
warn=0
pass() { printf '[PASS] %s\n' "$*"; }
note() { printf '[WARN] %s\n' "$*"; warn=$((warn + 1)); }
bad()  { printf '[FAIL] %s\n' "$*"; fail=$((fail + 1)); }

echo "== MySQL container preflight (read-only) =="
echo "CPUSET=${CPUSET:-<unset>} MEMS=${MEMS:-<unset>} MEM_LIMIT_GB=${MEM_LIMIT_GB}"
echo "IMAGE_NAME=${IMAGE_NAME:-<unset>} CONTAINER_NAME=${CONTAINER_NAME}"
echo

# docker --version 只证明客户端存在；守护进程未启动或账号无权限时仍会成功，必须单独探测 daemon
docker_ok=0
if command -v docker >/dev/null 2>&1; then
  pass "docker 客户端存在：$(docker --version 2>/dev/null | head -n1)"
  if docker info >/dev/null 2>&1; then
    docker_ok=1
    pass "docker 守护进程可访问"
  else
    bad "docker 守护进程不可访问（未启动或当前账号无权限）"
  fi
else
  bad "缺少 docker"
fi

if command -v supermin >/dev/null 2>&1; then
  pass "supermin 存在"
else
  note "缺少 supermin（若使用现有镜像可忽略）"
fi

# cpuset 校验

if [ -n "${CPUSET}" ]; then
  max_cpu=$(( $(nproc 2>/dev/null || echo 1) - 1 ))
  ok=1
  IFS=',' read -ra parts <<< "${CPUSET}"
  for p in "${parts[@]}"; do
    if [[ "$p" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      a="${BASH_REMATCH[1]}"; b="${BASH_REMATCH[2]}"
      if [ "$a" -lt 0 ] || [ "$b" -gt "$max_cpu" ] || [ "$a" -gt "$b" ]; then ok=0; fi
    elif [[ "$p" =~ ^[0-9]+$ ]]; then
      if [ "$p" -gt "$max_cpu" ]; then ok=0; fi
    else
      ok=0
    fi
  done
  if [ "$ok" -eq 1 ]; then pass "CPUSET 在宿主 CPU 范围内"; else bad "CPUSET=${CPUSET} 越界或格式错误（宿主 0-${max_cpu}）"; fi
else
  note "未设置 CPUSET，跳过 CPU 校验"
fi

# NUMA 校验：docker --cpuset-mems 接受逗号列表与区间（0,1 / 0-3），逐项校验，不当作单个数字

if [ -n "${MEMS}" ]; then
  nodes="$(ls -d /sys/devices/system/node/node* 2>/dev/null | wc -l | tr -d ' ')"
  max_node=$((nodes - 1))
  ok=1
  IFS=',' read -ra mem_parts <<< "${MEMS}"
  for p in "${mem_parts[@]}"; do
    if [[ "$p" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      a="${BASH_REMATCH[1]}"; b="${BASH_REMATCH[2]}"
      if [ "$a" -gt "$max_node" ] || [ "$b" -gt "$max_node" ] || [ "$a" -gt "$b" ]; then ok=0; fi
    elif [[ "$p" =~ ^[0-9]+$ ]]; then
      if [ "$p" -gt "$max_node" ]; then ok=0; fi
    else
      ok=0
    fi
  done
  if [ "$nodes" -eq 0 ]; then
    note "无法枚举宿主 NUMA 节点，跳过 MEMS 范围校验"
  elif [ "$ok" -eq 1 ]; then
    pass "MEMS=${MEMS} 在 NUMA 节点范围（共 ${nodes}，0-${max_node}）"
  else
    bad "MEMS=${MEMS} 越界或格式错误（宿主 NUMA 0-${max_node}，共 ${nodes} 个节点）"
  fi
else
  note "未设置 MEMS；容器可能跨 NUMA 分配内存"
fi

# 内存

mem_kb="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
mem_gb=$((mem_kb / 1024 / 1024))
if [ "${MEM_LIMIT_GB}" -gt 0 ] && [ "${MEM_LIMIT_GB}" -gt "${mem_gb}" ]; then
  bad "MEM_LIMIT_GB=${MEM_LIMIT_GB} 超过宿主内存 ${mem_gb} GiB"
elif [ "${MEM_LIMIT_GB}" -gt 0 ]; then
  pass "内存限制 ${MEM_LIMIT_GB} GiB ≤ 宿主 ${mem_gb} GiB"
else
  note "未设置 MEM_LIMIT_GB"
fi

# 端口

if command -v ss >/dev/null 2>&1; then
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -q ":${MYSQL_PORT}$"; then
    bad "端口 ${MYSQL_PORT} 已被监听（host 网络会冲突）"
  else
    pass "端口 ${MYSQL_PORT} 空闲"
  fi
fi

# 镜像与运行中容器：仅在守护进程可访问时判定名称冲突；查询失败记 FAIL，不能当成名字可用

if [ "${docker_ok}" -eq 1 ]; then
  if [ -n "${IMAGE_NAME}" ]; then
    if docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
      pass "镜像存在：${IMAGE_NAME}"
    else
      note "镜像不存在，需要先制作/导入：${IMAGE_NAME}"
    fi
  fi
  if container_list="$(docker ps -a --format '{{.Names}}' 2>/dev/null)"; then
    if printf '%s\n' "${container_list}" | grep -qx "${CONTAINER_NAME}"; then
      note "同名容器已存在：${CONTAINER_NAME}（将复用或需先删除）"
    else
      pass "容器名可用：${CONTAINER_NAME}"
    fi
  else
    bad "docker ps -a 查询失败，无法确认容器名是否与现存容器冲突"
  fi
else
  note "docker 守护进程不可访问，跳过镜像/容器名检查"
fi

# 挂载目录

for d in "${MYSQL_HOME}" "${DATA_ROOT}"; do
  if [ -z "$d" ]; then continue; fi
  if [ -d "$d" ]; then
    pass "挂载目录存在：$d"
  else
    note "挂载目录不存在（需创建）：$d"
  fi
done
# nofile

ulimit_n="$(ulimit -n 2>/dev/null || echo unknown)"
echo "[INFO] host ulimit -n=${ulimit_n}"
if [ "${ulimit_n}" != "unlimited" ] && [ "${ulimit_n:-0}" -lt 102400 ] 2>/dev/null; then
  note "宿主 ulimit -n=${ulimit_n} < 102400；容器 --ulimit nofile 需显式设置"
fi

# 大页提示

hp="$(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo unknown)"
echo "[INFO] nr_hugepages=${hp}"

echo
echo "== summary: FAIL=${fail} WARN=${warn} =="
[ "$fail" -gt 0 ] && { echo "存在 FAIL，禁止启动容器。"; exit 1; }
echo "无 FAIL，可继续；请确认 WARN。"
exit 0
