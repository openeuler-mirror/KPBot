#!/usr/bin/env bash
#===============================================================
# install_pgvector.sh — pgvector 环境安装部署
#
# 默认行为:
#   pgvector 版本 master（git 默认分支），安装到本机 PostgreSQL；
#   若 PostgreSQL 未安装，先安装发行版默认的 server + devel 包并启动实例；
#   在目标数据库（默认 pgvector）创建/升级 vector 扩展并做向量检索冒烟验证。
#
# 版本策略:
#   - 未指定 --version 时固定 master，并记录实际 commit 到 deploy.env
#   - 指定 tag（如 v0.8.0）或 commit 时按指定 ref 构建
#
# 安全:
#   - 构建目录 root:root 0700，父目录属主/sticky 校验
#   - 路径拒绝 .、..、//，realpath 归一化后做系统目录黑名单
#   - --git-url 仅允许 http(s)://；本地源码必须走 --source-dir，避免绕过属主/写权限校验
#   - 离线源码目录要求 root 属主且组/其他不可写，复制后再次校验
#   - 数据库密码只从 600 文件读取，经 PGPASSWORD 环境变量传递，不进入 argv/日志
#===============================================================
set -euo pipefail
umask 022

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

PGVECTOR_VERSION=master
PG_CONFIG=""
GIT_URL=https://github.com/pgvector/pgvector.git
SOURCE_DIR=""
BUILD_ROOT=/var/cache/pgvector-deploy
RESULT_DIR=""
DB_NAME=pgvector
DB_USER=postgres
DB_OS_USER=postgres
DB_HOST=""
DB_PORT=5432
DB_PASSWORD_FILE=""
CREATE_DATABASE=1
SKIP_DEPS=0
SKIP_POSTGRES_SETUP=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: install_pgvector.sh [options]

pgvector:
  --version VER          pgvector 版本/ref（默认 master；可用 v0.8.0 等 tag）
  --git-url URL          源码仓库，仅支持 http(s)://（默认 https://github.com/pgvector/pgvector.git）；
                         本地源码请用 --source-dir
  --source-dir DIR       离线源码目录（要求 root 属主、组/其他不可写）
  --pg-config PATH       指定 pg_config（默认自动查找）

PostgreSQL / 数据库:
  --db-name NAME         目标数据库（默认 pgvector）
  --db-user USER         数据库角色（默认 postgres，需超级用户）
  --db-os-user USER      本机 peer 认证的 OS 用户（默认 postgres）
  --db-host HOST         远程数据库主机（默认本机 socket）
  --db-port PORT         数据库端口（默认 5432）
  --db-password-file FILE 远程数据库密码文件（要求 600，经 PGPASSWORD 传入）
  --no-create-database   数据库不存在时失败，不自动创建

构建与执行:
  --build-root DIR       构建缓存根目录（默认 /var/cache/pgvector-deploy，要求 root:root 0700）
  --result-dir DIR       日志与 deploy.env 输出目录（默认 /var/log/pgvector-deploy/<run_id>）
  --skip-deps            跳过依赖安装
  --skip-postgres-setup  不安装/初始化/启动 PostgreSQL，仅使用已有实例
  --dry-run              打印计划，不落盘、不连接数据库
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) PGVECTOR_VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --git-url) GIT_URL=${2:?--git-url 缺少参数}; shift 2 ;;
    --source-dir) SOURCE_DIR=${2:?--source-dir 缺少参数}; shift 2 ;;
    --pg-config) PG_CONFIG=${2:?--pg-config 缺少参数}; shift 2 ;;
    --db-name) DB_NAME=${2:?--db-name 缺少参数}; shift 2 ;;
    --db-user) DB_USER=${2:?--db-user 缺少参数}; shift 2 ;;
    --db-os-user) DB_OS_USER=${2:?--db-os-user 缺少参数}; shift 2 ;;
    --db-host) DB_HOST=${2:?--db-host 缺少参数}; shift 2 ;;
    --db-port) DB_PORT=${2:?--db-port 缺少参数}; shift 2 ;;
    --db-password-file) DB_PASSWORD_FILE=${2:?--db-password-file 缺少参数}; shift 2 ;;
    --no-create-database) CREATE_DATABASE=0; shift ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --result-dir) RESULT_DIR=${2:?--result-dir 缺少参数}; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --skip-postgres-setup) SKIP_POSTGRES_SETUP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

