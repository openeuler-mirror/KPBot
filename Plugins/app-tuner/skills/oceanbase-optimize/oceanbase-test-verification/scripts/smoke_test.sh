#!/usr/bin/env bash

# OceanBase 只读冒烟：版本、节点、Zone、租户、Unit、参数、可选业务表抽样。

#

# 用法（只读，不改数据）：

# OB_HOST=<host> OB_PORT=2881 OB_USER='root@sys' OB_DB=oceanbase \
#   （2881 是社区 observer mysql_port 默认值；实际端口以 install.cfg 为准）

  # bash smoke_test.sh

#

# 可选：

# 业务层（系统租户与业务租户分开连接；SPACE_DB 设置时 BIZ_USER 必填）：

# BIZ_USER='obtest@mysql_tenant' BIZ_DB=obtest BIZ_PASSWORD=<pw>
#   直连 observer 用 user@tenant（不加 #集群名）；经 obproxy 才用 user@tenant#cluster

# SPACE_DB=<db> SAMPLE_TABLE=<table> SAMPLE_COL=<显式列名,默认不选列>

# EXPECT_SERVERS=<n> EXPECT_ZONES=<n> EXPECT_TENANTS=<n>

# EXPECT_TABLE_COUNT=<n> EXPECT_ROWS=<n>

# 写可用性：EXPECT_WRITABLE=1 必须显式给 BIZ_USER/BIZ_PASSWORD（业务租户），
# 用业务连接核 read_only；系统租户的 read_only 不能代表写目标租户。

#

# 退出码：0 全部通过；1 有检查失败；2 缺少依赖/配置（含非法端口/标识符）。

set -u

OB_HOST="${OB_HOST:-127.0.0.1}"
OB_PORT="${OB_PORT:-2881}"   # 社区 observer mysql_port 默认值；实际以 install.cfg 为准
case "${OB_PORT}" in ''|*[!0-9]*) echo "FAIL  OB_PORT 须为数字: ${OB_PORT}" >&2; exit 2 ;; esac
if [ "${OB_PORT}" -lt 1 ] || [ "${OB_PORT}" -gt 65535 ]; then
  echo "FAIL  OB_PORT 超出合法范围 1-65535: ${OB_PORT}" >&2
  exit 2
fi
OB_USER="${OB_USER:-root@sys}"
OB_DB="${OB_DB:-oceanbase}"
OB_PASSWORD="${OB_PASSWORD:-}"
SPACE_DB="${SPACE_DB:-}"
# 业务租户账号（必须与系统租户分开）；库名默认取 SPACE_DB
BIZ_USER="${BIZ_USER:-}"
BIZ_DB="${BIZ_DB:-${SPACE_DB}}"
BIZ_PASSWORD="${BIZ_PASSWORD:-}"
SAMPLE_TABLE="${SAMPLE_TABLE:-}"
# 默认不选列：抽样只判“可读”，不把业务表原始行写进验收报告
SAMPLE_COL="${SAMPLE_COL:-}"

# 输入校验前置：非法参数必须在任何 obclient 调用之前拒绝（用法错误 exit 2）
valid_ident() {
  # SQL 标识符只接受字母/数字/下划线且不以数字开头；
  # 拒绝反引号/引号/空白，避免双引号中的反引号触发 Bash 命令替换
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

valid_col_list() {
  # 抽样列须显式给出，不接受 * / 反引号 / 表达式
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*([[:space:]]*,[[:space:]]*[A-Za-z_][A-Za-z0-9_]*)*$ ]]
}

if [ -n "${SPACE_DB}" ]; then
  if [ -z "${BIZ_USER}" ]; then
    echo "FAIL  设置 SPACE_DB 时必须显式指定业务租户账号 BIZ_USER（直连 observer 用 'obtest@mysql_tenant'；经 obproxy 才用 'obtest@mysql_tenant#obcluster'）" >&2
    exit 2
  fi
  if ! valid_ident "${SPACE_DB}"; then
    echo "FAIL  SPACE_DB 不是合法 SQL 标识符: ${SPACE_DB}" >&2
    exit 2
  fi
  if [ -n "${SAMPLE_TABLE}" ] && ! valid_ident "${SAMPLE_TABLE}"; then
    echo "FAIL  SAMPLE_TABLE 不是合法 SQL 标识符: ${SAMPLE_TABLE}" >&2
    exit 2
  fi
  if [ -n "${SAMPLE_COL}" ] && ! valid_col_list "${SAMPLE_COL}"; then
    echo "FAIL  SAMPLE_COL 须为显式列名列表（不接受 * / 反引号 / 表达式）: ${SAMPLE_COL}" >&2
    exit 2
  fi
