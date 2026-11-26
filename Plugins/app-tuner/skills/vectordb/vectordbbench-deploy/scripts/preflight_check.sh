#!/usr/bin/env bash
#===============================================================
# preflight_check.sh — VectorDBBench 部署前置检查（只读，无副作用）
#
# 用法:
#   bash scripts/preflight_check.sh [options]
#
# 检查项:
#   1. root 权限
#   2. 操作系统与 CPU 架构
#   3. CPU、内存、安装/构建目录磁盘空间
#   4. git/gcc/make/tar/find 与 realpath
#   5. Python 3.11+、解释器属主/权限、venv 模块
#   6. 源码目录或仓库可达性
#   7. wheelhouse / pip 索引配置
#   8. 构建根目录父目录属主与 sticky 位
#   9. install-root / bin-dir 目录安全与 CLI 链接冲突
#  10. systemd/SELinux 提示
#
# 输出逐项 [PASS]/[WARN]/[FAIL] + 汇总；存在 FAIL 时退出码非零。
# 本脚本不创建、不修改任何文件，也不访问数据库。
#===============================================================
set -u

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

VDB_PINNED_VERSION=v2.0.0
VDB_VERSION=$VDB_PINNED_VERSION
ALLOW_MOVING_BRANCH=0
GIT_URL=https://github.com/zilliztech/VectorDBBench.git
SOURCE_DIR=""
PYTHON=""
INSTALL_ROOT=/opt/vectordbbench
BIN_DIR=/usr/local/bin
CLI_LINK=1
BUILD_ROOT=/var/cache/vectordbbench-deploy
EXTRAS=""
WHEELHOUSE=""
PIP_INDEX_URL_OPT=""
PIP_INDEX_URL_FILE=""

usage() {
  cat <<'EOF'
用法: preflight_check.sh [options]

只读检查 VectorDBBench 部署前置条件，不修改系统、不创建文件。

  --version VER          VectorDBBench 版本/ref（默认内置固定版本 v2.0.0）
  --allow-moving-branch  允许 master/main 等移动分支（默认拒绝）
  --git-url URL          源码仓库
  --source-dir DIR       离线源码目录
  --python PATH          指定 Python 3.11+ 解释器
  --extras LIST          数据库客户端 extras
  --install-root DIR     安装根目录（默认 /opt/vectordbbench）
  --bin-dir DIR          CLI 链接目录（默认 /usr/local/bin）
  --no-cli-link          不检查/创建全局 CLI 链接
  --build-root DIR       构建缓存根目录（默认 /var/cache/vectordbbench-deploy）
  --wheelhouse DIR       离线 wheelhouse
  --pip-index-url-file FILE  自定义 pip 索引文件（root 0600；只校验，不读取内容）
  --pip-index-url URL    自定义 pip 索引（禁止含凭据；建议改用文件/环境变量）
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VDB_VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --allow-moving-branch) ALLOW_MOVING_BRANCH=1; shift ;;
    --git-url) GIT_URL=${2:?--git-url 缺少参数}; shift 2 ;;
    --source-dir) SOURCE_DIR=${2:?--source-dir 缺少参数}; shift 2 ;;
    --python) PYTHON=${2:?--python 缺少参数}; shift 2 ;;
    --extras) EXTRAS=${2:?--extras 缺少参数}; shift 2 ;;
    --install-root) INSTALL_ROOT=${2:?--install-root 缺少参数}; shift 2 ;;
    --bin-dir) BIN_DIR=${2:?--bin-dir 缺少参数}; shift 2 ;;
    --no-cli-link) CLI_LINK=0; shift ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --wheelhouse) WHEELHOUSE=${2:?--wheelhouse 缺少参数}; shift 2 ;;
    --pip-index-url) PIP_INDEX_URL_OPT=${2:?--pip-index-url 缺少参数}; shift 2 ;;
    --pip-index-url-file) PIP_INDEX_URL_FILE=${2:?--pip-index-url-file 缺少参数}; shift 2 ;;
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
  fail "未以 root 运行（安装系统包、写 /opt、创建 CLI 链接都需要 root）"
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
      warn "未在已验证发行版列表内（${ID:-unknown}），按 yum/apt/zypper 通用路径尝试" ;;
  esac
else
  warn "/etc/os-release 不可读，无法识别发行版"
fi
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|aarch64) pass "CPU 架构: $ARCH" ;;
  *) warn "未验证的 CPU 架构: $ARCH" ;;
