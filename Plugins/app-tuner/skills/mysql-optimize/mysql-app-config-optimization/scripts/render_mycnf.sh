#!/usr/bin/env bash

# 渲染 MySQL my.cnf（默认 dry-run 打印；--write 才写文件，覆盖需 --force）。
#
# 用法：
#   bash scripts/render_mycnf.sh --spec 8u32g --scenario ro
#   bash scripts/render_mycnf.sh --spec 8u32g --scenario rw \
#     --home /opt/mysql-8.0.25 --datadir /data/mysql/data \
#     --socket /data/mysql/mysql.sock --port 3306 --write --out /etc/my.cnf \
#     [--disable-tls]（仅显式选择的隔离压测）
#
# 默认 ssl=1 保留 TLS；--disable-tls 只用于隔离压测。
# 参数值拒绝换行/控制字符；--write 在同目录临时文件校验后原子替换，保留备份与权限。
# 模板是起点，不是承诺；写文件前请 diff 并备份现网配置。
# 退出码：0=成功；2=参数错误；1=拒绝覆盖/校验或写盘失败。

set -u

MAXPREP_LIMIT=4194304  # MySQL 8.0 上限：sql/sys_vars.cc 中 4*1024*1024

SPEC="8u32g"
SCENARIO="ro"
MYSQL_HOME="/opt/mysql-8.0.25"
DATADIR="/data/mysql/data"
SOCKET="/data/mysql/mysql.sock"
PORT="3306"
LOGDIR="/data/mysql/log"
TMPDIR_DB="/data/mysql/tmp"
RUNDIR="/data/mysql/run"
MYSQL_USER="mysql"
WRITE=0
FORCE=0
TLS_ENABLED=1
OUT="/etc/my.cnf"

need_value() {
  # $1=选项名；$2=解析循环剩余的 $#。缺值时必须点名选项并退出，避免 shift 2 失败死循环。
  if [ "$2" -lt 2 ]; then
    echo "[FAIL] 选项 $1 缺少取值" >&2
    exit 2
  fi
}

reject_control() {
  # $1=选项名；$2=取值。换行/控制字符会变成额外的 my.cnf 配置项，渲染前一律拒绝。
  local ctrl=$'\001\002\003\004\005\006\007\010\011\012\013\014\015\016\017\020\021\022\023\024\025\026\027\030\031\032\033\034\035\036\037\177'
  if [ -z "$2" ]; then
    echo "[FAIL] 选项 $1 的取值为空" >&2
    exit 2
  fi
  case "$2" in
    *["$ctrl"]*)
      echo "[FAIL] 选项 $1 的取值含换行或控制字符，拒绝渲染" >&2
      exit 2
      ;;
  esac
}

quote_value() {
  # 按 MySQL 选项文件语法给含空格/#/; 等的取值加引号，并转义反斜杠与双引号。
  # 控制字符已由 reject_control 拒绝。
  local v="$1"
  case "$v" in
    *[!A-Za-z0-9_./:=+@%^-]*)
      v=${v//\\/\\\\}
      v=${v//\"/\\\"}
      printf '"%s"' "$v"
      ;;
    *)
      printf '%s' "$v"
      ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --spec) need_value "$1" "$#"; SPEC="$2"; shift 2 ;;
    --scenario) need_value "$1" "$#"; SCENARIO="$2"; shift 2 ;;
    --home) need_value "$1" "$#"; MYSQL_HOME="$2"; shift 2 ;;
    --datadir) need_value "$1" "$#"; DATADIR="$2"; shift 2 ;;
    --socket) need_value "$1" "$#"; SOCKET="$2"; shift 2 ;;
    --port) need_value "$1" "$#"; PORT="$2"; shift 2 ;;
    --logdir) need_value "$1" "$#"; LOGDIR="$2"; shift 2 ;;
    --tmpdir) need_value "$1" "$#"; TMPDIR_DB="$2"; shift 2 ;;
    --rundir) need_value "$1" "$#"; RUNDIR="$2"; shift 2 ;;
    --user) need_value "$1" "$#"; MYSQL_USER="$2"; shift 2 ;;
    --out) need_value "$1" "$#"; OUT="$2"; shift 2 ;;
    --write) WRITE=1; shift ;;
    --force) FORCE=1; shift ;;
    --disable-tls) TLS_ENABLED=0; shift ;;
    -h|--help)
      cat <<'HELP'
用法：render_mycnf.sh [--spec 2c8g|8c16g|8u32g|32c256g]
                    [--scenario point|ro|rw|wo] [--home PATH]
                    [--datadir PATH] [--socket PATH] [--port 1-65535]
                    [--logdir PATH] [--tmpdir PATH] [--rundir PATH]
                    [--user NAME] [--out PATH] [--write] [--force]
                    [--disable-tls]（仅隔离压测）
默认 dry-run；--write 才落盘，覆盖需 --force（自动备份、原子替换、保留权限）。
默认 ssl=1；max_prepared_stmt_count 上限 4194304（MySQL 8.0）。
HELP
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

