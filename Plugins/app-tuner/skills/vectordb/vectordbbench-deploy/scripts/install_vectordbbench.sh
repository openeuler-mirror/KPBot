#!/usr/bin/env bash
#===============================================================
# install_vectordbbench.sh — VectorDBBench 环境安装部署
#
# 默认行为:
#   安装内置固定版本 VectorDBBench v2.0.0（https://github.com/zilliztech/VectorDBBench），
#   用 Python 3.11+ 创建独立 venv，pip 从源码安装，安装 vectordbbench CLI。
#
# 版本策略:
#   - 未指定 --version 时安装内置固定版本并校验 commit，包版本以 pip show 为准
#   - 指定 tag/commit 时按 ref checkout，并记录实际 commit / package version
#   - master/main 等移动分支必须显式 --allow-moving-branch，并记录实际 commit
#   - --source-dir 使用本地源码树（目录自带版本）
#
# 安全:
#   - 构建/安装目录 root 私有，路径拒绝 .、..、//
#   - Python 解释器与源码/wheelhouse 要求 root 属主、组/其他不可写（stat -L 解引用符号链接）
#   - 非 PyPI 索引通过 PIP_INDEX_URL 环境变量或 root-only 0600 文件传入；
#     不接受含凭据的命令行 URL，不写入日志/deploy.env
#===============================================================
set -euo pipefail
umask 022

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

VDB_PINNED_VERSION=v2.0.0
VDB_PINNED_COMMIT=f50544230470770e711126f48d9a077b9cb62ff1
VDB_VERSION=$VDB_PINNED_VERSION
GIT_URL=https://github.com/zilliztech/VectorDBBench.git
SOURCE_DIR=""
PYTHON=""
INSTALL_ROOT=/opt/vectordbbench
BIN_DIR=/usr/local/bin
CLI_LINK=1
BUILD_ROOT=/var/cache/vectordbbench-deploy
RESULT_DIR=""
EXTRAS=""
WHEELHOUSE=""
PIP_INDEX_URL_OPT=""
PIP_INDEX_URL_FILE=""
ALLOW_MOVING_BRANCH=0
VERSION_EXPLICIT=0
SKIP_DEPS=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: install_vectordbbench.sh [options]

VectorDBBench:
  --version VER          版本/ref（默认内置固定版本 v2.0.0，并校验 commit）
  --allow-moving-branch  允许 master/main 等移动分支（默认拒绝；会记录实际 commit）
  --git-url URL          源码仓库（默认 https://github.com/zilliztech/VectorDBBench.git）
  --source-dir DIR       离线源码目录（要求 root 属主、组/其他不可写）
  --python PATH          指定 Python 3.11+ 解释器（默认自动查找）
  --extras LIST          安装数据库客户端 extras，逗号分隔（如 pgvector,redis）

安装位置:
  --install-root DIR     安装根目录（默认 /opt/vectordbbench）
  --bin-dir DIR          CLI 链接目录（默认 /usr/local/bin）
  --no-cli-link          不创建全局 CLI 链接
  --build-root DIR       构建缓存根目录（默认 /var/cache/vectordbbench-deploy）
  --result-dir DIR       日志与 deploy.env 输出目录（默认 /var/log/vectordbbench-deploy/<run_id>）