esac
info "内核: $(uname -r)"

# ---------- 3. 资源 ----------
sect "3. 资源"
NPROC=$(nproc 2>/dev/null || echo 1)
pass "可用 CPU 核数: $NPROC"
MEM_MB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "${MEM_MB:-0}" -ge 2048 ]]; then
  pass "内存: ${MEM_MB} MiB"
else
  warn "内存偏小: ${MEM_MB} MiB（pip 安装/编译依赖建议至少 2 GiB）"
fi
check_disk() {
  local target=$1 need_mb=$2 label=$3
  while [[ ! -d "$target" && "$target" != "/" ]]; do
    target=$(dirname "$target")
  done
  local avail
  avail=$(df -Pk "$target" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1024}')
  if [[ "${avail:-0}" -ge "$need_mb" ]]; then
    pass "$label 可用空间: ${avail} MiB（$target）"
  else
    warn "$label 可用空间不足: ${avail:-unknown} MiB（建议 >= ${need_mb} MiB）"
  fi
}
check_disk "$INSTALL_ROOT" 5120 "安装目录"
check_disk "$BUILD_ROOT" 1024 "构建目录"

# ---------- 4. 工具 ----------
sect "4. 工具依赖"
for cmd in git gcc make tar find; do
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

# ---------- 5. Python ----------
sect "5. Python 3.11+ 与 venv"
py_candidates=()
if [[ -n "$PYTHON" ]]; then
  if [[ "$PYTHON" != /* ]]; then
    PYTHON=$(command -v "$PYTHON" 2>/dev/null || true)
  fi
  [[ -n "$PYTHON" && -x "$PYTHON" ]] || fail "指定的 Python 不存在或不可执行: ${PYTHON:-<empty>}"
  [[ -n "$PYTHON" ]] && py_candidates=("$PYTHON")
else
  for c in python3.13 python3.12 python3.11 python3; do
    p=$(command -v "$c" 2>/dev/null || true)
    [[ -n "$p" ]] && py_candidates+=("$p")
  done
fi

py_chosen=""
py_untrusted=""
for p in "${py_candidates[@]}"; do
  [[ -x "$p" ]] || continue
  if ! "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    continue
  fi
  owner=$(stat -L -c '%u' "$p" 2>/dev/null || echo unknown)
  mode=$(stat -L -c '%a' "$p" 2>/dev/null || echo unknown)
  if [[ "$owner" == "0" ]] && [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0022) == 0 )); then
    py_chosen=$p
    break
  fi
  [[ -z "$py_untrusted" ]] && py_untrusted="$p (uid=$owner mode=$mode)"
done

if [[ -n "$py_chosen" ]]; then
  py_full=$("$py_chosen" -c 'import platform; print(platform.python_version())')
  pass "Python 3.11+: $py_full（$py_chosen）"
  if "$py_chosen" -m venv --help >/dev/null 2>&1; then
    pass "venv 模块可用"
  else
    fail "Python 缺少 venv 模块；Debian/Ubuntu 安装 python3.X-venv"
  fi
  if "$py_chosen" -m ensurepip --version >/dev/null 2>&1; then
    pass "ensurepip 可用（venv 内可安装 pip）"
  else
    warn "ensurepip 不可用；创建 venv 后可能需要系统 pip/网络"
  fi
elif [[ -n "$py_untrusted" ]]; then
  fail "找到 Python 3.11+ 但解释器不可信（必须 root 属主且组/其他不可写）: $py_untrusted"
else
  fail "未找到 Python 3.11+；请安装 python3.11/3.12 并用 --python 指定"
fi

# ---------- 6. 源码 / 网络 ----------
sect "6. 源码与下载源"
if [[ -n "$SOURCE_DIR" ]]; then
  if [[ ! -d "$SOURCE_DIR" ]]; then
    fail "源码目录不存在: $SOURCE_DIR"
  else
    owner=$(stat -L -c '%u' "$SOURCE_DIR")
    mode=$(stat -L -c '%a' "$SOURCE_DIR")
    [[ "$owner" == "0" ]] && pass "源码目录属于 root" || fail "源码目录必须属于 root（当前 uid=$owner）"
    (( (8#$mode & 0022) == 0 )) && pass "源码目录组/其他不可写（$mode）" || fail "源码目录组/其他可写（$mode）"
    [[ -f "$SOURCE_DIR/pyproject.toml" ]] && pass "存在 pyproject.toml" || fail "源码目录缺少 pyproject.toml"
    [[ -d "$SOURCE_DIR/vectordb_bench" ]] && pass "存在 vectordb_bench/" || fail "源码目录缺少 vectordb_bench/"
  fi
else
  if command -v git >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    if timeout 20 git ls-remote --exit-code "$GIT_URL" HEAD >/dev/null 2>&1; then
      pass "源码仓库可达: $GIT_URL"
    else
      warn "无法访问 $GIT_URL；离线部署请用 --source-dir + --wheelhouse"
    fi
  else
    warn "git/timeout 不可用，跳过源码仓库可达性检查"
  fi
fi

# ---------- 7. wheelhouse / pip 索引 ----------
sect "7. pip 依赖来源"
if [[ -n "$WHEELHOUSE" && -n "$PIP_INDEX_URL_OPT" ]]; then
  warn "--wheelhouse 与 --pip-index-url 同时指定；部署时 pip 索引将被忽略"
fi
if [[ -n "$PIP_INDEX_URL_OPT" && -n "$PIP_INDEX_URL_FILE" ]]; then
  fail "--pip-index-url 与 --pip-index-url-file 只能二选一"
fi
if [[ -n "$WHEELHOUSE" ]]; then
  if [[ ! -d "$WHEELHOUSE" ]]; then
    fail "wheelhouse 不存在: $WHEELHOUSE"
  else
    owner=$(stat -L -c '%u' "$WHEELHOUSE")
    mode=$(stat -L -c '%a' "$WHEELHOUSE")
    [[ "$owner" == "0" ]] && pass "wheelhouse 属于 root" || fail "wheelhouse 必须属于 root（当前 uid=$owner）"
    (( (8#$mode & 0022) == 0 )) && pass "wheelhouse 组/其他不可写（$mode）" || fail "wheelhouse 组/其他可写（$mode）"
    info "离线安装将使用 --no-index --find-links 与 --no-build-isolation"
  fi
elif [[ -n "$PIP_INDEX_URL_FILE" ]]; then
  if [[ ! -f "$PIP_INDEX_URL_FILE" ]]; then
    fail "pip 索引文件不存在: $PIP_INDEX_URL_FILE"
  else
    owner=$(stat -L -c '%u' "$PIP_INDEX_URL_FILE" 2>/dev/null || echo unknown)
    mode=$(stat -L -c '%a' "$PIP_INDEX_URL_FILE" 2>/dev/null || echo unknown)
    if [[ "$owner" == "0" ]] && [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0077) == 0 )); then
      pass "pip 索引文件 root 属主且 0600: $PIP_INDEX_URL_FILE（不读取内容）"
    else
      fail "pip 索引文件必须 root 属主且 0600（当前 owner=$owner mode=$mode）: $PIP_INDEX_URL_FILE"
    fi
  fi
elif [[ -n "$PIP_INDEX_URL_OPT" ]]; then
  if url_has_userinfo "$PIP_INDEX_URL_OPT"; then
    fail "--pip-index-url 不得包含用户名/凭据（命令行可被 ps/proc 读取）；请改用 --pip-index-url-file 或环境变量 PIP_INDEX_URL"
  else
    pass "使用自定义 pip 索引（命令行 URL，无凭据）"
  fi
elif [[ -n "${PIP_INDEX_URL:-}" ]]; then
  pass "使用自定义 pip 索引（环境变量，不在预检中读取/打印 URL）"
else
  info "使用 PyPI 默认索引"
fi

# ---------- 8. 构建根目录 ----------
sect "8. 构建根目录"
if [[ -e "$BUILD_ROOT" ]]; then
  owner=$(stat -L -c '%u' "$BUILD_ROOT" 2>/dev/null || echo unknown)
  mode=$(stat -L -c '%a' "$BUILD_ROOT" 2>/dev/null || echo unknown)
  if [[ "$owner" == "0" && ( "$mode" == "700" || "$mode" == "0700" ) ]]; then
    pass "构建根目录已是 root:root 0700: $BUILD_ROOT"
  else
    fail "构建根目录必须是 root:root 0700（当前 owner=$owner mode=$mode）: $BUILD_ROOT"
  fi
else
  info "构建根目录不存在，部署时将创建为 root:root 0700: $BUILD_ROOT"
fi
unsafe=$(unsafe_parent_of "$BUILD_ROOT")
[[ -z "$unsafe" ]] || fail "构建根父目录不安全: ${unsafe/|/: }"

# ---------- 9. install-root / bin-dir ----------
sect "9. 安装目录与 CLI 链接"
check_dir_parents() {
  local dir=$1 label=$2 unsafe
  unsafe=$(unsafe_parent_of "$dir")
  [[ -z "$unsafe" ]] || fail "$label 父目录不安全: ${unsafe/|/: }"
}
if is_system_install_tree "$INSTALL_ROOT"; then
  fail "install-root 不得指向系统目录: $INSTALL_ROOT"
else
  if [[ -e "$INSTALL_ROOT" ]]; then
    owner=$(stat -L -c '%u' "$INSTALL_ROOT" 2>/dev/null || echo unknown)
    mode=$(stat -L -c '%a' "$INSTALL_ROOT" 2>/dev/null || echo unknown)
    if [[ "$owner" == "0" ]] && [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0022) == 0 )); then
      pass "install-root 属主/权限安全: $INSTALL_ROOT（$mode）"
    else
      fail "install-root 必须属于 root 且组/其他不可写（当前 owner=$owner mode=$mode）"
    fi
  else
    info "install-root 不存在，部署时将创建为 root:root 0755: $INSTALL_ROOT"
  fi
  check_dir_parents "$INSTALL_ROOT" "install-root"
fi
if ((CLI_LINK == 1)); then
  if is_system_bin_tree "$BIN_DIR"; then
    fail "bin-dir 不得指向系统二进制目录: $BIN_DIR"
  else
    if [[ -d "$BIN_DIR" ]]; then
      owner=$(stat -L -c '%u' "$BIN_DIR" 2>/dev/null || echo unknown)
      mode=$(stat -L -c '%a' "$BIN_DIR" 2>/dev/null || echo unknown)
      if [[ "$owner" == "0" ]] && [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0022) == 0 )); then
        pass "bin-dir 属主/权限安全: $BIN_DIR（$mode）"
      else
        fail "bin-dir 必须属于 root 且组/其他不可写（当前 owner=$owner mode=$mode）"
      fi
    else
      warn "bin-dir 不存在: $BIN_DIR（部署时将创建）"
    fi
    if [[ -e "$BIN_DIR/vectordbbench" || -L "$BIN_DIR/vectordbbench" ]]; then
      if [[ -d "$BIN_DIR/vectordbbench" && ! -L "$BIN_DIR/vectordbbench" ]]; then
        fail "CLI 链接路径是真实目录，部署会拒绝覆盖: $BIN_DIR/vectordbbench"
      else
        warn "已存在 $BIN_DIR/vectordbbench，部署前会备份为 .bak.<时间戳>"
      fi
    fi
    check_dir_parents "$BIN_DIR" "bin-dir"
  fi
fi
[[ -n "$EXTRAS" ]] && info "数据库 extras: $EXTRAS"

# ---------- 10. 环境提示 ----------
sect "10. 环境提示"
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  info "systemd 可用"
else
  info "未检测到 systemd；本次部署不需要 systemd"
fi
if command -v getenforce >/dev/null 2>&1; then
  info "SELinux: $(getenforce 2>/dev/null || echo unknown)"
fi
if [[ "$VDB_VERSION" == "$VDB_PINNED_VERSION" ]]; then
  info "VectorDBBench 版本: $VDB_VERSION（内置固定版本，部署时校验 commit）"
else
  case "$VDB_VERSION" in
    master|main|develop|HEAD)
      if ((ALLOW_MOVING_BRANCH == 1)); then
        warn "使用移动分支 $VDB_VERSION（已显式允许）；部署后记录实际 commit 与包版本"
      else
        fail "版本 $VDB_VERSION 是移动分支；部署脚本默认拒绝，需显式加 --allow-moving-branch"
      fi ;;
    *)
      info "VectorDBBench 版本: $VDB_VERSION（用户指定 ref，记录实际 commit）" ;;
  esac
fi

# ---------- 汇总 ----------
printf '\n===== 汇总 =====\n'
printf 'PASS=%d  WARN=%d  FAIL=%d\n' "$PASS_CNT" "$WARN_CNT" "$FAIL_CNT"
if ((FAIL_CNT > 0)); then
  printf '存在 FAIL 项，修复后重跑本脚本再开始部署。\n'
  exit 1
fi
printf '前置检查通过（WARN 项确认影响后可继续）。\n'
exit 0
