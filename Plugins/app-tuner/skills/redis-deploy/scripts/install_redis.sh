#!/usr/bin/env bash
#===============================================================
# install_redis.sh — Redis 环境部署（源码构建 + systemd 托管）
#
# 默认行为:
#   Redis 7.0.15，安装到 /opt/redis-7.0.15，实例名 redis，端口 6379，
#   bind 127.0.0.1，systemd 单元 redis.service，数据目录 /var/lib/redis/redis。
#
# 版本策略:
#   - 未指定 --version 时固定 7.0.15（不追随发行版仓库版本）
#   - assets/checksums.txt 未收录的版本必须提供 --sha256
#   - 下载包必须通过 sha256 校验；确需跳过需显式 --allow-unverified
#
# 安全:
#   密码只从 --password-file 读取，不进入 argv、日志或 deploy.env；
#   非回环 bind 必须提供密码，或显式 --allow-insecure-remote 降级。
#
# 输出:
#   $RESULT_DIR/env/deploy.env  部署契约（不含凭据）
#   $RESULT_DIR/logs/*.log      阶段日志
#===============================================================
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

VERSION=7.0.15
INSTANCE=redis
PREFIX=""
CONF_DIR=/etc/redis
DATA_ROOT=/var/lib/redis
LOG_DIR=/var/log/redis
RUN_DIR=/run/redis
PORT=6379
BIND=127.0.0.1
RUN_AS=redis
PASSWORD_FILE=""
ALLOW_INSECURE_REMOTE=0
MIRROR=https://download.redis.io/releases
TARBALL=""
SHA256=""
ALLOW_UNVERIFIED=0
BUILD_ROOT=/var/cache/redis-deploy
RESULT_DIR=""
BUILD_TLS=0
SKIP_DEPS=0
NO_SYSTEMD=0
KEEP_CONF=0
FORCE=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: install_redis.sh [options]

版本:
  --version VER           Redis 版本（默认 7.0.15）
  --sha256 HEX            源码包期望 sha256（版本未收录时必须提供）
  --tarball FILE          使用本地源码包（离线部署）
  --mirror URL            下载源（默认 https://download.redis.io/releases）
  --allow-unverified      跳过 sha256 校验（不推荐，会记录告警）

实例与路径:
  --instance-name NAME    实例名（默认 redis；决定配置/服务/数据目录名）
  --port PORT             监听端口（默认 6379）
  --bind ADDR             bind 地址（默认 127.0.0.1；支持空格分隔多地址）
  --prefix DIR            安装前缀（默认 /opt/redis-<version>）
  --conf-dir DIR          配置目录（默认 /etc/redis）
  --data-root DIR         数据根目录（默认 /var/lib/redis）
  --log-dir DIR           日志目录（默认 /var/log/redis）
  --run-dir DIR           pid 目录（默认 /run/redis）
  --run-as USER           运行用户（默认 redis，不存在则创建系统用户）

安全:
  --password-file FILE    从文件读取 requirepass（要求权限 600，内容不打印）
  --allow-insecure-remote 允许非回环 bind 且无密码（不推荐）