依赖:
  --wheelhouse DIR       离线 wheelhouse（root 属主、组/其他不可写）
  --pip-index-url-file FILE  从 root 属主 0600 文件读取 pip 索引（推荐；首行生效）
  --pip-index-url URL    自定义 pip 索引（禁止含凭据；建议改用文件或环境变量）
  --skip-deps            跳过系统依赖安装
  --dry-run              打印计划，不落盘、不安装
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VDB_VERSION=${2:?--version 缺少参数}; VERSION_EXPLICIT=1; shift 2 ;;
    --allow-moving-branch) ALLOW_MOVING_BRANCH=1; shift ;;
    --git-url) GIT_URL=${2:?--git-url 缺少参数}; shift 2 ;;
    --source-dir) SOURCE_DIR=${2:?--source-dir 缺少参数}; shift 2 ;;
    --python) PYTHON=${2:?--python 缺少参数}; shift 2 ;;
    --extras) EXTRAS=${2:?--extras 缺少参数}; shift 2 ;;
    --install-root) INSTALL_ROOT=${2:?--install-root 缺少参数}; shift 2 ;;
    --bin-dir) BIN_DIR=${2:?--bin-dir 缺少参数}; shift 2 ;;
    --no-cli-link) CLI_LINK=0; shift ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --result-dir) RESULT_DIR=${2:?--result-dir 缺少参数}; shift 2 ;;
    --wheelhouse) WHEELHOUSE=${2:?--wheelhouse 缺少参数}; shift 2 ;;
    --pip-index-url) PIP_INDEX_URL_OPT=${2:?--pip-index-url 缺少参数}; shift 2 ;;
    --pip-index-url-file) PIP_INDEX_URL_FILE=${2:?--pip-index-url-file 缺少参数}; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

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
  local unsafe
  unsafe=$(unsafe_parent_of "$1")
  [[ -z "$unsafe" ]] || die "构建根目录的父目录不安全: ${unsafe/|/: }（请改用 root 私有路径）"
}

prepare_private_dir() {
  local dir=$1
  local owner perm
  if [[ ! -e "$dir" ]]; then
    install -d -m 0700 -o root -g root "$dir"
  fi
  [[ -d "$dir" ]] || die "构建根路径不是目录: $dir"
  owner=$(stat -L -c '%u' "$dir")
  perm=$(stat -L -c '%a' "$dir")
  [[ "$owner" == "0" ]] || die "构建根目录必须属于 root（当前 uid=$owner）: $dir"
  case "$perm" in
    700|0700) ;;
    *) die "构建根目录权限必须为 0700（当前 $perm）: $dir" ;;
  esac
}

check_parent_chain() {
  local unsafe
  unsafe=$(unsafe_parent_of "$1")
  [[ -z "$unsafe" ]] || die "$2 的父目录不安全: ${unsafe/|/: }（请改用 root 私有路径）"
}

