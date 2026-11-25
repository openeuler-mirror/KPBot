#!/usr/bin/env bash
#===============================================================
# preflight_check.sh — pgvector 部署前置检查（只读，无副作用）
#
# 用法:
#   bash scripts/preflight_check.sh [options]
#
# 检查项:
#   1. root 权限
#   2. 操作系统与 CPU 架构
#   3. CPU、内存、构建目录磁盘空间
#   4. 构建工具、git、findutils/coreutils
#   5. pg_config、PostgreSQL 服务端头文件、psql/pg_isready
#   6. PostgreSQL 运行状态、版本一致性、超级用户权限
#   7. 源码目录 / 下载源可达性
#   8. 构建根目录父目录属主与 sticky 位
#   9. 数据库密码文件权限
#  10. systemd / SELinux 提示
#
# 输出逐项 [PASS]/[WARN]/[FAIL] + 汇总；存在 FAIL 时退出码非零。
# 本脚本不创建、不修改任何文件，也不连接数据库做写操作。
#===============================================================
set -u

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

PGVECTOR_VERSION=master
PG_CONFIG=""
GIT_URL=https://github.com/pgvector/pgvector.git
SOURCE_DIR=""
BUILD_ROOT=/var/cache/pgvector-deploy
DB_NAME=pgvector
DB_USER=postgres
DB_OS_USER=postgres
DB_HOST=""
DB_PORT=5432
DB_PASSWORD_FILE=""
SKIP_POSTGRES_SETUP=0

usage() {
  cat <<'EOF'
用法: preflight_check.sh [options]

只读检查 pgvector 部署前置条件，不修改任何配置、不连接数据库做写操作。

  --version VER          pgvector 版本/ref（默认 master）
  --git-url URL          源码仓库
  --source-dir DIR       离线源码目录
  --pg-config PATH       指定 pg_config
  --build-root DIR       构建缓存根目录（默认 /var/cache/pgvector-deploy）
  --db-name NAME         目标数据库（默认 pgvector）
  --db-user USER         数据库角色（默认 postgres）
  --db-os-user USER      本机 peer 认证 OS 用户（默认 postgres）
  --db-host HOST         远程数据库主机
  --db-port PORT         数据库端口（默认 5432）
  --db-password-file FILE 数据库密码文件（检查权限，不读取内容）
  --skip-postgres-setup  要求已有运行中的 PostgreSQL
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) PGVECTOR_VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --git-url) GIT_URL=${2:?--git-url 缺少参数}; shift 2 ;;
    --source-dir) SOURCE_DIR=${2:?--source-dir 缺少参数}; shift 2 ;;
    --pg-config) PG_CONFIG=${2:?--pg-config 缺少参数}; shift 2 ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --db-name) DB_NAME=${2:?--db-name 缺少参数}; shift 2 ;;
    --db-user) DB_USER=${2:?--db-user 缺少参数}; shift 2 ;;
    --db-os-user) DB_OS_USER=${2:?--db-os-user 缺少参数}; shift 2 ;;
    --db-host) DB_HOST=${2:?--db-host 缺少参数}; shift 2 ;;
    --db-port) DB_PORT=${2:?--db-port 缺少参数}; shift 2 ;;
    --db-password-file) DB_PASSWORD_FILE=${2:?--db-password-file 缺少参数}; shift 2 ;;
    --skip-postgres-setup) SKIP_POSTGRES_SETUP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

PASS_CNT=0
WARN_CNT=0
FAIL_CNT=0

pass() { PASS_CNT=$((PASS_CNT + 1)); printf '%s[PASS]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { WARN_CNT=$((WARN_CNT + 1)); printf '%s[WARN]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
fail() { FAIL_CNT=$((FAIL_CNT + 1)); printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RST" "$*"; }
info() { printf '[INFO] %s\n' "$*"; }
sect() { printf '\n===== %s =====\n' "$*"; }

# ---------- 1. root ----------
sect "1. 权限"
if [[ "$(id -u)" -eq 0 ]]; then
  pass "以 root 运行"
else
  fail "未以 root 运行（安装包、make install、CREATE EXTENSION 都需要 root/超级用户）"
fi

# ---------- 2. OS / arch ----------
sect "2. 操作系统与架构"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "发行版: ${PRETTY_NAME:-unknown}"
  case "${ID:-}" in
    openEuler|centos|rhel|fedora|rocky|almalinux|debian|ubuntu|kylin|anolis|sles|opensuse*)
      pass "发行版在支持列表内（${ID}）" ;;
    *)
      warn "未在已验证发行版列表内（${ID:-unknown}），脚本按 yum/apt/zypper 通用路径尝试" ;;
  esac