构建与执行:
  --build-root DIR        构建缓存根目录（默认 /var/cache/redis-deploy，要求 root:root 0700）
  --result-dir DIR        日志与 deploy.env 输出目录（默认 /var/log/redis-deploy/<run_id>）
  --build-tls             构建 TLS 支持（需要 OpenSSL 开发包）
  --skip-deps             跳过依赖安装
  --no-systemd            只写文件和配置，不启用/启动服务
  --keep-conf             保留已有配置文件（不重新生成，密码不会写入）
  --force                 已存在同版本时仍重新编译
  --dry-run               打印计划，不落盘、不启动
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --instance-name) INSTANCE=${2:?--instance-name 缺少参数}; shift 2 ;;
    --prefix) PREFIX=${2:?--prefix 缺少参数}; shift 2 ;;
    --conf-dir) CONF_DIR=${2:?--conf-dir 缺少参数}; shift 2 ;;
    --data-root) DATA_ROOT=${2:?--data-root 缺少参数}; shift 2 ;;
    --log-dir) LOG_DIR=${2:?--log-dir 缺少参数}; shift 2 ;;
    --run-dir) RUN_DIR=${2:?--run-dir 缺少参数}; shift 2 ;;
    --port) PORT=${2:?--port 缺少参数}; shift 2 ;;
    --bind) BIND=${2:?--bind 缺少参数}; shift 2 ;;
    --run-as) RUN_AS=${2:?--run-as 缺少参数}; shift 2 ;;
    --password-file) PASSWORD_FILE=${2:?--password-file 缺少参数}; shift 2 ;;
    --allow-insecure-remote) ALLOW_INSECURE_REMOTE=1; shift ;;
    --mirror) MIRROR=${2:?--mirror 缺少参数}; shift 2 ;;
    --tarball) TARBALL=${2:?--tarball 缺少参数}; shift 2 ;;
    --sha256) SHA256=${2:?--sha256 缺少参数}; shift 2 ;;
    --allow-unverified) ALLOW_UNVERIFIED=1; shift ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --result-dir) RESULT_DIR=${2:?--result-dir 缺少参数}; shift 2 ;;
    --build-tls) BUILD_TLS=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --keep-conf) KEEP_CONF=1; shift ;;
    --force) FORCE=1; shift ;;
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
      die "拒绝把系统级目录直接作为安装/配置/数据/日志/构建/结果路径: $1（请使用其下专用子目录）" ;;
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

check_existing_dir() {
  local dir=$1
  local owner
  if [[ ! -e "$dir" ]]; then
    return 1
  fi
  [[ -d "$dir" ]] || die "路径已存在但不是目录: $dir"
  owner=$(stat -c '%u' "$dir")
  [[ "$owner" == "0" || "$owner" == "$(id -u "$RUN_AS")" ]] \
    || die "目录已存在且属主既不是 root 也不是 $RUN_AS: $dir（拒绝修改系统目录）"
  return 0
}

[[ -n "$PREFIX" ]] || PREFIX="/opt/redis-$VERSION"
PREFIX=$(trim_slash "$PREFIX")
CONF_DIR=$(trim_slash "$CONF_DIR")
DATA_ROOT=$(trim_slash "$DATA_ROOT")
LOG_DIR=$(trim_slash "$LOG_DIR")
RUN_DIR=$(trim_slash "$RUN_DIR")
BUILD_ROOT=$(trim_slash "$BUILD_ROOT")
[[ -n "$RESULT_DIR" ]] && RESULT_DIR=$(trim_slash "$RESULT_DIR")

secure_path PREFIX "$PREFIX"
secure_path CONF_DIR "$CONF_DIR"
secure_path DATA_ROOT "$DATA_ROOT"
secure_path LOG_DIR "$LOG_DIR"
secure_path RUN_DIR "$RUN_DIR"
secure_path BUILD_ROOT "$BUILD_ROOT"
if [[ -n "$RESULT_DIR" ]]; then
  secure_path RESULT_DIR "$RESULT_DIR"