trim_slash() {
  local p=$1
  while [[ "$p" == */ && "$p" != "/" ]]; do
    p=${p%/}
  done
  printf '%s' "$p"
}

valid_abs_path() {
  local p=$1
  [[ "$p" =~ ^/[A-Za-z0-9._/-]+$ ]] || return 1
  [[ "$p" == *".."* ]] && return 1
  [[ "$p" =~ (^|/)\.(/|$) ]] && return 1
  [[ "$p" == *"//"* ]] && return 1
  return 0
}

normalize_path() {
  local p=$1
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$p" || die "无法归一化路径: $p"
  else
    printf '%s' "$p"
  fi
}

check_system_path() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/local|/var|/var/cache|/var/lib|/var/log|/var/opt|/var/spool|/var/tmp)
      die "拒绝把系统级目录直接作为安装/源码/日志/构建/结果路径: $1（请使用其下专用子目录）" ;;
  esac
}

secure_path() {
  local name=$1
  local p=$2
  valid_abs_path "$p" || die "路径非法（必须是绝对路径，且不含 .、..、// 段）: $p"
  p=$(normalize_path "$p")
  valid_abs_path "$p" || die "归一化后的路径非法: $p"
  check_system_path "$p"
  printf -v "$name" '%s' "$p"
}

check_build_parents() {
  local dir=$1
  local parent perm owner bits
  parent=$(dirname "$dir")
  while [[ "$parent" != "/" && "$parent" != "." ]]; do
    if [[ -d "$parent" ]]; then
      owner=$(stat -c '%u' "$parent")
      perm=$(stat -c '%a' "$parent")
      bits=$((8#$perm))
      [[ "$owner" == "0" ]] || die "构建根目录的父目录不属于 root: $parent（请改用 root 私有路径）"
      if (((bits & 0022) != 0 && (bits & 01000) == 0)); then
        die "构建根目录的父目录可被组/其他用户写入且无 sticky 位: $parent（权限 $perm）"
      fi
    fi
    parent=$(dirname "$parent")
  done
}

prepare_private_dir() {
  local dir=$1
  local owner perm
  if [[ ! -e "$dir" ]]; then
    install -d -m 0700 -o root -g root "$dir"
  fi
  [[ -d "$dir" ]] || die "构建根路径不是目录: $dir"
  owner=$(stat -c '%u' "$dir")
  perm=$(stat -c '%a' "$dir")
  [[ "$owner" == "0" ]] || die "构建根目录必须属于 root（当前 uid=$owner）: $dir"
  case "$perm" in
    700|0700) ;;
    *) die "构建根目录权限必须为 0700（当前 $perm）: $dir" ;;
  esac
}