prepare_root_dir() {
  local dir=$1
  local mode=${2:-0755}
  local owner perm
  if [[ ! -e "$dir" ]]; then
    install -d -m "$mode" -o root -g root "$dir"
  fi
  [[ -d "$dir" ]] || die "路径不是目录: $dir"
  owner=$(stat -L -c '%u' "$dir")
  perm=$(stat -L -c '%a' "$dir")
  [[ "$owner" == "0" ]] || die "安装目录必须属于 root（当前 uid=$owner）: $dir"
  (( (8#$perm & 0022) == 0 )) || die "安装目录不得组/其他可写（当前 $perm）: $dir"
}

# ---------- 参数校验 ----------
[[ "$VDB_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
  || die "VectorDBBench 版本/ref 非法: $VDB_VERSION"
[[ "$VDB_VERSION" != *".."* && "$VDB_VERSION" != *"//"* && "$VDB_VERSION" != */ ]] \
  || die "VectorDBBench 版本/ref 非法: $VDB_VERSION"
case "$VDB_VERSION" in
  master|main|develop|HEAD)
    ((ALLOW_MOVING_BRANCH == 1)) \
      || die "拒绝默认安装移动分支 $VDB_VERSION；默认使用固定版本 $VDB_PINNED_VERSION。如确需移动分支，请显式加 --allow-moving-branch（并记录实际 commit）" ;;
esac
[[ "$EXTRAS" =~ ^[A-Za-z0-9_,.-]*$ ]] \
  || die "extras 非法: $EXTRAS（只允许字母、数字、下划线、逗号、点、连字符）"
if [[ "$GIT_URL" != http://* && "$GIT_URL" != https://* && "$GIT_URL" != file://* ]]; then
  die "git-url 必须是 http(s):// 或 file:// URL: $GIT_URL"
fi
if [[ -n "$PIP_INDEX_URL_OPT" && -n "$PIP_INDEX_URL_FILE" ]]; then
  die "--pip-index-url 与 --pip-index-url-file 只能二选一"
fi
if [[ -n "$PIP_INDEX_URL_OPT" ]]; then
  case "$PIP_INDEX_URL_OPT" in
    http://*|https://*|file://*) ;;
    *) die "pip-index-url 必须是 http(s):// 或 file:// URL" ;;
  esac
  url_has_userinfo "$PIP_INDEX_URL_OPT" \
    && die "--pip-index-url 不得包含用户名/凭据（命令行可被 ps/proc 读取）；请改用 --pip-index-url-file FILE 或预置环境变量 PIP_INDEX_URL"
fi
if [[ -n "$SOURCE_DIR" && "$VERSION_EXPLICIT" == "1" ]]; then
  die "--source-dir 使用目录自带版本，不能再指定 --version $VDB_VERSION"
fi

BUILD_ROOT=$(trim_slash "$BUILD_ROOT")
[[ -n "$RESULT_DIR" ]] && RESULT_DIR=$(trim_slash "$RESULT_DIR")
INSTALL_ROOT=$(trim_slash "$INSTALL_ROOT")
BIN_DIR=$(trim_slash "$BIN_DIR")
if [[ -n "$SOURCE_DIR" ]]; then
  SOURCE_DIR=$(trim_slash "$SOURCE_DIR")
  secure_path SOURCE_DIR "$SOURCE_DIR"
  [[ -d "$SOURCE_DIR" ]] || die "源码目录不存在: $SOURCE_DIR"
  owner=$(stat -L -c '%u' "$SOURCE_DIR")
  mode=$(stat -L -c '%a' "$SOURCE_DIR")
  [[ "$owner" == "0" ]] || die "源码目录必须属于 root（当前 uid=$owner）: $SOURCE_DIR"
  (( (8#$mode & 0022) == 0 )) || die "源码目录不得组/其他可写（当前 $mode）: $SOURCE_DIR"
fi
if [[ -n "$WHEELHOUSE" ]]; then
  WHEELHOUSE=$(trim_slash "$WHEELHOUSE")
  secure_path WHEELHOUSE "$WHEELHOUSE"
  [[ -d "$WHEELHOUSE" ]] || die "wheelhouse 不存在: $WHEELHOUSE"
  owner=$(stat -L -c '%u' "$WHEELHOUSE")
  mode=$(stat -L -c '%a' "$WHEELHOUSE")
  [[ "$owner" == "0" ]] || die "wheelhouse 必须属于 root（当前 uid=$owner）: $WHEELHOUSE"
  (( (8#$mode & 0022) == 0 )) || die "wheelhouse 不得组/其他可写（当前 $mode）: $WHEELHOUSE"
fi
if [[ -n "$PIP_INDEX_URL_FILE" ]]; then
  PIP_INDEX_URL_FILE=$(trim_slash "$PIP_INDEX_URL_FILE")
  secure_path PIP_INDEX_URL_FILE "$PIP_INDEX_URL_FILE"
  [[ -f "$PIP_INDEX_URL_FILE" ]] || die "pip 索引文件不存在: $PIP_INDEX_URL_FILE"
  owner=$(stat -L -c '%u' "$PIP_INDEX_URL_FILE" 2>/dev/null || echo '')
  mode=$(stat -L -c '%a' "$PIP_INDEX_URL_FILE" 2>/dev/null || echo '')
  [[ "$owner" == "0" ]] || die "pip 索引文件必须属于 root（当前 uid=${owner:-unknown}）: $PIP_INDEX_URL_FILE"
  [[ "$mode" =~ ^[0-9]+$ ]] || die "无法读取 pip 索引文件权限: $PIP_INDEX_URL_FILE"
  (( (8#$mode & 0077) == 0 )) || die "pip 索引文件不得被组/其他用户访问（要求 0600，当前 $mode）: $PIP_INDEX_URL_FILE"
  IFS= read -r PIP_INDEX_URL_OPT <"$PIP_INDEX_URL_FILE" || true
  PIP_INDEX_URL_OPT=${PIP_INDEX_URL_OPT%$'\r'}
  [[ -n "$PIP_INDEX_URL_OPT" ]] || die "pip 索引文件首行为空: $PIP_INDEX_URL_FILE"
  case "$PIP_INDEX_URL_OPT" in
    http://*|https://*|file://*) ;;
    *) die "pip 索引文件首行必须是 http(s):// 或 file:// URL: $PIP_INDEX_URL_FILE" ;;
  esac
fi
secure_path BUILD_ROOT "$BUILD_ROOT"
secure_path INSTALL_ROOT "$INSTALL_ROOT"
secure_path BIN_DIR "$BIN_DIR"
if [[ -n "$RESULT_DIR" ]]; then
  secure_path RESULT_DIR "$RESULT_DIR"
fi
is_system_install_tree "$INSTALL_ROOT" \
  && die "install-root 不得指向系统目录: $INSTALL_ROOT（默认 /opt/vectordbbench）"
if ((CLI_LINK == 1)); then
  is_system_bin_tree "$BIN_DIR" \
    && die "bin-dir 不得指向系统二进制目录: $BIN_DIR（默认 /usr/local/bin）"
fi

# ---------- 运行目录与日志 ----------
run_id="$(date +%Y%m%d-%H%M%S)-$$"
[[ -n "$RESULT_DIR" ]] || RESULT_DIR="/var/log/vectordbbench-deploy/$run_id"
DEPLOY_ENV="$RESULT_DIR/env/deploy.env"
MAIN_LOG="$RESULT_DIR/logs/install.log"
WORK_DIR=""
VDB_COMMIT=""
PACKAGE_VERSION=""
VENV_DIR=""
CLI_BIN=""
CLI_LINK_PATH=""
UI_LINK_PATH=""
PIP_SOURCE="pypi"

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

if [[ -n "$WHEELHOUSE" ]]; then
  PIP_SOURCE=wheelhouse
  pip_index_display="无（wheelhouse）"
elif [[ -n "$PIP_INDEX_URL_OPT" || -n "${PIP_INDEX_URL:-}" ]]; then
  PIP_SOURCE=custom-index
  pip_index_display=$(mask_url "${PIP_INDEX_URL_OPT:-${PIP_INDEX_URL:-}}")
else
  PIP_SOURCE=pypi
  pip_index_display="无（PyPI 默认）"
fi
if [[ -n "$WHEELHOUSE" && -n "$PIP_INDEX_URL_OPT" ]]; then
  log_warn "--wheelhouse 已指定，pip 索引配置将被忽略"
fi
if [[ -n "$SOURCE_DIR" ]]; then
  version_policy="source-dir（目录自带版本）"
elif [[ "$VDB_VERSION" == "$VDB_PINNED_VERSION" ]]; then
  version_policy="内置固定版本 + commit 校验（$VDB_PINNED_COMMIT）"
else
  version_policy="用户指定 ref（记录实际 commit 与包版本）"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
[dry-run] VectorDBBench 环境部署计划（不落盘、不安装）
  VectorDBBench 版本 : $VDB_VERSION
  版本策略           : $version_policy
  源码               : ${SOURCE_DIR:-$GIT_URL}
  Python             : ${PYTHON:-自动查找 3.11+}
  安装根目录         : $INSTALL_ROOT（venv-py<PY_TAG> + current 符号链接）
  CLI 链接           : $( ((CLI_LINK == 1)) && echo "$BIN_DIR/vectordbbench" || echo "不创建" )
  数据库 extras      : ${EXTRAS:-无（仅 PyMilvus）}
  pip 来源           : $PIP_SOURCE
  pip 索引           : $pip_index_display（命令行 URL 禁止含凭据，推荐 0600 文件或环境变量）
  构建缓存           : $BUILD_ROOT（要求 root:root 0700）
  结果目录           : $RESULT_DIR
执行顺序:
  1) install_deps.sh（可 --skip-deps）
  2) 查找 Python 3.11+ → 创建/复用 venv → 升级 pip/setuptools/wheel/setuptools_scm
  3) 克隆/复制源码到 root 私有目录 → pip install .[extras]
  4) vectordbbench --help 验证 → 更新 current / 全局 CLI 链接 → 写 deploy.env
EOF
  exit 0
fi

require_root
install -d -m 0700 -o root -g root "$RESULT_DIR" "$RESULT_DIR/logs" "$RESULT_DIR/env"
exec > >(tee -a "$MAIN_LOG") 2>&1

if [[ -f "$DEPLOY_ENV" ]]; then
  mv "$DEPLOY_ENV" "$DEPLOY_ENV.previous.$run_id"
  log_info "旧 deploy.env 已备份: $DEPLOY_ENV.previous.$run_id"
fi
printf 'DEPLOY_STATUS=running\nDEPLOY_RUN_ID=%s\n' "$run_id" >"$DEPLOY_ENV"

# ---------- 1. 依赖与 Python ----------
CURRENT_PHASE=deps
if ((SKIP_DEPS == 0)); then
  log_info "阶段 1/4: 安装系统依赖"
  if ! bash "$SKILL_ROOT/scripts/install_deps.sh" >"$RESULT_DIR/logs/deps.log" 2>&1; then
    tail -n 40 "$RESULT_DIR/logs/deps.log" >&2 || true
    die "依赖安装失败，完整日志: $RESULT_DIR/logs/deps.log"
  fi
  log_ok "依赖安装完成"
else
  log_info "阶段 1/4: 跳过依赖安装（--skip-deps）"
  for cmd in gcc make tar find; do require_cmd "$cmd"; done
  [[ -n "$SOURCE_DIR" ]] || require_cmd git
fi

find_python
log_ok "Python: $PY_VERSION（$PYTHON）"

# ---------- 2. 构建目录与源码 ----------
CURRENT_PHASE=source
log_info "阶段 2/4: 获取 VectorDBBench 源码"
check_build_parents "$BUILD_ROOT"
prepare_private_dir "$BUILD_ROOT"
check_build_parents "$BUILD_ROOT"

safe_version=${VDB_VERSION//\//_}
WORK_DIR=$(mktemp -d "$BUILD_ROOT/vectordbbench-$safe_version.XXXXXX") \
  || die "无法在 $BUILD_ROOT 下创建构建目录"
SRC_DIR="$WORK_DIR/src"

if [[ -n "$SOURCE_DIR" ]]; then
  mkdir -p "$SRC_DIR"
  cp -a "$SOURCE_DIR/." "$SRC_DIR/"
  log_info "已复制离线源码: $SOURCE_DIR -> $SRC_DIR"
else
  require_cmd git
  log_info "克隆 $GIT_URL（ref: $VDB_VERSION）"
  if ! git clone --quiet "$GIT_URL" "$SRC_DIR"; then
    die "git clone 失败: $GIT_URL"
  fi
  if ! git -C "$SRC_DIR" checkout --quiet "$VDB_VERSION"; then
    die "VectorDBBench ref 不存在或无法 checkout: $VDB_VERSION"
  fi
fi

[[ -f "$SRC_DIR/pyproject.toml" ]] || die "源码目录缺少 pyproject.toml: $SRC_DIR"
[[ -d "$SRC_DIR/vectordb_bench" ]] || die "源码目录缺少 vectordb_bench/: $SRC_DIR"
bad_entry=$(find "$SRC_DIR" \( ! -user 0 -o -type f -perm /022 -o -type d -perm /022 \) -print -quit)
[[ -z "$bad_entry" ]] || die "源码目录存在非 root 属主或组/其他可写的文件/目录: $bad_entry"
if [[ -n "$WHEELHOUSE" ]]; then
  bad_wheel=$(find "$WHEELHOUSE" \( ! -user 0 -o -type f -perm /022 -o -type d -perm /022 \) -print -quit)
  [[ -z "$bad_wheel" ]] || die "wheelhouse 存在非 root 属主或组/其他可写的文件/目录: $bad_wheel"
fi

if command -v git >/dev/null 2>&1 && [[ -d "$SRC_DIR/.git" ]]; then
  VDB_COMMIT=$(git -C "$SRC_DIR" rev-parse HEAD)
else
  VDB_COMMIT=source-dir
fi
if [[ -z "$SOURCE_DIR" && "$VDB_VERSION" == "$VDB_PINNED_VERSION" ]]; then
  [[ "$VDB_COMMIT" == "$VDB_PINNED_COMMIT" ]] \
    || die "内置版本 $VDB_PINNED_VERSION 完整性校验失败：期望 commit $VDB_PINNED_COMMIT，实际 $VDB_COMMIT（请检查 --git-url、网络或镜像是否被篡改）"
  log_ok "版本完整性校验通过: $VDB_PINNED_VERSION @ $VDB_PINNED_COMMIT"
fi

# ---------- 3. venv 与 pip 安装 ----------
CURRENT_PHASE=install
log_info "阶段 3/4: 创建虚拟环境并安装 VectorDBBench"
check_parent_chain "$INSTALL_ROOT" "install-root"
prepare_root_dir "$INSTALL_ROOT" 0755
check_parent_chain "$INSTALL_ROOT" "install-root"
if ((CLI_LINK == 1)); then
  check_parent_chain "$BIN_DIR" "bin-dir"
  prepare_root_dir "$BIN_DIR" 0755
  check_parent_chain "$BIN_DIR" "bin-dir"
fi

VENV_DIR="$INSTALL_ROOT/venv-py$PY_TAG"
venv_ok=0
if [[ -x "$VENV_DIR/bin/python" ]]; then
  venv_tag=$("$VENV_DIR/bin/python" -c 'import sys; print("{}.{}".format(sys.version_info[0], sys.version_info[1]))' 2>/dev/null || true)
  if [[ "$venv_tag" == "$PY_TAG" ]]; then
    venv_ok=1
  fi
fi
if ((venv_ok == 0)) && [[ -e "$VENV_DIR" ]]; then
  log_warn "已有 venv 不可用，创建新的版本化目录（旧目录保留）: $VENV_DIR"
  VENV_DIR="$INSTALL_ROOT/venv-py$PY_TAG-$run_id"
fi
if ((venv_ok == 0)); then
  if ! "$PYTHON" -m venv "$VENV_DIR"; then
    die "创建 venv 失败: $VENV_DIR"
  fi
fi
if [[ ! -x "$VENV_DIR/bin/pip" ]]; then
  "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 \
    || die "venv 缺少 pip 且 ensurepip 失败: $VENV_DIR"
fi
CLI_BIN="$VENV_DIR/bin/vectordbbench"

CURRENT_LINK="$INSTALL_ROOT/current"
if [[ -L "$CURRENT_LINK" || -e "$CURRENT_LINK" ]]; then
  if [[ -d "$CURRENT_LINK" && ! -L "$CURRENT_LINK" ]]; then
    die "$CURRENT_LINK 是真实目录，拒绝覆盖；请人工重命名后重试"
  fi
  backup="$CURRENT_LINK.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CURRENT_LINK" "$backup"
  log_info "已备份旧 current 链接: $backup（验证通过后才切换）"
fi

pip_args=(install --no-cache-dir)
if [[ -n "$WHEELHOUSE" ]]; then
  pip_args+=(--no-index --find-links "$WHEELHOUSE")
fi
if [[ -n "$PIP_INDEX_URL_OPT" ]]; then
  export PIP_INDEX_URL="$PIP_INDEX_URL_OPT"
fi

pip_log="$RESULT_DIR/logs/pip.log"
log_info "pip 来源: $PIP_SOURCE"
if ! "$VENV_DIR/bin/pip" "${pip_args[@]}" --upgrade pip setuptools wheel setuptools_scm >"$pip_log" 2>&1; then
  tail -n 60 "$pip_log" >&2 || true
  if [[ -n "$WHEELHOUSE" ]]; then
    die "离线 wheelhouse 缺少 pip/setuptools/wheel/setuptools_scm，或安装失败；完整日志: $pip_log"
  fi
  die "pip/setuptools/wheel 安装失败，完整日志: $pip_log"
fi

target="."
[[ -n "$EXTRAS" ]] && target=".[$EXTRAS]"
isolation=()
[[ -n "$WHEELHOUSE" ]] && isolation=(--no-build-isolation)
scm_pretend=0
if [[ ! -d "$SRC_DIR/.git" ]]; then
  export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
  scm_pretend=1
fi
if ! ( cd "$SRC_DIR" && "$VENV_DIR/bin/pip" "${pip_args[@]}" "${isolation[@]}" "$target" >>"$pip_log" 2>&1 ); then
  tail -n 80 "$pip_log" >&2 || true
  die "VectorDBBench 安装失败，完整日志: $pip_log"
fi
((scm_pretend == 0)) || unset SETUPTOOLS_SCM_PRETEND_VERSION

PACKAGE_VERSION=$("$VENV_DIR/bin/pip" show vectordb-bench 2>/dev/null | awk '/^Version:/ {print $2}')
[[ -n "$PACKAGE_VERSION" ]] || die "无法读取 vectordb-bench 版本"

# ---------- 4. 验证与 CLI 链接 ----------
CURRENT_PHASE=verify
log_info "阶段 4/4: 验证 CLI 并创建链接"
if ! bash "$SKILL_ROOT/assets/smoke_check.sh" "$CLI_BIN"; then
  die "vectordbbench --help 验证失败: $CLI_BIN（current 未切换，保留原指向）"
fi
log_ok "CLI 验证通过: $CLI_BIN"

ln -sfn "$VENV_DIR" "$CURRENT_LINK"
log_ok "current -> $VENV_DIR"

if ((CLI_LINK == 1)); then
  link_cli() {
    local src=$1
    local dst=$2
    [[ -x "$src" ]] || return 0
    if [[ -e "$dst" || -L "$dst" ]]; then
      if [[ -d "$dst" && ! -L "$dst" ]]; then
        die "链接目标 $dst 是真实目录，拒绝覆盖"
      fi
      local bak="$dst.bak.$(date +%Y%m%d-%H%M%S)"
      cp -a "$dst" "$bak"
      log_info "已备份 $dst -> $bak"
    fi
    ln -sfn "$src" "$dst"
  }
  CLI_LINK_PATH="$BIN_DIR/vectordbbench"
  link_cli "$CLI_BIN" "$CLI_LINK_PATH"
  if [[ -x "$VENV_DIR/bin/init_bench" ]]; then
    UI_LINK_PATH="$BIN_DIR/init_bench"
    link_cli "$VENV_DIR/bin/init_bench" "$UI_LINK_PATH"
  fi
  if ! "$CLI_LINK_PATH" --help >/dev/null 2>&1; then
    die "全局 CLI 链接验证失败: $CLI_LINK_PATH"
  fi
  log_ok "全局 CLI: $CLI_LINK_PATH"
fi

CURRENT_PHASE=finalize
version_label=$VDB_VERSION
[[ -n "$SOURCE_DIR" ]] && version_label=source-dir
{
  printf 'DEPLOY_STATUS=ok\n'
  printf 'DEPLOY_RUN_ID=%s\n' "$run_id"
  printf 'VECTORDBBENCH_VERSION=%s\n' "$version_label"
  printf 'VECTORDBBENCH_COMMIT=%s\n' "$VDB_COMMIT"
  printf 'VECTORDBBENCH_PACKAGE_VERSION=%s\n' "$PACKAGE_VERSION"
  printf 'VECTORDBBENCH_SOURCE=%s\n' "${SOURCE_DIR:-$GIT_URL}"
  printf 'VECTORDBBENCH_EXTRAS=%s\n' "${EXTRAS:-none}"
  printf 'VECTORDBBENCH_PYTHON=%s\n' "$PYTHON"
  printf 'VECTORDBBENCH_PYTHON_VERSION=%s\n' "$PY_VERSION"
  printf 'VECTORDBBENCH_VENV=%s\n' "$VENV_DIR"
  printf 'VECTORDBBENCH_CLI=%s\n' "$CLI_BIN"
  printf 'VECTORDBBENCH_CLI_LINK=%s\n' "${CLI_LINK_PATH:-none}"
  printf 'VECTORDBBENCH_UI_LINK=%s\n' "${UI_LINK_PATH:-none}"
  printf 'VECTORDBBENCH_PIP_SOURCE=%s\n' "$PIP_SOURCE"
  printf 'VECTORDBBENCH_WORK_DIR=%s\n' "$WORK_DIR"
  printf 'VECTORDBBENCH_VERIFY=ok\n'
  printf 'DEPLOY_TIME=%s\n' "$(date -Is)"
} >"$DEPLOY_ENV"

log_ok "VectorDBBench 环境部署完成"
log_info "包版本: $PACKAGE_VERSION（ref: $version_label, commit: $VDB_COMMIT）"
log_info "Python: $PY_VERSION（$PYTHON）"
log_info "venv: $VENV_DIR"
log_info "CLI: ${CLI_LINK_PATH:-$CLI_BIN}"
log_info "UI: ${UI_LINK_PATH:-$VENV_DIR/bin/init_bench}"
log_info "产物: $DEPLOY_ENV"
log_info "提示: 数据集与结果目录需可写，运行前按实际 DB 安装对应 extras（如 --extras pgvector）"
exit 0