reject_control --spec "$SPEC"
reject_control --scenario "$SCENARIO"
reject_control --home "$MYSQL_HOME"
reject_control --datadir "$DATADIR"
reject_control --socket "$SOCKET"
reject_control --port "$PORT"
reject_control --logdir "$LOGDIR"
reject_control --tmpdir "$TMPDIR_DB"
reject_control --rundir "$RUNDIR"
reject_control --user "$MYSQL_USER"
reject_control --out "$OUT"

case "$PORT" in
  ''|*[!0-9]*) echo "[FAIL] --port 必须是 1-65535 的十进制整数" >&2; exit 2 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "[FAIL] --port=${PORT} 超出 1-65535" >&2
  exit 2
fi

case "${SPEC}" in
  2c8g)   BP="4G";  INST=4;  LOGF="1G";   PCL=4;  RIO=1;  WIO=1;  PURGE=1;  MAXCONN=1200; TOC=1024; TOCI=4 ;;
  8c16g)  BP="8G";  INST=8;  LOGF="2G";   PCL=8;  RIO=4;  WIO=4;  PURGE=4;  MAXCONN=4000; TOC=2048; TOCI=16 ;;
  8u32g)  BP="22G"; INST=16; LOGF="2048M";PCL=16; RIO=25; WIO=5;  PURGE=1;  MAXCONN=2000; TOC=30000; TOCI=16 ;;
  32c256g) BP="128G"; INST=8; LOGF="4G";  PCL=8;  RIO=16; WIO=16; PURGE=16; MAXCONN=48000; TOC=2048; TOCI=16 ;;
  *) echo "[FAIL] 未知规格：${SPEC}（2c8g|8c16g|8u32g|32c256g）" >&2; exit 2 ;;
esac

case "${SCENARIO}" in
  point|ro)
    SPIN_D=20; SPIN_L=100
    # 场景线程/表缓存覆盖仅适用于 8U32G/64t 基线；其它规格沿用规格表基线。
    # 因此 2c8g/ro = 读/写 1/1、表缓存 1024，不会被 25/5、500000 覆盖。
    if [ "${SPEC}" = "8u32g" ]; then
      RIO=25; WIO=5; TOC=500000; TOCI=64
    fi
    ;;
  rw|wo)
    SPIN_D=30; SPIN_L=110
    if [ "${SPEC}" = "8u32g" ]; then
      RIO=20; WIO=15; TOC=16000; TOCI=16
    fi
    # RW/WO 的 purge 至少 4，但不降低规格基线（8c16g=4、32c256g=16 保持）。
    if [ "${PURGE}" -lt 4 ]; then
      PURGE=4
    fi
    ;;
  *) echo "[FAIL] 未知场景：${SCENARIO}（point|ro|rw|wo）" >&2; exit 2 ;;
esac

MAXPREP=4194304
# validate：MySQL 8.0 的 max_prepared_stmt_count 最大值是 4194304，越界会让 mysqld 启动失败。
if [ "${MAXPREP}" -gt "${MAXPREP_LIMIT}" ]; then
  echo "[FAIL] max_prepared_stmt_count=${MAXPREP} 超过 MySQL 8.0 上限 ${MAXPREP_LIMIT}" >&2
  exit 2
fi

CONF="$(cat <<EOF
[mysqld]
basedir=$(quote_value "${MYSQL_HOME}")
datadir=$(quote_value "${DATADIR}")
socket=$(quote_value "${SOCKET}")
pid-file=$(quote_value "${RUNDIR}/mysql.pid")
log-error=$(quote_value "${LOGDIR}/mysql.log")
tmpdir=$(quote_value "${TMPDIR_DB}")
port=${PORT}
user=$(quote_value "${MYSQL_USER}")
character_set_server=utf8mb4
default_time_zone=+8:00
skip_name_resolve=ON

max_connections=${MAXCONN}
back_log=4000
thread_cache_size=2000
max_prepared_stmt_count=${MAXPREP}
performance_schema=OFF

innodb_file_per_table=ON
innodb_buffer_pool_size=${BP}
innodb_buffer_pool_instances=${INST}
innodb_log_file_size=${LOGF}
innodb_log_files_in_group=8
innodb_log_buffer_size=2G
innodb_open_files=10000
table_open_cache=${TOC}
table_open_cache_instances=${TOCI}

innodb_flush_method=O_DIRECT
innodb_flush_log_at_trx_commit=1
sync_binlog=1
innodb_use_native_aio=1
innodb_io_capacity=30000
innodb_io_capacity_max=40000
innodb_lru_scan_depth=9000
innodb_page_cleaners=${PCL}
innodb_flush_neighbors=0
innodb_spin_wait_delay=${SPIN_D}
innodb_sync_spin_loops=${SPIN_L}
innodb_spin_wait_pause_multiplier=5
innodb_adaptive_hash_index=OFF