# ---------- 参数校验 ----------
[[ "$PGVECTOR_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
  || die "pgvector 版本/ref 非法: $PGVECTOR_VERSION"
[[ "$PGVECTOR_VERSION" != *".."* && "$PGVECTOR_VERSION" != *"//"* && "$PGVECTOR_VERSION" != */ ]] \
  || die "pgvector 版本/ref 非法: $PGVECTOR_VERSION"
[[ "$DB_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
  || die "数据库名非法: $DB_NAME（只允许字母、数字、下划线，且不以数字开头）"
[[ "$DB_USER" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
  || die "数据库用户非法: $DB_USER"
[[ "$DB_OS_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] \
  || die "OS 用户非法: $DB_OS_USER"
[[ "$DB_PORT" =~ ^[0-9]+$ ]] && ((DB_PORT >= 1 && DB_PORT <= 65535)) \
  || die "数据库端口非法: $DB_PORT"
if [[ -n "$DB_HOST" ]]; then
  [[ "$DB_HOST" =~ ^[A-Za-z0-9.:-]+$ ]] || die "db-host 非法: $DB_HOST"
fi
if [[ "$GIT_URL" != http://* && "$GIT_URL" != https://* ]]; then
  die "git-url 仅支持 http(s):// URL；本地源码请使用 --source-dir（会校验 root 属主与组/其他不可写）: $GIT_URL"
fi
if [[ -n "$SOURCE_DIR" && "$PGVECTOR_VERSION" != "master" ]]; then
  die "--source-dir 使用目录自带版本，不能再指定 --version $PGVECTOR_VERSION（默认 master 即可）"
fi

BUILD_ROOT=$(trim_slash "$BUILD_ROOT")
[[ -n "$RESULT_DIR" ]] && RESULT_DIR=$(trim_slash "$RESULT_DIR")
if [[ -n "$SOURCE_DIR" ]]; then
  SOURCE_DIR=$(trim_slash "$SOURCE_DIR")
  secure_path SOURCE_DIR "$SOURCE_DIR"
  [[ -d "$SOURCE_DIR" ]] || die "源码目录不存在: $SOURCE_DIR"
  owner=$(stat -c '%u' "$SOURCE_DIR")
  mode=$(stat -c '%a' "$SOURCE_DIR")
  [[ "$owner" == "0" ]] || die "源码目录必须属于 root（当前 uid=$owner）: $SOURCE_DIR"
  (( (8#$mode & 0022) == 0 )) || die "源码目录不得组/其他可写（当前 $mode）: $SOURCE_DIR"
fi
secure_path BUILD_ROOT "$BUILD_ROOT"
if [[ -n "$RESULT_DIR" ]]; then
  secure_path RESULT_DIR "$RESULT_DIR"
fi

# ---------- 运行目录与日志 ----------
run_id="$(date +%Y%m%d-%H%M%S)-$$"
[[ -n "$RESULT_DIR" ]] || RESULT_DIR="/var/log/pgvector-deploy/$run_id"
DEPLOY_ENV="$RESULT_DIR/env/deploy.env"
MAIN_LOG="$RESULT_DIR/logs/install.log"
WORK_DIR=""
PGVECTOR_COMMIT=""
PGVECTOR_EXTENSION_VERSION=""
PGVECTOR_CONTROL=""
PGVECTOR_LIB=""
PG_VERSION=""
PG_MAJOR=""
PG_SHAREDIR=""
PG_PKGLIBDIR=""
DB_PASSWORD_VALUE=""

CURRENT_PHASE=validate
on_exit() {
  local rc=$?
  if ((rc != 0)); then
    if [[ -n "${DEPLOY_ENV:-}" && -f "$DEPLOY_ENV" ]]; then
      {
        printf 'DEPLOY_STATUS=failed\n'
        printf 'FAILED_PHASE=%s\n' "$CURRENT_PHASE"
        printf 'FAILED_EXIT_CODE=%s\n' "$rc"
        printf 'FAILED_LOG=%s\n' "$MAIN_LOG"
      } >>"$DEPLOY_ENV" 2>/dev/null || true
    fi
    if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
      log_warn "构建目录保留供排查: $WORK_DIR"
    fi
    log_error "部署失败（阶段: $CURRENT_PHASE；日志: ${MAIN_LOG:-unknown}）"
  fi
}
trap on_exit EXIT

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
[dry-run] pgvector 环境部署计划（不落盘、不连接数据库）
  pgvector 版本 : $PGVECTOR_VERSION
  源码          : ${SOURCE_DIR:-$GIT_URL}
  pg_config     : ${PG_CONFIG:-自动查找}
  构建缓存      : $BUILD_ROOT（要求 root:root 0700）
  目标数据库    : $DB_NAME（$DB_USER@${DB_HOST:-local}:$DB_PORT）
  自动建库      : $CREATE_DATABASE
  数据库密码    : $( [[ -n "$DB_PASSWORD_FILE" ]] && echo "从 $DB_PASSWORD_FILE 读取（不读取内容）" || echo "未设置" )
  依赖安装      : $([[ "$SKIP_DEPS" == "1" ]] && echo skip || echo yes)
  PostgreSQL    : $([[ "$SKIP_POSTGRES_SETUP" == "1" ]] && echo "仅使用已有实例" || echo "按需安装/初始化/启动" )
  结果目录      : $RESULT_DIR
执行顺序:
  1) install_deps.sh（可 --skip-deps；--skip-postgres-setup 时传 --no-postgres）
  2) setup_postgres.sh（可 --skip-postgres-setup 跳过）
  3) 复制/克隆源码到 root 私有构建目录 → make → make install
  4) 校验 vector.control / vector.so 已安装
  5) 检查数据库版本与超级用户 → 建库（可选）→ CREATE/UPDATE EXTENSION vector
  6) 向量检索冒烟验证 → 写 deploy.env
EOF
  exit 0
fi

require_root
mkdir -p "$RESULT_DIR/logs" "$RESULT_DIR/env"
exec > >(tee -a "$MAIN_LOG") 2>&1

if [[ -f "$DEPLOY_ENV" ]]; then
  mv "$DEPLOY_ENV" "$DEPLOY_ENV.previous.$run_id"
  log_info "旧 deploy.env 已备份: $DEPLOY_ENV.previous.$run_id"
fi
printf 'DEPLOY_STATUS=running\nDEPLOY_RUN_ID=%s\n' "$run_id" >"$DEPLOY_ENV"

if [[ -n "$DB_PASSWORD_FILE" ]]; then
  [[ -f "$DB_PASSWORD_FILE" ]] || die "数据库密码文件不存在: $DB_PASSWORD_FILE"
  mode=$(stat -c '%a' "$DB_PASSWORD_FILE")
  (( (8#$mode & 0077) == 0 )) || die "数据库密码文件权限过宽（$mode），请执行: chmod 600 $DB_PASSWORD_FILE"
  IFS= read -r DB_PASSWORD_VALUE <"$DB_PASSWORD_FILE" || true
  [[ -n "$DB_PASSWORD_VALUE" ]] || die "数据库密码文件首行为空: $DB_PASSWORD_FILE"
fi

# ---------- 1. 依赖 ----------
CURRENT_PHASE=deps
if ((SKIP_DEPS == 0)); then
  log_info "阶段 1/5: 安装依赖"
  deps_args=()
  [[ "$SKIP_POSTGRES_SETUP" == "1" ]] && deps_args+=(--no-postgres)
  if ! bash "$SKILL_ROOT/scripts/install_deps.sh" "${deps_args[@]}" >"$RESULT_DIR/logs/deps.log" 2>&1; then
    tail -n 40 "$RESULT_DIR/logs/deps.log" >&2 || true
    die "依赖安装失败，完整日志: $RESULT_DIR/logs/deps.log"
  fi
  log_ok "依赖安装完成"
else
  log_info "阶段 1/5: 跳过依赖安装（--skip-deps）"
  for cmd in gcc make tar find; do require_cmd "$cmd"; done
  [[ -n "$SOURCE_DIR" ]] || require_cmd git
fi

# ---------- 2. PostgreSQL ----------
CURRENT_PHASE=postgres
log_info "阶段 2/5: 检查 PostgreSQL"
if [[ -n "$PG_CONFIG" ]]; then
  setup_pg_args=(--pg-config "$PG_CONFIG")
else
  setup_pg_args=()
fi
setup_pg_args+=(--db-port "$DB_PORT")
if ((SKIP_POSTGRES_SETUP == 0)); then
  if ! bash "$SKILL_ROOT/scripts/setup_postgres.sh" "${setup_pg_args[@]}" >"$RESULT_DIR/logs/postgres.log" 2>&1; then
    tail -n 60 "$RESULT_DIR/logs/postgres.log" >&2 || true
    die "PostgreSQL 安装/初始化/启动失败，完整日志: $RESULT_DIR/logs/postgres.log"
  fi
  log_ok "PostgreSQL 就绪"
else
  log_info "--skip-postgres-setup：仅使用已有 PostgreSQL，不安装/启动"
fi

resolve_pg_config
check_pg_headers
PG_CONFIG_VERSION=$("$PG_CONFIG" --version | awk '{print $2}')
PG_MAJOR=${PG_CONFIG_VERSION%%.*}
if ((PG_MAJOR < 13)); then
  log_warn "pgvector master 通常要求 PostgreSQL 13+（当前 pg_config: $PG_CONFIG_VERSION）；旧 tag 可能支持更低版本"
fi
PG_BINDIR=$("$PG_CONFIG" --bindir)
PG_SHAREDIR=$("$PG_CONFIG" --sharedir)
PG_PKGLIBDIR=$("$PG_CONFIG" --pkglibdir)
PSQL="$PG_BINDIR/psql"
if [[ ! -x "$PSQL" ]]; then
  PSQL=$(command -v psql 2>/dev/null || true)
fi
[[ -n "$PSQL" ]] || die "未找到 psql（应与 $PG_CONFIG 同版本）"

psql_super() {
  if [[ -n "$DB_HOST" ]]; then
    if [[ -n "$DB_PASSWORD_VALUE" ]]; then
      PGCONNECT_TIMEOUT=10 PGPASSWORD="$DB_PASSWORD_VALUE" "$PSQL" -X -w -v ON_ERROR_STOP=1 \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$@"
    else
      PGCONNECT_TIMEOUT=10 "$PSQL" -X -w -v ON_ERROR_STOP=1 \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$@"
    fi
  else
    command -v runuser >/dev/null 2>&1 || die "缺少 runuser（util-linux），无法以 $DB_OS_USER 执行 psql"
    id -u "$DB_OS_USER" >/dev/null 2>&1 || die "OS 用户不存在: $DB_OS_USER"
    runuser -u "$DB_OS_USER" -- "$PSQL" -X -w -v ON_ERROR_STOP=1 \
      -p "$DB_PORT" -U "$DB_USER" "$@"
  fi
}

if [[ -n "$DB_HOST" ]]; then
  case "$DB_HOST" in
    localhost|127.0.0.1|::1) ;;
    *) log_warn "远程数据库：扩展文件必须已安装在该 DB 主机上；本机构建通常只适用于本机 PostgreSQL" ;;
  esac
fi

raw_server_version=$(psql_super -d postgres -tAc "SHOW server_version" | tr -d '[:space:]')
[[ -n "$raw_server_version" ]] || die "无法查询 PostgreSQL 版本（$DB_USER@${DB_HOST:-local}:$DB_PORT）"
PG_VERSION=$(printf '%s' "$raw_server_version" | sed -E 's/^([0-9]+(\.[0-9]+)*).*/\1/')
[[ -n "$PG_VERSION" ]] || die "无法解析 PostgreSQL 版本: $raw_server_version"
server_major=${PG_VERSION%%.*}
if [[ "$server_major" != "$PG_MAJOR" ]]; then
  die "pg_config 版本（$PG_MAJOR）与运行中的 PostgreSQL（$PG_VERSION）不一致；请用 --pg-config 指定匹配的版本"
fi
is_super=$(psql_super -d postgres -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = current_user" | tr -d '[:space:]')
[[ "$is_super" == "t" ]] || die "数据库角色 $DB_USER 不是超级用户，无法 CREATE EXTENSION vector"

# ---------- 3. 源码与构建 ----------
CURRENT_PHASE=source
log_info "阶段 3/5: 获取 pgvector 源码"
check_build_parents "$BUILD_ROOT"
prepare_private_dir "$BUILD_ROOT"
check_build_parents "$BUILD_ROOT"

safe_version=${PGVECTOR_VERSION//\//_}
WORK_DIR=$(mktemp -d "$BUILD_ROOT/pgvector-$safe_version.XXXXXX") \
  || die "无法在 $BUILD_ROOT 下创建构建目录"

if [[ -n "$SOURCE_DIR" ]]; then
  mkdir -p "$WORK_DIR/src"
  cp -a "$SOURCE_DIR/." "$WORK_DIR/src/"
  log_info "已复制离线源码: $SOURCE_DIR -> $WORK_DIR/src"
else
  require_cmd git
  log_info "克隆 $GIT_URL（ref: $PGVECTOR_VERSION）"
  if ! git clone --quiet "$GIT_URL" "$WORK_DIR/src"; then
    die "git clone 失败: $GIT_URL"
  fi
  if ! git -C "$WORK_DIR/src" checkout --quiet "$PGVECTOR_VERSION"; then
    die "pgvector ref 不存在或无法 checkout: $PGVECTOR_VERSION"
  fi
fi

[[ -f "$WORK_DIR/src/Makefile" ]] || die "源码目录缺少 Makefile: $WORK_DIR/src"
[[ -f "$WORK_DIR/src/vector.control" ]] || die "源码目录缺少 vector.control: $WORK_DIR/src"
bad_entry=$(find "$WORK_DIR/src" \( ! -user 0 -o -type f -perm /022 -o -type d -perm /022 \) -print -quit)
[[ -z "$bad_entry" ]] || die "源码目录存在非 root 属主或组/其他可写的文件/目录: $bad_entry"

if command -v git >/dev/null 2>&1 && [[ -d "$WORK_DIR/src/.git" ]]; then
  PGVECTOR_COMMIT=$(git -C "$WORK_DIR/src" rev-parse HEAD)
else
  PGVECTOR_COMMIT=source-dir
fi

# ---------- 4. 编译安装 ----------
CURRENT_PHASE=build
log_info "阶段 4/5: 编译安装 pgvector（$PGVECTOR_COMMIT）"
jobs=$(nproc 2>/dev/null || echo 1)
build_log="$RESULT_DIR/logs/build.log"
if ! make -C "$WORK_DIR/src" -j"$jobs" PG_CONFIG="$PG_CONFIG" >"$build_log" 2>&1; then
  tail -n 60 "$build_log" >&2 || true
  die "pgvector 编译失败，完整日志: $build_log"
fi
if ! make -C "$WORK_DIR/src" install PG_CONFIG="$PG_CONFIG" >>"$build_log" 2>&1; then
  tail -n 60 "$build_log" >&2 || true
  die "pgvector 安装失败，完整日志: $build_log"
fi

PGVECTOR_CONTROL="$PG_SHAREDIR/extension/vector.control"
PGVECTOR_LIB="$PG_PKGLIBDIR/vector.so"
[[ -f "$PGVECTOR_CONTROL" ]] || die "未找到扩展控制文件: $PGVECTOR_CONTROL"
[[ -f "$PGVECTOR_LIB" ]] || die "未找到扩展库: $PGVECTOR_LIB"
if ! compgen -G "$PG_SHAREDIR/extension/vector--*.sql" >/dev/null; then
  die "未找到 vector 扩展 SQL 文件: $PG_SHAREDIR/extension/vector--*.sql"
fi

# ---------- 5. 数据库与扩展 ----------
CURRENT_PHASE=database
log_info "阶段 5/5: 创建/升级 vector 扩展并验证"

db_exists=$(psql_super -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'") \
  || die "查询数据库列表失败"
if [[ "$CREATE_DATABASE" == "1" ]]; then
  if [[ "$db_exists" != "1" ]]; then
    psql_super -d postgres -c "CREATE DATABASE \"$DB_NAME\""
    log_ok "已创建数据库: $DB_NAME"
  fi
else
  [[ "$db_exists" == "1" ]] || die "数据库不存在（--no-create-database）: $DB_NAME"
fi

available_version=$(psql_super -d "$DB_NAME" -tAc "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
[[ -n "$available_version" ]] || die "运行实例未识别到 vector 扩展；请确认 pg_config=$PG_CONFIG 与实例 sharedir 匹配"
psql_super -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector"
current_version=$(psql_super -d "$DB_NAME" -tAc "SELECT extversion FROM pg_extension WHERE extname = 'vector'" | tr -d '[:space:]')
default_version=$(psql_super -d "$DB_NAME" -tAc "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'" | tr -d '[:space:]')
[[ -n "$default_version" ]] || die "数据库未识别到 vector 扩展文件；请确认扩展安装到了运行实例的 sharedir"
if [[ -n "$current_version" && "$current_version" != "$default_version" ]]; then
  log_info "升级 vector 扩展: $current_version -> $default_version"
  psql_super -d "$DB_NAME" -c "ALTER EXTENSION vector UPDATE"
  current_version=$(psql_super -d "$DB_NAME" -tAc "SELECT extversion FROM pg_extension WHERE extname = 'vector'" | tr -d '[:space:]')
fi
[[ -n "$current_version" ]] || die "vector 扩展创建失败"
PGVECTOR_EXTENSION_VERSION=$current_version

smoke=$(psql_super -d "$DB_NAME" -tAc "
CREATE TEMP TABLE pgvector_smoke (id int, embedding vector(3));
INSERT INTO pgvector_smoke VALUES (1, '[1,2,3]'), (2, '[4,5,6]');
SELECT id FROM pgvector_smoke ORDER BY embedding <-> '[1,2,3]' LIMIT 1;
" | tail -n 1 | tr -d '[:space:]')
[[ "$smoke" == "1" ]] || die "向量检索冒烟验证失败（期望最近邻 id=1，实际: ${smoke:-empty}）"

CURRENT_PHASE=finalize
version_label=$PGVECTOR_VERSION
[[ -n "$SOURCE_DIR" ]] && version_label=source-dir
{
  printf 'DEPLOY_STATUS=ok\n'
  printf 'DEPLOY_RUN_ID=%s\n' "$run_id"
  printf 'PGVECTOR_VERSION=%s\n' "$version_label"
  printf 'PGVECTOR_COMMIT=%s\n' "$PGVECTOR_COMMIT"
  printf 'PGVECTOR_EXTENSION_VERSION=%s\n' "$PGVECTOR_EXTENSION_VERSION"
  printf 'PGVECTOR_SOURCE=%s\n' "${SOURCE_DIR:-$GIT_URL}"
  printf 'PGVECTOR_WORK_DIR=%s\n' "$WORK_DIR"
  printf 'PGVECTOR_CONTROL=%s\n' "$PGVECTOR_CONTROL"
  printf 'PGVECTOR_LIB=%s\n' "$PGVECTOR_LIB"
  printf 'PG_CONFIG=%s\n' "$PG_CONFIG"
  printf 'PG_VERSION=%s\n' "$PG_VERSION"
  printf 'PG_MAJOR=%s\n' "$PG_MAJOR"
  printf 'PG_SHAREDIR=%s\n' "$PG_SHAREDIR"
  printf 'PG_PKGLIBDIR=%s\n' "$PG_PKGLIBDIR"
  printf 'PG_DATABASE=%s\n' "$DB_NAME"
  printf 'PG_DB_USER=%s\n' "$DB_USER"
  printf 'PG_HOST=%s\n' "${DB_HOST:-local}"
  printf 'PG_PORT=%s\n' "$DB_PORT"
  printf 'PGVECTOR_VERIFY=ok\n'
  printf 'DEPLOY_TIME=%s\n' "$(date -Is)"
} >"$DEPLOY_ENV"

log_ok "pgvector 环境部署完成"
log_info "扩展版本: $PGVECTOR_EXTENSION_VERSION（源码: $version_label, commit: $PGVECTOR_COMMIT）"
log_info "PostgreSQL: $PG_VERSION（$PG_CONFIG）"
log_info "数据库: $DB_NAME（$DB_USER@${DB_HOST:-local}:$DB_PORT）"
log_info "产物: $DEPLOY_ENV"

unset DB_PASSWORD_VALUE
exit 0