else
  warn "/etc/os-release 不可读，无法识别发行版"
fi
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|aarch64) pass "CPU 架构: $ARCH" ;;
  *) warn "未验证的 CPU 架构: $ARCH（pgvector 理论上可构建，需自行验证）" ;;
esac
info "内核: $(uname -r)"

# ---------- 3. 资源 ----------
sect "3. 资源"
NPROC=$(nproc 2>/dev/null || echo 1)
pass "可用 CPU 核数: $NPROC"
MEM_MB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "${MEM_MB:-0}" -ge 1024 ]]; then
  pass "内存: ${MEM_MB} MiB"
else
  warn "内存偏小: ${MEM_MB} MiB（建议至少 1 GiB）"
fi
disk_target=$BUILD_ROOT
while [[ ! -d "$disk_target" && "$disk_target" != "/" ]]; do
  disk_target=$(dirname "$disk_target")
done
DISK_AVAIL_MB=$(df -Pk "$disk_target" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1024}')
if [[ "${DISK_AVAIL_MB:-0}" -ge 1024 ]]; then
  pass "构建目录可用空间: ${DISK_AVAIL_MB} MiB（$disk_target）"
else
  warn "构建目录可用空间不足: ${DISK_AVAIL_MB:-unknown} MiB（建议 >= 1 GiB）"
fi

# ---------- 4. 工具 ----------
sect "4. 工具依赖"
for cmd in gcc make git tar find; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "命令存在: $cmd"
  else
    warn "缺少 $cmd（install_deps.sh 会安装，或加 --skip-deps 自行准备）"
  fi
done
if command -v realpath >/dev/null 2>&1; then
  pass "realpath 可用（路径归一化）"
else
  warn "缺少 realpath（install_deps 会安装 coreutils；--skip-deps 时需自备）"
fi
if command -v runuser >/dev/null 2>&1; then
  pass "runuser 可用（本机以 postgres 执行 psql）"
else
  warn "缺少 runuser（util-linux）；本机 peer 认证部署需要"
fi

# ---------- 5. PostgreSQL 工具 ----------
sect "5. PostgreSQL 工具"
if [[ -n "$PG_CONFIG" ]]; then
  if [[ -x "$PG_CONFIG" ]]; then
    pass "pg_config: $PG_CONFIG"
  else
    fail "pg_config 不存在或不可执行: $PG_CONFIG"
  fi
else
  PG_CONFIG=$(command -v pg_config 2>/dev/null || true)
  if [[ -n "$PG_CONFIG" ]]; then
    pass "pg_config: $PG_CONFIG"
  elif [[ "$SKIP_POSTGRES_SETUP" == "1" ]]; then
    fail "未找到 pg_config，且指定了 --skip-postgres-setup（需要已有 PostgreSQL 服务端与 devel 包）"
  else
    warn "未找到 pg_config；install_deps.sh 会安装发行版 PostgreSQL server/devel"
  fi
fi