innodb_write_io_threads=${WIO}
innodb_read_io_threads=${RIO}
innodb_purge_threads=${PURGE}

log-bin=mysql-bin
# TLS 默认开启；ssl=0 仅限显式选择的隔离压测（--disable-tls）。
ssl=${TLS_ENABLED}
sql_mode=STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO,STRICT_ALL_TABLES
EOF
)"

echo "== render my.cnf =="
echo "SPEC=${SPEC} SCENARIO=${SCENARIO} BP=${BP} INST=${INST} RIO=${RIO} WIO=${WIO} PURGE=${PURGE} TOC=${TOC} TOCI=${TOCI}"
echo "（模板为起点，必须按内存/核数/数据/负载复测；ssl=${TLS_ENABLED}，0 仅限隔离压测）"
echo
printf '%s\n' "${CONF}"

if [ "${WRITE}" -eq 0 ]; then
  echo
  echo "Dry-run：未写文件。使用 --write --out <path> 落地；覆盖需 --force。"
  exit 0
fi

if [ -e "${OUT}" ] && [ ! -f "${OUT}" ]; then
  echo "[FAIL] ${OUT} 不是普通文件" >&2
  exit 1
fi
if [ -e "${OUT}" ] && [ "${FORCE}" -ne 1 ]; then
  echo "[FAIL] ${OUT} 已存在；先备份并用 --force，或换 --out" >&2
  exit 1
fi

OUT_DIR=$(dirname -- "${OUT}")
OUT_BASE=$(basename -- "${OUT}")
if [ ! -d "${OUT_DIR}" ]; then
  echo "[FAIL] 输出目录不存在：${OUT_DIR}" >&2
  exit 1
fi

validate_conf() {
  # 原子替换前校验临时文件：结构完整 + 关键取值合法，失败绝不碰目标文件。
  local f="$1" mp sslv
  if [ ! -s "${f}" ]; then
    echo "[FAIL] 配置为空：${f}" >&2
    return 1
  fi
  if ! grep -q '^\[mysqld\]$' "${f}"; then
    echo "[FAIL] 配置缺少 [mysqld] 段" >&2
    return 1
  fi
  mp=$(sed -n 's/^max_prepared_stmt_count=//p' "${f}")
  case "${mp}" in
    ''|*[!0-9]*) echo "[FAIL] max_prepared_stmt_count 取值非法" >&2; return 1 ;;
  esac
  if [ "${mp}" -gt "${MAXPREP_LIMIT}" ]; then
    echo "[FAIL] max_prepared_stmt_count=${mp} 超过 MySQL 8.0 上限 ${MAXPREP_LIMIT}" >&2
    return 1
  fi
  sslv=$(sed -n 's/^ssl=//p' "${f}")
  case "${sslv}" in
    0|1) ;;
    *) echo "[FAIL] ssl 取值非法（只允许 0/1）" >&2; return 1 ;;
  esac
  return 0
}

# 同目录临时文件：写完并校验后原子 rename；绝不先截断目标。
TMP_FILE=$(mktemp "${OUT_DIR}/.${OUT_BASE}.tmp.XXXXXX") || {
  echo "[FAIL] 无法在 ${OUT_DIR} 创建临时文件" >&2
  exit 1
}
trap 'rm -f "${TMP_FILE}"' EXIT HUP INT TERM

if [ -e "${OUT}" ]; then
  # 保留目标原有权限与属主（属主保留失败时至少保留权限）。
  OLD_MODE=$(stat -c '%a' "${OUT}" 2>/dev/null) || OLD_MODE=""
  if [ -n "${OLD_MODE}" ]; then
    chmod "${OLD_MODE}" "${TMP_FILE}" || { echo "[FAIL] 权限设置失败" >&2; exit 1; }
  fi
  chown --reference="${OUT}" "${TMP_FILE}" 2>/dev/null || true
else
  chmod 0600 "${TMP_FILE}" || { echo "[FAIL] 权限设置失败" >&2; exit 1; }
fi

printf '%s\n' "${CONF}" > "${TMP_FILE}" || { echo "[FAIL] 临时文件写入失败" >&2; exit 1; }
validate_conf "${TMP_FILE}" || { echo "[FAIL] 配置校验失败，目标未改动" >&2; exit 1; }
if [ -e "${OUT}" ]; then
  cp -p "${OUT}" "${OUT}.bak.$(date +%Y%m%d%H%M%S)" || { echo "[FAIL] 备份失败，目标未改动" >&2; exit 1; }
fi
mv -f "${TMP_FILE}" "${OUT}" || { echo "[FAIL] 原子替换失败" >&2; exit 1; }
trap - EXIT HUP INT TERM

echo "[INFO] 已写入 ${OUT}（ssl=${TLS_ENABLED}，durability=1/1；上线前请 mysqld --validate-config 并回读 SHOW VARIABLES）"
exit 0