fi

# 写验收必须指向可写的业务租户：系统租户的 read_only 不代表写目标
if [ "${EXPECT_WRITABLE:-0}" = "1" ]; then
  if [ -z "${BIZ_USER}" ] || [ -z "${BIZ_PASSWORD}" ]; then
    echo "FAIL  EXPECT_WRITABLE=1 必须显式指定业务租户账号与口令 BIZ_USER/BIZ_PASSWORD（写验收目标为业务租户）" >&2
    exit 2
  fi
fi

FAILS=0
CHECKS=0

need_cmd() { command -v "$1" >/dev/null 2>&1; }

pass() { CHECKS=$((CHECKS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { CHECKS=$((CHECKS + 1)); FAILS=$((FAILS + 1)); printf 'FAIL  %s\n' "$*"; }
info() { printf 'INFO  %s\n' "$*"; }

if ! need_cmd obclient; then
  echo "FAIL  obclient 未安装（需要 OceanBase 客户端）" >&2
  exit 2
fi

DEFAULTS_FILE=""
BIZ_DEFAULTS_FILE=""
cleanup() {
  if [ -n "${DEFAULTS_FILE}" ] && [ -f "${DEFAULTS_FILE}" ]; then
    rm -f "${DEFAULTS_FILE}"
  fi
  if [ -n "${BIZ_DEFAULTS_FILE}" ] && [ -f "${BIZ_DEFAULTS_FILE}" ]; then
    rm -f "${BIZ_DEFAULTS_FILE}"
  fi
}
trap cleanup EXIT INT TERM

make_defaults() {
  # $1=口令；只写 0600 临时 defaults-file，不进 argv/日志；无口令输出空串
  local pw="$1" f=""
  if [ -n "${pw}" ]; then
    umask 077
    f="$(mktemp)"
    {
      printf '[client]\n'
      printf 'password=%s\n' "${pw}"
    } > "${f}"
  fi
  printf '%s' "${f}"
}

DEFAULTS_FILE="$(make_defaults "${OB_PASSWORD}")"
BIZ_DEFAULTS_FILE="$(make_defaults "${BIZ_PASSWORD}")"

# 系统租户连接（部署/元数据验收）
q() {
  local sql="$1"
  if [ -n "${DEFAULTS_FILE}" ]; then
    # --defaults-extra-file 必须放在其它客户端选项之前，否则客户端会忽略该配置
    obclient --defaults-extra-file="${DEFAULTS_FILE}" -h "${OB_HOST}" -P "${OB_PORT}" \
      -u "${OB_USER}" -D "${OB_DB}" -N -B -e "${sql}" 2>/dev/null
  else
    obclient -h "${OB_HOST}" -P "${OB_PORT}" -u "${OB_USER}" -D "${OB_DB}" \
      -N -B -e "${sql}" 2>/dev/null
  fi
}

# 业务租户连接（业务库计数/抽样）：与系统租户分开，BIZ_USER 必须显式指定
qb() {
  local sql="$1"
  if [ -n "${BIZ_DEFAULTS_FILE}" ]; then
    # 同上：defaults-file 必须首位，保证业务租户口令真的被客户端读取
    obclient --defaults-extra-file="${BIZ_DEFAULTS_FILE}" -h "${OB_HOST}" -P "${OB_PORT}" \
      -u "${BIZ_USER}" -D "${BIZ_DB}" -N -B -e "${sql}" 2>/dev/null
  else
    obclient -h "${OB_HOST}" -P "${OB_PORT}" -u "${BIZ_USER}" -D "${BIZ_DB}" \
      -N -B -e "${sql}" 2>/dev/null
  fi
}

echo "== OceanBase smoke (read-only) =="
info "sys target=${OB_HOST}:${OB_PORT} user=${OB_USER} db=${OB_DB}"
if [ -n "${BIZ_USER}" ]; then
  info "biz target=${OB_HOST}:${OB_PORT} user=${BIZ_USER} db=${BIZ_DB}"
fi

# 0. 连通性

if VERSION="$(q 'SELECT VERSION();')"; then
  if [ -n "${VERSION}" ]; then
    pass "连接成功，VERSION=${VERSION}"
  else
    fail "连接成功但 VERSION 为空"
  fi
else
  fail "无法连接（检查 host/port/user/tenant/端口映射）"
  echo "== summary: checks=${CHECKS} failures=${FAILS} =="
  exit 1
fi

# 1. 节点

SERVERS="$(q 'SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS;')"
if [ -z "${SERVERS}" ]; then
  fail "DBA_OB_SERVERS 为空（节点未注册或权限不足）"
else
  N_SERVERS="$(printf '%s\n' "${SERVERS}" | wc -l | tr -d ' ')"
  BAD="$(printf '%s\n' "${SERVERS}" | awk '$2 != "ACTIVE"')"
  if [ -n "${BAD}" ]; then
    fail "存在非 ACTIVE 节点：${BAD}"
  else
    pass "节点全部 ACTIVE（${N_SERVERS} 个）"
  fi
  if [ -n "${EXPECT_SERVERS:-}" ]; then
    if [ "${N_SERVERS}" = "${EXPECT_SERVERS}" ]; then
      pass "节点数 ${N_SERVERS} 符合期望"
    else
      fail "节点数 ${N_SERVERS} != 期望 ${EXPECT_SERVERS}"
    fi
  fi
fi

# 2. Zone

ZONES="$(q 'SELECT ZONE, STATUS FROM oceanbase.DBA_OB_ZONES;')"
if [ -z "${ZONES}" ]; then
  fail "DBA_OB_ZONES 为空"
else
  N_ZONES="$(printf '%s\n' "${ZONES}" | wc -l | tr -d ' ')"
  pass "Zone 数量=${N_ZONES}"
  if [ -n "${EXPECT_ZONES:-}" ] && [ "${N_ZONES}" != "${EXPECT_ZONES}" ]; then
    fail "Zone 数 ${N_ZONES} != 期望 ${EXPECT_ZONES}"
  fi
fi

# 3. 租户

TENANTS="$(q 'SELECT TENANT_NAME, STATUS FROM oceanbase.DBA_OB_TENANTS;')"
if [ -z "${TENANTS}" ]; then
  fail "DBA_OB_TENANTS 为空"
else
  N_TENANTS="$(printf '%s\n' "${TENANTS}" | wc -l | tr -d ' ')"
  BAD_T="$(printf '%s\n' "${TENANTS}" | awk '$2 != "NORMAL"')"
  if [ -n "${BAD_T}" ]; then
    fail "存在非 NORMAL 租户：${BAD_T}"
  else
    pass "租户全部 NORMAL（${N_TENANTS} 个）"
  fi
  if [ -n "${EXPECT_TENANTS:-}" ] && [ "${N_TENANTS}" != "${EXPECT_TENANTS}" ]; then
    fail "租户数 ${N_TENANTS} != 期望 ${EXPECT_TENANTS}"
  fi
fi

# 4. Unit

UNITS="$(q 'SELECT UNIT_ID, TENANT_ID, STATUS FROM oceanbase.DBA_OB_UNITS;')"
if [ -z "${UNITS}" ]; then
  fail "DBA_OB_UNITS 为空"
else
  N_UNITS="$(printf '%s\n' "${UNITS}" | wc -l | tr -d ' ')"
  pass "Unit 数=${N_UNITS}"
fi

# 5. 关键参数

for p in cpu_count memory_limit net_thread_count freeze_trigger_percentage memstore_limit_percentage; do
  # 外层双引号 + 内层 SQL 单引号：name='<参数名>' 必须是带引号的字符串字面量，
  # 否则 OB 报 Unknown column；GV$ 的 $ 需转义，避免被 bash 当成变量展开
  V="$(q "SELECT DISTINCT value FROM oceanbase.GV\$OB_PARAMETERS WHERE name='${p}';" | awk 'NF {print; exit}')"
  if [ -n "${V}" ]; then
    info "${p}=${V}"
    pass "参数可读：${p}"
  else
    fail "参数读取失败：${p}"
  fi
done

# 5b. 写可用性（默认关：只读冒烟到此即可；需写验收时显式 EXPECT_WRITABLE=1——
# 只读验证不证明可写，写压测连错备库/只读租户时在此拦截）。
# 必须查写目标（业务租户）而非 root@sys：系统租户的 read_only 恒 OFF 会让门禁失效。
if [ "${EXPECT_WRITABLE:-0}" = "1" ]; then
  RO="$(qb "SHOW VARIABLES LIKE 'read_only';" | awk -F'\t' '{print $2}')"
  if [ "${RO}" = "OFF" ]; then
    pass "写可用性：read_only(${BIZ_USER})=OFF"
  else
    fail "写可用性：read_only(${BIZ_USER})=${RO}（目标为只读？写验收须指向可写租户）"
  fi
fi

# 6. 业务库/表（可选；业务租户连接，与系统租户分开；标识符已在脚本开头校验）

if [ -n "${SPACE_DB}" ]; then
  # 库名作为 SQL 字符串字面量时转义单引号（已按标识符校验，仍统一处理）
  esc_db="$(printf '%s' "${SPACE_DB}" | sed "s/'/''/g")"
  TABLES="$(qb "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='${esc_db}';")"
  if [ -z "${TABLES}" ]; then
    fail "业务库 ${SPACE_DB} 无表或不存在（业务租户 ${BIZ_USER}）"
  else
    N_TABLES="$(printf '%s\n' "${TABLES}" | wc -l | tr -d ' ')"
    pass "业务库 ${SPACE_DB} 表数=${N_TABLES}"
    if [ -n "${EXPECT_TABLE_COUNT:-}" ] && [ "${N_TABLES}" != "${EXPECT_TABLE_COUNT}" ]; then
      fail "表数 ${N_TABLES} != 期望 ${EXPECT_TABLE_COUNT}"
    fi
  fi

  if [ -n "${SAMPLE_TABLE}" ]; then
    # 单引号拼接 SQL：变量内容不会被再解析为命令替换，也不使用反引号
    SQL_COUNT='SELECT COUNT(*) FROM '"${SPACE_DB}"'.'"${SAMPLE_TABLE}"';'
    ROWS="$(qb "${SQL_COUNT}")"
    if [ -n "${ROWS}" ]; then
      pass "抽样表 ${SPACE_DB}.${SAMPLE_TABLE} 行数=${ROWS}"
      if [ -n "${EXPECT_ROWS:-}" ] && [ "${ROWS}" != "${EXPECT_ROWS}" ]; then
        fail "行数 ${ROWS} != 期望 ${EXPECT_ROWS}"
      fi
      if [ -n "${SAMPLE_COL}" ]; then
        SQL_SAMPLE='SELECT '"${SAMPLE_COL}"' FROM '"${SPACE_DB}"'.'"${SAMPLE_TABLE}"' LIMIT 1;'
      else
        SQL_SAMPLE='SELECT 1 FROM '"${SPACE_DB}"'.'"${SAMPLE_TABLE}"' LIMIT 1;'
      fi
      SAMPLE="$(qb "${SQL_SAMPLE}")"
      if [ -n "${SAMPLE}" ]; then
        # 只记状态，不把原始行写进验收报告；确需样本证据时显式选列并在报告侧脱敏
        pass "抽样行可读（仅状态，不输出原始行）"
      else
        fail "抽样行读取为空"
      fi
    else
      fail "抽样表计数失败：${SPACE_DB}.${SAMPLE_TABLE}"
    fi
  fi
fi

echo "== summary: checks=${CHECKS} failures=${FAILS} =="
if [ "${FAILS}" -gt 0 ]; then
  exit 1
fi
exit 0