if [[ -n "$PG_CONFIG" ]]; then
  info "PostgreSQL 版本: $("$PG_CONFIG" --version 2>/dev/null)"
  pg_owner=$(stat -c '%u' "$PG_CONFIG" 2>/dev/null || echo unknown)
  pg_mode=$(stat -c '%a' "$PG_CONFIG" 2>/dev/null || echo unknown)
  if [[ "$pg_owner" == "0" ]] && [[ "$pg_mode" =~ ^[0-9]+$ ]] && (( (8#$pg_mode & 0022) == 0 )); then
    pass "pg_config 属于 root 且组/其他不可写（$pg_mode）"
  else
    fail "pg_config 必须属于 root 且组/其他不可写（当前 owner=$pg_owner mode=$pg_mode）"
  fi
  pg_major=$("$PG_CONFIG" --version 2>/dev/null | awk '{print $2}' | cut -d. -f1)
  if [[ "$pg_major" =~ ^[0-9]+$ ]] && ((pg_major < 13)); then
    warn "pgvector master 通常要求 PostgreSQL 13+（当前 major=$pg_major）；旧 tag 可能支持更低版本"
  fi
  info "sharedir: $("$PG_CONFIG" --sharedir 2>/dev/null)"
  info "pkglibdir: $("$PG_CONFIG" --pkglibdir 2>/dev/null)"
  inc=$("$PG_CONFIG" --includedir-server 2>/dev/null || true)
  if [[ -f "$inc/postgres.h" ]]; then
    pass "服务端头文件存在: $inc/postgres.h"
  elif [[ "$SKIP_POSTGRES_SETUP" == "1" ]]; then
    fail "缺少服务端头文件（$inc/postgres.h），无法编译 pgvector"
  else
    warn "缺少服务端头文件（$inc/postgres.h）；install_deps.sh 会安装 devel 包"
  fi
  PSQL=$("$PG_CONFIG" --bindir 2>/dev/null)/psql
  if [[ -x "$PSQL" ]]; then
    pass "psql: $PSQL"
  else
    warn "pg_config bindir 下未找到 psql: $PSQL"
  fi
fi

# ---------- 6. PostgreSQL 运行状态 ----------
sect "6. PostgreSQL 运行状态"
if [[ -n "$DB_HOST" ]]; then
  info "远程数据库 $DB_USER@$DB_HOST:$DB_PORT；预检不读取密码，连接性由部署阶段验证"
else
  if [[ -n "$PG_CONFIG" ]]; then
    PG_ISREADY=$("$PG_CONFIG" --bindir 2>/dev/null)/pg_isready
    if [[ -x "$PG_ISREADY" ]] && "$PG_ISREADY" -q -p "$DB_PORT" >/dev/null 2>&1; then
      pass "PostgreSQL 正在端口 $DB_PORT 运行"
      PSQL=$("$PG_CONFIG" --bindir 2>/dev/null)/psql
      if [[ -x "$PSQL" ]] && command -v runuser >/dev/null 2>&1; then
        server_ver=$(runuser -u "$DB_OS_USER" -- "$PSQL" -X -w -p "$DB_PORT" -U "$DB_USER" -d postgres -tAc "SHOW server_version" 2>/dev/null | tr -d '[:space:]' || true)
        if [[ -n "$server_ver" ]]; then
          info "运行版本: $server_ver"
          pg_major=$("$PG_CONFIG" --version | awk '{print $2}' | cut -d. -f1)
          server_major=${server_ver%%.*}
          if [[ "$pg_major" == "$server_major" ]]; then
            pass "pg_config 版本与运行实例一致（major $pg_major）"
          else
            fail "pg_config major=$pg_major，运行实例 major=$server_major；需 --pg-config 指定匹配版本"
          fi
          is_super=$(runuser -u "$DB_OS_USER" -- "$PSQL" -X -w -p "$DB_PORT" -U "$DB_USER" -d postgres -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = current_user" 2>/dev/null | tr -d '[:space:]' || true)
          if [[ "$is_super" == "t" ]]; then
            pass "数据库角色 $DB_USER 是超级用户"
          else
            fail "数据库角色 $DB_USER 不是超级用户（CREATE EXTENSION vector 需要）"
          fi
        else
          warn "无法以 $DB_OS_USER 连接 PostgreSQL 查询版本/权限（检查 pg_hba.conf 与端口）"
        fi
      fi
    else
      if [[ "$SKIP_POSTGRES_SETUP" == "1" ]]; then
        fail "PostgreSQL 未在端口 $DB_PORT 运行，且指定了 --skip-postgres-setup"
      else
        warn "PostgreSQL 未在端口 $DB_PORT 运行；部署阶段会尝试初始化/启动"
      fi
    fi
  fi
fi

# ---------- 7. 源码 / 网络 ----------
sect "7. 源码与下载源"
if [[ -n "$SOURCE_DIR" ]]; then
  if [[ ! -d "$SOURCE_DIR" ]]; then
    fail "源码目录不存在: $SOURCE_DIR"
  else
    owner=$(stat -c '%u' "$SOURCE_DIR")
    mode=$(stat -c '%a' "$SOURCE_DIR")
    if [[ "$owner" == "0" ]]; then
      pass "源码目录属于 root"
    else
      fail "源码目录必须属于 root（当前 uid=$owner）: $SOURCE_DIR"
    fi
    if (( (8#$mode & 0022) == 0 )); then
      pass "源码目录组/其他不可写（$mode）"
    else
      fail "源码目录组/其他可写（$mode），root 构建存在风险: $SOURCE_DIR"
    fi
    if [[ -f "$SOURCE_DIR/Makefile" && -f "$SOURCE_DIR/vector.control" ]]; then
      pass "源码目录包含 Makefile 与 vector.control"
    else
      fail "源码目录缺少 Makefile 或 vector.control"
    fi
  fi
else
  if command -v git >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    if timeout 20 git ls-remote --exit-code "$GIT_URL" HEAD >/dev/null 2>&1; then
      pass "源码仓库可达: $GIT_URL"
    else
      warn "无法访问 $GIT_URL；离线部署请用 --source-dir DIR"
    fi
  else
    warn "git/timeout 不可用，跳过源码仓库可达性检查"
  fi
fi

# ---------- 8. 构建根目录 ----------
sect "8. 构建根目录"
if [[ -e "$BUILD_ROOT" ]]; then
  owner=$(stat -c '%u' "$BUILD_ROOT" 2>/dev/null || echo unknown)
  mode=$(stat -c '%a' "$BUILD_ROOT" 2>/dev/null || echo unknown)
  if [[ "$owner" == "0" && ( "$mode" == "700" || "$mode" == "0700" ) ]]; then
    pass "构建根目录已是 root:root 0700: $BUILD_ROOT"
  else
    fail "构建根目录必须是 root:root 0700（当前 owner=$owner mode=$mode）: $BUILD_ROOT"
  fi
else
  info "构建根目录不存在，部署时将创建为 root:root 0700: $BUILD_ROOT"
fi
parent=$(dirname "$BUILD_ROOT")
while [[ "$parent" != "/" && "$parent" != "." ]]; do
  if [[ -d "$parent" ]]; then
    owner=$(stat -c '%u' "$parent" 2>/dev/null || echo unknown)
    mode=$(stat -c '%a' "$parent" 2>/dev/null || echo unknown)
    if [[ "$owner" != "0" ]]; then
      fail "构建根父目录不属于 root: $parent"
    else
      bits=$((8#$mode))
      if (((bits & 0022) != 0 && (bits & 01000) == 0)); then
        fail "构建根父目录可被组/其他写入且无 sticky 位: $parent（$mode）"
      fi
    fi
  fi
  parent=$(dirname "$parent")
done

# ---------- 9. 密码文件 ----------
sect "9. 数据库密码文件"
if [[ -n "$DB_PASSWORD_FILE" ]]; then
  if [[ ! -f "$DB_PASSWORD_FILE" ]]; then
    fail "密码文件不存在: $DB_PASSWORD_FILE"
  else
    mode=$(stat -c '%a' "$DB_PASSWORD_FILE")
    if (( (8#$mode & 0077) == 0 )); then
      pass "密码文件权限符合要求（$mode），内容未读取"
    else
      fail "密码文件权限过宽（$mode），请执行: chmod 600 $DB_PASSWORD_FILE"
    fi
  fi
else
  if [[ -n "$DB_HOST" ]]; then
    warn "远程数据库未提供 --db-password-file，psql 使用 -w（不会交互提示）；请确保 .pgpass/认证已配置"
  else
    info "本机部署使用 peer 认证，无需密码文件"
  fi
fi

# ---------- 10. 环境提示 ----------
sect "10. 环境提示"
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  info "systemd 可用，部署阶段可自动启动 PostgreSQL"
else
  warn "未检测到 systemd；需自行保证 PostgreSQL 已运行，并使用 --skip-postgres-setup"
fi
if command -v getenforce >/dev/null 2>&1; then
  info "SELinux: $(getenforce 2>/dev/null || echo unknown)"
fi
info "默认 pgvector 版本: master（部署后记录实际 commit）"

# ---------- 汇总 ----------
printf '\n===== 汇总 =====\n'
printf 'PASS=%d  WARN=%d  FAIL=%d\n' "$PASS_CNT" "$WARN_CNT" "$FAIL_CNT"
if ((FAIL_CNT > 0)); then
  printf '存在 FAIL 项，修复后重跑本脚本再开始部署。\n'
  exit 1
fi
printf '前置检查通过（WARN 项确认影响后可继续）。\n'
exit 0