fi
case "$PREFIX" in
  /bin|/bin/*|/sbin|/sbin/*|/usr/bin|/usr/bin/*|/usr/sbin|/usr/sbin/*|/usr/local/bin|/usr/local/bin/*|/usr/local/sbin|/usr/local/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/boot|/boot/*|/etc|/etc/*)
    die "安装前缀不得指向系统可执行/配置目录: $PREFIX（请使用 /opt 或 /usr/local 下的专用子目录）" ;;
esac

CONF="$CONF_DIR/$INSTANCE.conf"
DATA_DIR="$DATA_ROOT/$INSTANCE"
LOG_FILE="$LOG_DIR/$INSTANCE.log"
PID_FILE="$RUN_DIR/$INSTANCE.pid"
SERVICE_NAME="$INSTANCE"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"

# ---------- 参数校验 ----------
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9]+)?$ ]] \
  || die "版本号格式非法: $VERSION（示例: 7.0.15）"
[[ "$INSTANCE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]] \
  || die "实例名非法: $INSTANCE（只允许字母、数字、下划线、连字符）"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) \
  || die "端口非法: $PORT"
[[ "$RUN_AS" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] \
  || die "运行用户非法: $RUN_AS"

for tok in $BIND; do
  [[ "$tok" =~ ^-?(\*|[0-9A-Fa-f:.]+)$ ]] \
    || die "bind 地址非法: '$tok'（支持 IPv4/IPv6，以空格分隔，可带 - 前缀）"
done

if [[ "$MIRROR" != http://* && "$MIRROR" != https://* ]]; then
  die "mirror 必须是 http(s):// URL: $MIRROR"
fi

# 非回环 bind 安全门禁
is_loopback_bind() {
  local tok
  for tok in $BIND; do
    tok=${tok#-}
    case "$tok" in
      127.*|::1) ;;
      *) return 1 ;;
    esac
  done
  return 0
}
if ! is_loopback_bind && [[ -z "$PASSWORD_FILE" ]] && ((ALLOW_INSECURE_REMOTE == 0)); then
  die "bind '$BIND' 不是回环地址且未提供 --password-file；请提供密码文件，或显式 --allow-insecure-remote（不推荐）"
fi

# ---------- sha256 期望值 ----------
EXPECTED_SHA=""
if [[ -n "$SHA256" ]]; then
  is_sha256_hex "$SHA256" || die "--sha256 格式非法（需要 64 位十六进制）"
  EXPECTED_SHA=${SHA256,,}
else
  checksums="$SKILL_ROOT/assets/checksums.txt"
  if [[ -f "$checksums" ]]; then
    EXPECTED_SHA=$(awk -v v="$VERSION" '$1 == v {print $2; exit}' "$checksums")
  fi
fi
if [[ -z "$EXPECTED_SHA" ]] && ((ALLOW_UNVERIFIED == 0)); then
  die "版本 $VERSION 未收录 sha256；请提供 --sha256 HEX（官方发布页可查），或显式 --allow-unverified"
fi
if [[ -n "$TARBALL" && ! -f "$TARBALL" ]]; then
  die "源码包不存在: $TARBALL"
fi

# ---------- 运行目录与日志 ----------
run_id="$(date +%Y%m%d-%H%M%S)-$$"
[[ -n "$RESULT_DIR" ]] || RESULT_DIR="/var/log/redis-deploy/$run_id"
DEPLOY_ENV="$RESULT_DIR/env/deploy.env"
MAIN_LOG="$RESULT_DIR/logs/install.log"
WORK_DIR=""
CACHE_DIR="$BUILD_ROOT/download"
CACHE_FILE="$CACHE_DIR/redis-$VERSION.tar.gz"
PARTIAL_FILE=""

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
    if [[ -n "${PARTIAL_FILE:-}" && -f "$PARTIAL_FILE" ]]; then
      log_warn "下载未完成，保留临时文件供排查: $PARTIAL_FILE"
    fi
    if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
      log_warn "构建目录保留供排查: $WORK_DIR"
    fi
    log_error "部署失败（阶段: $CURRENT_PHASE；日志: ${MAIN_LOG:-unknown}）"
  fi
}
trap on_exit EXIT

REDIS_PASSWORD=""
if [[ -n "$PASSWORD_FILE" && "$DRY_RUN" != "1" ]]; then
  [[ -f "$PASSWORD_FILE" ]] || die "密码文件不存在: $PASSWORD_FILE"
  mode=$(stat -c '%a' "$PASSWORD_FILE")
  if ((8#$mode & 0077)); then
    die "密码文件权限过宽（$mode），请执行: chmod 600 $PASSWORD_FILE"
  fi
  IFS= read -r REDIS_PASSWORD <"$PASSWORD_FILE" || true
  [[ -n "$REDIS_PASSWORD" ]] || die "密码文件首行为空: $PASSWORD_FILE"
  [[ "$REDIS_PASSWORD" != *[[:space:]]* ]] || die "密码首行不能包含空白字符"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
[dry-run] Redis 部署计划（不落盘、不启动）
  版本            : $VERSION
  源码包          : ${TARBALL:-$MIRROR/redis-$VERSION.tar.gz}
  sha256          : ${EXPECTED_SHA:-<未提供，需 --sha256 或 --allow-unverified>}
  实例 / 端口     : $INSTANCE / $PORT
  bind            : $BIND
  安装前缀        : $PREFIX
  配置 / 数据     : $CONF / $DATA_DIR
  日志            : $LOG_FILE
  构建缓存        : $BUILD_ROOT（要求 root:root 0700）
  systemd 单元    : $UNIT_FILE
  运行用户        : $RUN_AS
  密码            : $( [[ -n "$PASSWORD_FILE" ]] && echo "从 $PASSWORD_FILE 读取（不读取内容）" || echo "未设置" )
  TLS 构建        : $([[ "$BUILD_TLS" == "1" ]] && echo yes || echo no)
  依赖安装        : $([[ "$SKIP_DEPS" == "1" ]] && echo skip || echo yes)
  结果目录        : $RESULT_DIR
执行顺序:
  1) install_deps.sh（可 --skip-deps）
  2) 下载/复用源码包 → sha256 校验 → 解压
  3) make -j\$(nproc) $( [[ "$BUILD_TLS" == "1" ]] && echo "BUILD_TLS=yes " )&& make PREFIX=$PREFIX install
  4) 创建运行用户/目录，渲染 redis.conf 与 systemd 单元
  5) 启动服务（可 --no-systemd）→ redis-cli PING / INFO 验收 → 写 deploy.env
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

# ---------- 1. 依赖 ----------
CURRENT_PHASE=deps
if ((SKIP_DEPS == 0)); then
  log_info "阶段 1/5: 安装依赖"
  if ! BUILD_TLS="$BUILD_TLS" bash "$SKILL_ROOT/scripts/install_deps.sh" >"$RESULT_DIR/logs/deps.log" 2>&1; then
    tail -n 40 "$RESULT_DIR/logs/deps.log" >&2 || true
    die "依赖安装失败，完整日志: $RESULT_DIR/logs/deps.log"
  fi
  log_ok "依赖安装完成"
else
  log_info "阶段 1/5: 跳过依赖安装（--skip-deps）"
  for cmd in gcc make tar find; do require_cmd "$cmd"; done
fi

# ---------- 2. 下载与校验 ----------
CURRENT_PHASE=download
log_info "阶段 2/5: 获取并校验源码包"
check_build_parents "$BUILD_ROOT"
prepare_private_dir "$BUILD_ROOT"
# 创建后再查一次，覆盖“中间父目录在两次检查之间被抢占”的情况。
check_build_parents "$BUILD_ROOT"
install -d -m 0700 -o root -g root "$CACHE_DIR"
if [[ -n "$TARBALL" ]]; then
  # 先复制到 root 私有缓存，再对副本做 sha256 与解压，避免校验后被替换（TOCTOU）。
  SRC_TARBALL="$CACHE_DIR/redis-$VERSION.offline.$run_id.tar.gz"
  cat -- "$TARBALL" >"$SRC_TARBALL"
  chown root:root "$SRC_TARBALL"
  chmod 0600 "$SRC_TARBALL"
  log_info "本地源码包已复制到私有缓存: $SRC_TARBALL"
else
  if [[ -f "$CACHE_FILE" ]]; then
    SRC_TARBALL=$CACHE_FILE
    log_info "复用已下载源码包: $SRC_TARBALL"
  else
    url="$MIRROR/redis-$VERSION.tar.gz"
    PARTIAL_FILE="$CACHE_FILE.part.$run_id"
    log_info "下载: $url"
    if command -v curl >/dev/null 2>&1; then
      curl -fL --retry 3 --connect-timeout 10 --max-time 600 -o "$PARTIAL_FILE" "$url"
    elif command -v wget >/dev/null 2>&1; then
      wget --timeout=30 --tries=3 -O "$PARTIAL_FILE" "$url"
    else
      die "缺少 curl 或 wget，无法下载源码包"
    fi
    mv "$PARTIAL_FILE" "$CACHE_FILE"
    PARTIAL_FILE=""
    SRC_TARBALL=$CACHE_FILE
  fi
fi

actual_sha=$(sha256_of "$SRC_TARBALL")
if [[ -n "$EXPECTED_SHA" ]]; then
  if [[ "$actual_sha" != "$EXPECTED_SHA" ]]; then
    die "sha256 不匹配（期望 $EXPECTED_SHA，实际 $actual_sha）；删除损坏包或改用正确源码包后重试"
  fi
  log_ok "sha256 校验通过: $actual_sha"
else
  EXPECTED_SHA=$actual_sha
  log_warn "--allow-unverified 已启用：未校验源码包来源，仅记录 sha256=$actual_sha"
fi

# ---------- 3. 构建 ----------
CURRENT_PHASE=build
log_info "阶段 3/5: 构建 Redis $VERSION"
existing_ver=""
if [[ -x "$PREFIX/bin/redis-server" ]]; then
  existing_ver=$("$PREFIX/bin/redis-server" --version 2>/dev/null | awk '{print $3}' | sed 's/^v=//' || true)
fi
BUILD_REUSED=0
if [[ "$existing_ver" == "$VERSION" && "$FORCE" != "1" ]]; then
  BUILD_REUSED=1
  log_ok "已存在 $PREFIX（v$existing_ver），复用安装产物（--force 可强制重编译）"
  if [[ "$BUILD_TLS" == "1" ]]; then
    log_warn "复用已有二进制；若上次构建未启用 TLS，请加 --force 重新编译"
  fi
elif [[ -n "$existing_ver" && "$existing_ver" != "$VERSION" && "$FORCE" != "1" ]]; then
  die "$PREFIX 已存在 v$existing_ver，与目标 v$VERSION 不一致；确认后加 --force 覆盖"
else
  # 在 root 私有构建根目录下用 mktemp 创建本次工作目录，避免被本地用户替换。
  WORK_DIR=$(mktemp -d "$BUILD_ROOT/redis-$VERSION.XXXXXX") \
    || die "无法在 $BUILD_ROOT 下创建构建目录"
  mkdir -p "$WORK_DIR/src"
  tar -xzf "$SRC_TARBALL" -C "$WORK_DIR/src" --strip-components=1 --no-same-owner
  [[ -f "$WORK_DIR/src/src/Makefile" ]] || die "源码包结构不符合预期（缺少 src/Makefile）"
  bad_entry=$(find "$WORK_DIR/src" \( ! -user 0 -o -type f -perm /022 -o -type d -perm /022 \) -print -quit)
  [[ -z "$bad_entry" ]] || die "解压目录存在非 root 属主或组/其他可写的文件/目录: $bad_entry"
  log_info "构建目录: $WORK_DIR"
  jobs=$(nproc 2>/dev/null || echo 1)
  build_log="$RESULT_DIR/logs/build.log"
  log_info "编译中（-j$jobs），日志: $build_log"
  if [[ "$BUILD_TLS" == "1" ]]; then
    make -C "$WORK_DIR/src" -j"$jobs" BUILD_TLS=yes >"$build_log" 2>&1 || {
      tail -n 50 "$build_log" >&2 || true
      die "编译失败，完整日志: $build_log"
    }
  else
    make -C "$WORK_DIR/src" -j"$jobs" >"$build_log" 2>&1 || {
      tail -n 50 "$build_log" >&2 || true
      die "编译失败，完整日志: $build_log"
    }
  fi
  if ! make -C "$WORK_DIR/src" PREFIX="$PREFIX" install >>"$build_log" 2>&1; then
    tail -n 50 "$build_log" >&2 || true
    die "安装到 $PREFIX 失败，完整日志: $build_log"
  fi
  log_ok "构建并安装完成: $PREFIX"
fi

installed_ver=$("$PREFIX/bin/redis-server" --version 2>/dev/null | awk '{print $3}' | sed 's/^v=//' || true)
[[ "$installed_ver" == "$VERSION" ]] \
  || die "安装后版本校验失败（期望 $VERSION，实际 ${installed_ver:-unknown}）"

# ---------- 4. 用户、目录、配置、服务 ----------
CURRENT_PHASE=configure
log_info "阶段 4/5: 创建用户、目录与配置"

if [[ "$RUN_AS" != "root" ]]; then
  if ! id -u "$RUN_AS" >/dev/null 2>&1; then
    command -v useradd >/dev/null 2>&1 || die "缺少 useradd，无法创建运行用户 $RUN_AS"
    nologin=/sbin/nologin
    [[ -x /usr/sbin/nologin ]] && nologin=/usr/sbin/nologin
    useradd -r -M -s "$nologin" -d "$DATA_ROOT" "$RUN_AS"
    log_ok "已创建系统用户: $RUN_AS"
  fi
  RUN_GROUP=$(id -gn "$RUN_AS")
else
  RUN_GROUP=root
fi

if ! check_existing_dir "$CONF_DIR"; then
  install -d -m 0755 -o root -g root "$CONF_DIR"
fi
for dir in "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"; do
  if check_existing_dir "$dir"; then
    if [[ "$(stat -c '%u' "$dir")" == "0" && "$RUN_AS" != "root" ]]; then
      die "目录已存在且属于 root: $dir；请确认是专用目录后 chown $RUN_AS:$RUN_GROUP 或删除后重试"
    fi
  else
    install -d -m 0750 -o "$RUN_AS" -g "$RUN_GROUP" "$dir"
  fi
done

render_template() {
  local tpl=$1 out=$2
  sed \
    -e "s|@NAME@|$INSTANCE|g" \
    -e "s|@VERSION@|$VERSION|g" \
    -e "s|@PORT@|$PORT|g" \
    -e "s|@BIND@|$BIND|g" \
    -e "s|@PREFIX@|$PREFIX|g" \
    -e "s|@CONF@|$CONF|g" \
    -e "s|@USER@|$RUN_AS|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    -e "s|@LOG_FILE@|$LOG_FILE|g" \
    -e "s|@PID_FILE@|$PID_FILE|g" \
    "$tpl" >"$out"
}

BACKUPS=()
backup_file() {
  local f=$1
  if [[ -e "$f" ]]; then
    local b="$f.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$f" "$b"
    BACKUPS+=("$b")
    log_info "已备份: $b"
  fi
}

if ((KEEP_CONF == 1)) && [[ -f "$CONF" ]]; then
  log_warn "--keep-conf：保留现有配置 $CONF（未重新生成）"
  if [[ -n "$PASSWORD_FILE" ]]; then
    log_warn "--keep-conf 下不会写入 requirepass，请自行确认现有配置的认证方式"
  fi
else
  backup_file "$CONF"
  tmp_conf=$(mktemp "$CONF_DIR/.${INSTANCE}.conf.XXXXXX")
  render_template "$SKILL_ROOT/assets/redis.conf.template" "$tmp_conf"
  if [[ -n "$REDIS_PASSWORD" ]]; then
    {
      printf '\n# requirepass injected by redis-deploy from %s (file mode 600)\n' "$PASSWORD_FILE"
      printf 'requirepass %s\n' "$REDIS_PASSWORD"
    } >>"$tmp_conf"
    log_info "已写入 requirepass（内容不打印）"
  fi
  chown root:"$RUN_GROUP" "$tmp_conf"
  chmod 0640 "$tmp_conf"
  mv -f "$tmp_conf" "$CONF"
  log_ok "已生成配置: $CONF（0640 root:$RUN_GROUP）"
fi

backup_file "$UNIT_FILE"
tmp_unit=$(mktemp "/etc/systemd/system/.${SERVICE_NAME}.service.XXXXXX")
render_template "$SKILL_ROOT/assets/redis.service.template" "$tmp_unit"
chmod 0644 "$tmp_unit"
mv -f "$tmp_unit" "$UNIT_FILE"
log_ok "已生成 systemd 单元: $UNIT_FILE"

# ---------- 5. 启动与验收 ----------
CURRENT_PHASE=start
VERIFY_STATUS=skipped
SERVICE_MANAGED=0
if ((NO_SYSTEMD == 0)); then
  if [[ -d /run/systemd/system ]]; then
    SERVICE_MANAGED=1
    log_info "阶段 5/5: 启动服务 $SERVICE_NAME"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || log_warn "systemctl enable 失败，请检查单元文件"
    if ! systemctl restart "$SERVICE_NAME"; then
      journalctl -u "$SERVICE_NAME" -n 50 --no-pager 2>/dev/null || true
      die "服务启动失败，请查看: journalctl -u $SERVICE_NAME -n 100"
    fi
  else
    log_warn "未检测到 systemd（容器环境？），跳过服务启动与在线验收"
  fi
else
  log_info "阶段 5/5: --no-systemd，跳过服务启动"
fi

PROBE_HOST=127.0.0.1
if [[ "$BIND" != *"127.0.0.1"* && "$BIND" != *"0.0.0.0"* && "$BIND" != *"*"* ]]; then
  first_bind=${BIND%% *}
  PROBE_HOST=${first_bind#-}
fi

redis_cli() {
  if [[ -n "$REDIS_PASSWORD" ]]; then
    REDISCLI_AUTH="$REDIS_PASSWORD" "$PREFIX/bin/redis-cli" -h "$PROBE_HOST" -p "$PORT" "$@" 2>/dev/null
  else
    "$PREFIX/bin/redis-cli" -h "$PROBE_HOST" -p "$PORT" "$@" 2>/dev/null
  fi
}

if ((SERVICE_MANAGED == 1)); then
  CURRENT_PHASE=verify
  ready=0
  for _ in $(seq 1 60); do
    if [[ "$(redis_cli ping || true)" == "PONG" ]]; then
      ready=1
      break
    fi
    sleep 0.5
  done
  if ((ready == 0)); then
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager 2>/dev/null || true
    die "服务已启动但 PING 未返回 PONG；检查 $LOG_FILE 与 journalctl -u $SERVICE_NAME"
  fi
  info_server=$(redis_cli info server || true)
  running_ver=$(printf '%s\n' "$info_server" | awk -F: '/^redis_version:/ {gsub("\r", "", $2); print $2}')
  [[ "$running_ver" == "$VERSION" ]] \
    || die "运行版本与目标不一致（期望 $VERSION，实际 ${running_ver:-unknown}）"
  if command -v ss >/dev/null 2>&1; then
    ss -lntH "sport = :$PORT" | grep -q . || die "端口 $PORT 未处于监听状态"
  fi
  systemctl is-active --quiet "$SERVICE_NAME" || die "systemd 服务状态不是 active"
  VERIFY_STATUS=ok
  log_ok "验收通过: PING=PONG, redis_version=$running_ver, port=$PORT"
fi

CURRENT_PHASE=finalize
{
  printf 'DEPLOY_STATUS=ok\n'
  printf 'DEPLOY_RUN_ID=%s\n' "$run_id"
  printf 'REDIS_VERSION=%s\n' "$VERSION"
  printf 'REDIS_INSTANCE=%s\n' "$INSTANCE"
  printf 'REDIS_PORT=%s\n' "$PORT"
  printf 'REDIS_BIND=%s\n' "$BIND"
  printf 'REDIS_PREFIX=%s\n' "$PREFIX"
  printf 'REDIS_SERVER_BIN=%s\n' "$PREFIX/bin/redis-server"
  printf 'REDIS_CLI_BIN=%s\n' "$PREFIX/bin/redis-cli"
  printf 'REDIS_BENCHMARK_BIN=%s\n' "$PREFIX/bin/redis-benchmark"
  printf 'REDIS_CONF=%s\n' "$CONF"
  printf 'REDIS_DATA_DIR=%s\n' "$DATA_DIR"
  printf 'REDIS_LOG_FILE=%s\n' "$LOG_FILE"
  printf 'REDIS_RUN_USER=%s\n' "$RUN_AS"
  printf 'REDIS_SERVICE=%s.service\n' "$SERVICE_NAME"
  printf 'REDIS_SERVICE_MANAGED=%s\n' "$SERVICE_MANAGED"
  printf 'REDIS_VERIFY=%s\n' "$VERIFY_STATUS"
  printf 'REDIS_TLS=%s\n' "$BUILD_TLS"
  printf 'REDIS_BUILD_REUSED=%s\n' "$BUILD_REUSED"
  printf 'REDIS_SHA256=%s\n' "$EXPECTED_SHA"
  printf 'REDIS_PASSWORD_SET=%s\n' "$([[ -n "$REDIS_PASSWORD" ]] && echo 1 || echo 0)"
  printf 'DEPLOY_TIME=%s\n' "$(date -Is)"
} >"$DEPLOY_ENV"

log_ok "Redis $VERSION 部署完成"
log_info "配置: $CONF"
log_info "数据: $DATA_DIR"
log_info "服务: $SERVICE_NAME.service（managed=$SERVICE_MANAGED, verify=$VERIFY_STATUS）"
log_info "验证: $PREFIX/bin/redis-cli -h $PROBE_HOST -p $PORT ping"
log_info "产物: $DEPLOY_ENV"
if ((SERVICE_MANAGED == 0)); then
  log_info "手动启动: sudo -u $RUN_AS $PREFIX/bin/redis-server $CONF"
fi
if ((${#BACKUPS[@]} > 0)); then
  log_info "本次备份文件: ${BACKUPS[*]}"
fi

exit 0
