#!/usr/bin/env bash
#===============================================================
# install_openviking.sh — OpenViking 服务端安装部署
#
# 默认行为:
#   - 版本 main（https://github.com/volcengine/OpenViking.git；master 为历史别名）
#   - 系统依赖 + Rust 工具链（需 --install-rust 或已预装）
#   - 独立 release venv 中构建并安装 OpenViking
#   - 生成/接管 /etc/openviking/ov.conf
#   - systemd 托管 openviking.service
#   - openviking-server doctor + /health + /ready 验收
#   - 写 deploy.env；旧 release 保留用于回滚
#
# 版本策略:
#   - 未指定 --version 时安装上游默认分支 main（master 为历史别名），并记录实际 commit
#   - 指定 tag/分支/commit 时按 ref checkout；可选 --verify-signature 做 GPG 校验
#
# 安全:
#   - 构建目录 root:root 0700，父目录属主/sticky 校验
#   - 路径拒绝 .、..、//，realpath 归一化后做系统目录黑名单
#   - --git-url 仅允许 http(s)://；本地源码必须走 --source-dir
#   - API key 只从 600 文件或用户 ov.conf 读取，不进入 argv/日志
#   - 非回环监听必须提供 root API key
#===============================================================
set -euo pipefail
umask 022

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

OPENVIKING_VERSION=main
GIT_URL=https://github.com/volcengine/OpenViking.git
SOURCE_DIR=""
WHEEL=""
PREFIX=/opt/openviking
BUILD_ROOT=/var/cache/openviking-deploy
RESULT_DIR=""
RUN_AS=openviking
CONF_DIR=/etc/openviking
DATA_DIR=/var/lib/openviking
SERVICE_NAME=openviking
OV_CONF=""
PROVIDER=""
API_BASE=""
EMBEDDING_MODEL=""
VLM_MODEL=""
DIMENSION=""
INPUT_MODE=""
API_VERSION=""
API_KEY_FILE=""
ROOT_API_KEY_FILE=""
WORKSPACE=""
HOST=127.0.0.1
PORT=1933
WITH_STUDIO=0
SKIP_STUDIO=0
INSTALL_RUST=0
SKIP_DEPS=0
NO_SYSTEMD=0
PIP_INDEX_URL=""
READY_TIMEOUT=180
VERIFY_SIGNATURE=0
HOST_SET=0
PORT_SET=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: install_openviking.sh [options]

版本与源码:
  --version REF            版本/分支/commit（默认 main；master 为历史别名）
  --verify-signature       对 tag/commit 做 GPG 签名校验（需已导入公钥）
  --git-url URL            源码仓库，仅支持 http(s):// 且不得含 userinfo（默认官方仓库）；
                           本地源码请用 --source-dir，预编译包用 --wheel
  --source-dir DIR         离线/本地源码目录（要求 root 属主、组/其他不可写）
  --wheel FILE             预编译 wheel（跳过源码构建）

安装布局:
  --prefix DIR             安装前缀（默认 /opt/openviking）
  --build-root DIR         构建缓存（默认 /var/cache/openviking-deploy，要求 root:root 0700）
  --result-dir DIR         日志与 deploy.env 输出（默认 /var/log/openviking-deploy/<run_id>）
  --run-as USER            运行用户（默认 openviking，不存在则创建系统用户）
  --conf-dir DIR           配置目录（默认 /etc/openviking）
  --data-dir DIR           数据根目录（默认 /var/lib/openviking）
  --service-name NAME      服务名（默认 openviking）

模型配置:
  --ov-conf FILE           接管已有 ov.conf（校验后安装）
  --provider NAME          生成配置时的 provider：volcengine/openai/azure/ollama
  --api-base URL           Embedding 与 VLM 的 API Base（可按 provider 默认）
  --embedding-model NAME   Embedding 模型名
  --vlm-model NAME         VLM 模型名
  --dimension N            Embedding 维度（可选）
  --input-mode MODE        Embedding input：text/multimodal（可选，volcengine）
  --api-version VER        Azure API 版本（可选）
  --api-key-file FILE      API key 文件（要求 0600，内容不打印）
  --root-api-key-file FILE 非回环监听时的 root API key 文件（要求 0600）
  --workspace DIR          storage.workspace（默认 <data-dir>/data）
  --host ADDR              监听地址（默认 127.0.0.1）
  --port PORT              监听端口（默认 1933）

执行控制:
  --with-studio            安装 Node.js 并构建 Web Studio
  --skip-studio            跳过 Web Studio 构建
  --install-rust           允许脚本用 rustup-init（sha256 校验）安装 Rust
  --skip-deps              跳过系统依赖安装
  --no-systemd             不使用 systemd，以后台进程方式启动
  --pip-index-url URL      私有 pip 源（禁止携带用户名/密码）
  --ready-timeout SEC      等待 /ready 的超时（默认 180）
  --dry-run                打印计划，不落盘、不启动服务
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) OPENVIKING_VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --verify-signature) VERIFY_SIGNATURE=1; shift ;;
    --git-url) GIT_URL=${2:?--git-url 缺少参数}; shift 2 ;;
    --source-dir) SOURCE_DIR=${2:?--source-dir 缺少参数}; shift 2 ;;
    --wheel) WHEEL=${2:?--wheel 缺少参数}; shift 2 ;;
    --prefix) PREFIX=${2:?--prefix 缺少参数}; shift 2 ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --result-dir) RESULT_DIR=${2:?--result-dir 缺少参数}; shift 2 ;;
    --run-as) RUN_AS=${2:?--run-as 缺少参数}; shift 2 ;;
    --conf-dir) CONF_DIR=${2:?--conf-dir 缺少参数}; shift 2 ;;
    --data-dir) DATA_DIR=${2:?--data-dir 缺少参数}; shift 2 ;;
    --service-name) SERVICE_NAME=${2:?--service-name 缺少参数}; shift 2 ;;
    --ov-conf) OV_CONF=${2:?--ov-conf 缺少参数}; shift 2 ;;
    --provider) PROVIDER=${2:?--provider 缺少参数}; shift 2 ;;
    --api-base) API_BASE=${2:?--api-base 缺少参数}; shift 2 ;;
    --embedding-model) EMBEDDING_MODEL=${2:?--embedding-model 缺少参数}; shift 2 ;;
    --vlm-model) VLM_MODEL=${2:?--vlm-model 缺少参数}; shift 2 ;;
    --dimension) DIMENSION=${2:?--dimension 缺少参数}; shift 2 ;;
    --input-mode) INPUT_MODE=${2:?--input-mode 缺少参数}; shift 2 ;;
    --api-version) API_VERSION=${2:?--api-version 缺少参数}; shift 2 ;;
    --api-key-file) API_KEY_FILE=${2:?--api-key-file 缺少参数}; shift 2 ;;
    --root-api-key-file) ROOT_API_KEY_FILE=${2:?--root-api-key-file 缺少参数}; shift 2 ;;
    --workspace) WORKSPACE=${2:?--workspace 缺少参数}; shift 2 ;;
    --host) HOST=${2:?--host 缺少参数}; HOST_SET=1; shift 2 ;;
    --port) PORT=${2:?--port 缺少参数}; PORT_SET=1; shift 2 ;;
    --with-studio) WITH_STUDIO=1; shift ;;
    --skip-studio) SKIP_STUDIO=1; shift ;;
    --install-rust) INSTALL_RUST=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --pip-index-url) PIP_INDEX_URL=${2:?--pip-index-url 缺少参数}; shift 2 ;;
    --ready-timeout) READY_TIMEOUT=${2:?--ready-timeout 缺少参数}; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

# ---------- 参数校验 ----------
if [[ "$OPENVIKING_VERSION" == "master" ]]; then
  log_warn "上游已无 master 分支，按 main（历史别名）处理"
  OPENVIKING_VERSION=main
fi
[[ "$OPENVIKING_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
  || die "版本/ref 非法: $OPENVIKING_VERSION"
[[ "$OPENVIKING_VERSION" != *".."* && "$OPENVIKING_VERSION" != *"//"* && "$OPENVIKING_VERSION" != */ ]] \
  || die "版本/ref 非法: $OPENVIKING_VERSION"
[[ "$RUN_AS" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "运行用户非法: $RUN_AS"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && "$SERVICE_NAME" != *".."* ]] \
  || die "服务名非法: $SERVICE_NAME"
SERVICE_NAME=${SERVICE_NAME%.service}
[[ -n "$SERVICE_NAME" ]] || die "服务名非法: 不能为空"
[[ "$HOST" =~ ^[A-Za-z0-9.:-]+$ ]] || die "监听地址非法: $HOST"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || die "端口非法: $PORT"
[[ "$READY_TIMEOUT" =~ ^[0-9]+$ ]] && ((READY_TIMEOUT >= 10)) || die "ready-timeout 非法: $READY_TIMEOUT"
[[ "$DIMENSION" =~ ^[0-9]*$ ]] || die "dimension 非法: $DIMENSION"
if [[ "$GIT_URL" != http://* && "$GIT_URL" != https://* ]]; then
  die "git-url 仅支持 http(s):// URL；本地源码请使用 --source-dir"
fi
url_has_userinfo "$GIT_URL" \
  && die "git-url 不得携带用户名/密码（userinfo）；请改用 SSH remote 或凭据助手"
[[ "$GIT_URL" == *"?"* || "$GIT_URL" == *"#"* ]] \
  && die "git-url 不得包含查询串或片段（避免令牌进入日志）；请改用 SSH remote 或凭据助手"
if [[ -n "$PIP_INDEX_URL" ]]; then
  [[ "$PIP_INDEX_URL" == http://* || "$PIP_INDEX_URL" == https://* ]] \
    || die "pip-index-url 仅支持 http(s)://"
  url_has_userinfo "$PIP_INDEX_URL" && die "pip-index-url 不得携带用户名/密码"
fi
if [[ -n "$SOURCE_DIR" && "$OPENVIKING_VERSION" != "main" ]]; then
  die "--source-dir 使用目录自带版本，不能再指定 --version $OPENVIKING_VERSION（默认 main 即可）"
fi
if [[ -n "$SOURCE_DIR" && -n "$WHEEL" ]]; then
  die "--source-dir 与 --wheel 不能同时使用"
fi
if [[ "$WITH_STUDIO" == "1" && "$SKIP_STUDIO" == "1" ]]; then
  die "--with-studio 与 --skip-studio 不能同时使用"
fi

PREFIX=$(trim_slash "$PREFIX")
BUILD_ROOT=$(trim_slash "$BUILD_ROOT")
CONF_DIR=$(trim_slash "$CONF_DIR")
DATA_DIR=$(trim_slash "$DATA_DIR")
secure_path PREFIX "$PREFIX"
secure_path BUILD_ROOT "$BUILD_ROOT"
secure_path CONF_DIR "$CONF_DIR"
secure_path DATA_DIR "$DATA_DIR"
if [[ -n "$RESULT_DIR" ]]; then
  RESULT_DIR=$(trim_slash "$RESULT_DIR")
  secure_path RESULT_DIR "$RESULT_DIR"
fi
if [[ -z "$WORKSPACE" ]]; then
  WORKSPACE="$DATA_DIR/data"
fi
WORKSPACE=$(trim_slash "$WORKSPACE")
secure_path WORKSPACE "$WORKSPACE"

if [[ -n "$SOURCE_DIR" ]]; then
  SOURCE_DIR=$(trim_slash "$SOURCE_DIR")
  secure_path SOURCE_DIR "$SOURCE_DIR"
  [[ -d "$SOURCE_DIR" ]] || die "源码目录不存在: $SOURCE_DIR"
  owner=$(stat -c '%u' "$SOURCE_DIR")
  mode=$(stat -c '%a' "$SOURCE_DIR")
  [[ "$owner" == "0" ]] || die "源码目录必须属于 root（当前 uid=$owner）: $SOURCE_DIR"
  (( (8#$mode & 0022) == 0 )) || die "源码目录不得组/其他可写（当前 $mode）: $SOURCE_DIR"
  check_trusted_parents "源码目录" "$SOURCE_DIR"
  [[ -f "$SOURCE_DIR/pyproject.toml" && -f "$SOURCE_DIR/setup.py" ]] \
    || die "源码目录缺少 pyproject.toml 或 setup.py: $SOURCE_DIR"
fi
if [[ -n "$WHEEL" ]]; then
  [[ -f "$WHEEL" ]] || die "wheel 不存在: $WHEEL"
  owner=$(stat -c '%u' "$WHEEL")
  mode=$(stat -c '%a' "$WHEEL")
  [[ "$owner" == "0" ]] || die "wheel 必须属于 root（当前 uid=$owner）: $WHEEL"
  (( (8#$mode & 0022) == 0 )) || die "wheel 不得组/其他可写（当前 $mode）: $WHEEL"
  check_trusted_parents "wheel" "$WHEEL"
fi

if [[ -n "$OV_CONF" ]]; then
  [[ -f "$OV_CONF" ]] || die "ov.conf 不存在: $OV_CONF"
  mode=$(stat -c '%a' "$OV_CONF")
  (( (8#$mode & 0077) == 0 )) || log_warn "ov.conf 权限过宽（$mode），建议先执行: chmod 600 $OV_CONF"
  command -v python3 >/dev/null 2>&1 || die "缺少 python3，无法校验 ov.conf"
  python3 "$SKILL_ROOT/scripts/gen_ov_conf.py" --check "$OV_CONF" >/dev/null \
    || die "ov.conf 校验失败: $OV_CONF"
  conf_info=$(python3 - "$OV_CONF" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
server = doc.get("server") or {}
storage = doc.get("storage") or {}
print(server.get("host") or "127.0.0.1")
print(server.get("port") or 1933)
print(1 if server.get("root_api_key") else 0)
print(storage.get("workspace") or "")
PY
  )
  conf_host=$(sed -n '1p' <<<"$conf_info")
  conf_port=$(sed -n '2p' <<<"$conf_info")
  conf_has_root=$(sed -n '3p' <<<"$conf_info")
  conf_workspace=$(sed -n '4p' <<<"$conf_info")
  if [[ "$HOST_SET" == "1" && "$HOST" != "$conf_host" ]]; then
    log_warn "--host 与 ov.conf server.host 不一致，探测以配置为准: $conf_host"
  fi
  if [[ "$PORT_SET" == "1" && "$PORT" != "$conf_port" ]]; then
    log_warn "--port 与 ov.conf server.port 不一致，探测以配置为准: $conf_port"
  fi
  HOST=$conf_host
  PORT=$conf_port
  [[ "$HOST" =~ ^[A-Za-z0-9.:-]+$ ]] || die "ov.conf server.host 非法: $HOST"
  [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) \
    || die "ov.conf server.port 非法: $PORT"
  if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" && "$conf_has_root" != "1" ]]; then
    die "ov.conf 监听 $HOST 不是回环地址，但未配置 server.root_api_key"
  fi
  if [[ -n "$conf_workspace" ]]; then
    if [[ "$conf_workspace" == /* ]]; then
      WORKSPACE="$conf_workspace"
    else
      [[ "$conf_workspace" == *".."* ]] \
        && die "ov.conf storage.workspace 相对路径不得包含 ..: $conf_workspace"
      WORKSPACE=$(normalize_path "$DATA_DIR/$conf_workspace")
    fi
    WORKSPACE=$(trim_slash "$WORKSPACE")
    secure_path WORKSPACE "$WORKSPACE"
    log_info "采用 ov.conf storage.workspace: $WORKSPACE"
  fi
else
  [[ -n "$PROVIDER" ]] || die "未提供 --ov-conf，必须提供 --provider/--embedding-model/--vlm-model（先与用户确认，不要猜测）"
  [[ -n "$EMBEDDING_MODEL" ]] || die "缺少 --embedding-model"
  [[ -n "$VLM_MODEL" ]] || die "缺少 --vlm-model"
  if [[ "$PROVIDER" != "ollama" ]]; then
    [[ -n "$API_KEY_FILE" ]] || die "provider $PROVIDER 需要 --api-key-file（0600）"
  fi
  if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
    [[ -n "$ROOT_API_KEY_FILE" ]] || die "监听 $HOST 不是回环地址，必须提供 --root-api-key-file 开启鉴权"
  fi
fi
if [[ -n "$API_KEY_FILE" ]]; then
  check_secret_file "$API_KEY_FILE"
fi
if [[ -n "$ROOT_API_KEY_FILE" ]]; then
  check_secret_file "$ROOT_API_KEY_FILE"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
[dry-run] OpenViking 环境部署计划（不落盘、不启动服务）
  版本          : $OPENVIKING_VERSION
  源码          : ${SOURCE_DIR:-${WHEEL:-$GIT_URL}}
  安装前缀      : $PREFIX
  release       : $PREFIX/releases/<commit12>
  当前链接      : $PREFIX/current
  配置          : ${OV_CONF:-生成 $CONF_DIR/ov.conf}
  运行用户      : $RUN_AS
  数据目录      : $DATA_DIR
  workspace     : $WORKSPACE
  监听          : $HOST:$PORT
  服务          : $([[ "$NO_SYSTEMD" == "1" ]] && echo "后台进程（--no-systemd）" || echo "$SERVICE_NAME.service")
  构建缓存      : $BUILD_ROOT（要求 root:root 0700）
  Web Studio    : $([[ "$SKIP_STUDIO" == "1" ]] && echo skip || echo "按 npm 可用性构建" )
  Rust          : $([[ "$INSTALL_RUST" == "1" ]] && echo "允许安装 Rust" || echo "要求已预装" )
  依赖安装      : $([[ "$SKIP_DEPS" == "1" ]] && echo skip || echo yes)
  结果目录      : ${RESULT_DIR:-/var/log/openviking-deploy/<run_id>}
执行顺序:
  1) install_deps.sh（可 --skip-deps / --install-rust / --with-studio）
  2) 获取源码（git main/tag 或 --source-dir）或使用 --wheel
  3) 创建 release venv → 构建 wheel → pip 安装
  4) 生成/接管 ov.conf（0640 root:<运行组>）
  5) 安装 systemd 服务（或后台进程）→ doctor
  6) /health + /ready 验收 → 写 deploy.env
EOF
  exit 0
fi

# ---------- 运行目录与日志 ----------
run_id="$(date +%Y%m%d-%H%M%S)-$$"
[[ -n "$RESULT_DIR" ]] || RESULT_DIR="/var/log/openviking-deploy/$run_id"
DEPLOY_ENV="$RESULT_DIR/env/deploy.env"
MAIN_LOG="$RESULT_DIR/logs/install.log"
WORK_DIR=""
RELEASE_DIR=""
VENV_DIR=""
WHEEL_PATH=""
OPENVIKING_COMMIT=""
OPENVIKING_BRANCH=""
OPENVIKING_PACKAGE_VERSION=""
SIGNATURE_VERIFIED=0
SCM_PRETEND_VERSION=""
CONF_FILE="$CONF_DIR/ov.conf"
RUN_GROUP=""
PROBE_HOST="$HOST"
CURRENT_PHASE=validate

set_deploy_failed() {
  local rc=$1
  local tmp
  tmp=$(mktemp) || return 0
  awk '!/^DEPLOY_STATUS=/' "$DEPLOY_ENV" >"$tmp" 2>/dev/null || true
  {
    printf 'DEPLOY_STATUS=failed\n'
    cat "$tmp"
    printf 'FAILED_PHASE=%s\n' "$CURRENT_PHASE"
    printf 'FAILED_EXIT_CODE=%s\n' "$rc"
    printf 'FAILED_LOG=%s\n' "$MAIN_LOG"
  } >"$DEPLOY_ENV" 2>/dev/null || true
  rm -f "$tmp"
}

on_exit() {
  local rc=$?
  if ((rc != 0)); then
    if [[ -n "${DEPLOY_ENV:-}" && -f "$DEPLOY_ENV" ]]; then
      set_deploy_failed "$rc"
    fi
    if [[ -n "${RESULT_DIR:-}" && -d "$RESULT_DIR" ]]; then
      chmod -R u=rwX,g=rX,o= "$RESULT_DIR/logs" "$RESULT_DIR/env" 2>/dev/null || true
    fi
    if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
      log_warn "构建目录保留供排查: $WORK_DIR"
    fi
    log_error "部署失败（阶段: $CURRENT_PHASE；日志: ${MAIN_LOG:-unknown}）"
  fi
}
trap on_exit EXIT

require_root
mkdir -p "$RESULT_DIR/logs" "$RESULT_DIR/env"
chmod 0750 "$RESULT_DIR/logs" "$RESULT_DIR/env" 2>/dev/null || true
for f in deps.log pip-build-deps.log build.log pip-install.log doctor.log health.json ready.json; do
  [[ -f "$RESULT_DIR/logs/$f" ]] || install -m 0640 /dev/null "$RESULT_DIR/logs/$f"
  chmod 0640 "$RESULT_DIR/logs/$f" 2>/dev/null || true
done
[[ -f "$MAIN_LOG" ]] || install -m 0640 /dev/null "$MAIN_LOG"
chmod 0640 "$MAIN_LOG" 2>/dev/null || true
exec > >(tee -a "$MAIN_LOG") 2>&1

if [[ -f "$DEPLOY_ENV" ]]; then
  mv "$DEPLOY_ENV" "$DEPLOY_ENV.previous.$run_id"
  log_info "旧 deploy.env 已备份: $DEPLOY_ENV.previous.$run_id"
fi
[[ -f "$DEPLOY_ENV" ]] || install -m 0640 /dev/null "$DEPLOY_ENV"
chmod 0640 "$DEPLOY_ENV" 2>/dev/null || true
printf 'DEPLOY_STATUS=running\nDEPLOY_RUN_ID=%s\n' "$run_id" >"$DEPLOY_ENV"

export PATH="/usr/local/cargo/bin:$PATH"
[[ -d /usr/local/rustup ]] && export RUSTUP_HOME=/usr/local/rustup
[[ -d /usr/local/cargo ]] && export CARGO_HOME=/usr/local/cargo
export PIP_DISABLE_PIP_VERSION_CHECK=1
export GIT_TERMINAL_PROMPT=0

# ---------- 依赖 ----------
CURRENT_PHASE=deps
if ((SKIP_DEPS == 0)); then
  log_info "阶段 1/7: 安装系统依赖"
  deps_args=()
  [[ "$INSTALL_RUST" == "1" ]] && deps_args+=(--install-rust)
  [[ -n "$WHEEL" ]] && deps_args+=(--skip-rust)
  [[ "$WITH_STUDIO" == "1" ]] && deps_args+=(--with-studio)
  if ! bash "$SKILL_ROOT/scripts/install_deps.sh" "${deps_args[@]}" >"$RESULT_DIR/logs/deps.log" 2>&1; then
    tail -n 40 "$RESULT_DIR/logs/deps.log" >&2 || true
    die "依赖安装失败，完整日志: $RESULT_DIR/logs/deps.log"
  fi
  log_ok "系统依赖安装完成"
else
  log_info "阶段 1/7: 跳过依赖安装（--skip-deps）"
  for cmd in git python3 make tar find curl; do require_cmd "$cmd"; done
  if [[ -z "$WHEEL" ]]; then
    require_cmd cmake
    require_cmd cargo
    command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 \
      || die "缺少 C++ 编译器（要求 g++ >= 9 或 clang++ >= 11）"
  fi
fi

PYTHON_BIN=$(command -v python3 2>/dev/null || true)
[[ -n "$PYTHON_BIN" ]] || die "未找到 python3"
PY_VER=$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
version_ge "$PY_VER" "3.10.0" || die "Python 版本过低: $PY_VER（要求 >= 3.10）"

BUILD_STUDIO=0
if [[ "$SKIP_STUDIO" == "0" ]] && command -v npm >/dev/null 2>&1; then
  BUILD_STUDIO=1
elif [[ "$WITH_STUDIO" == "1" && "$SKIP_STUDIO" == "0" ]]; then
  log_warn "未找到 npm，跳过 Web Studio 构建（/studio 不可用）"
fi

# ---------- 源码 ----------
CURRENT_PHASE=source
log_info "阶段 2/7: 获取 OpenViking 源码"
check_build_parents "$BUILD_ROOT"
prepare_private_dir "$BUILD_ROOT"
check_build_parents "$BUILD_ROOT"

safe_version=${OPENVIKING_VERSION//\//_}
WORK_DIR=$(mktemp -d "$BUILD_ROOT/openviking-$safe_version.XXXXXX") \
  || die "无法在 $BUILD_ROOT 下创建构建目录"
SRC_DIR="$WORK_DIR/src"

if [[ -n "$WHEEL" ]]; then
  log_info "使用预编译 wheel: $WHEEL"
  mkdir -p "$WORK_DIR/artifacts"
  cp -a "$WHEEL" "$WORK_DIR/artifacts/"
  WHEEL_PATH="$WORK_DIR/artifacts/$(basename "$WHEEL")"
  OPENVIKING_COMMIT=wheel
elif [[ -n "$SOURCE_DIR" ]]; then
  mkdir -p "$SRC_DIR"
  cp -a "$SOURCE_DIR/." "$SRC_DIR/"
  log_info "已复制离线源码: $SOURCE_DIR -> $SRC_DIR"
else
  require_cmd git
  log_info "克隆 $GIT_URL（ref: $OPENVIKING_VERSION）"
  if ! git clone --quiet "$GIT_URL" "$SRC_DIR"; then
    die "git clone 失败: $GIT_URL"
  fi
  if [[ "$OPENVIKING_VERSION" == "main" ]]; then
    if git -C "$SRC_DIR" show-ref --verify --quiet refs/heads/main; then
      git -C "$SRC_DIR" checkout --quiet main
    else
      log_info "仓库本地无 main 分支；按克隆后的默认分支安装并记录实际 commit"
    fi
  elif ! git -C "$SRC_DIR" checkout --quiet "$OPENVIKING_VERSION" 2>/dev/null; then
    if git -C "$SRC_DIR" fetch --quiet origin "$OPENVIKING_VERSION" 2>/dev/null; then
      git -C "$SRC_DIR" checkout --quiet FETCH_HEAD
    else
      die "ref 不存在或无法 checkout: $OPENVIKING_VERSION"
    fi
  fi
fi

if [[ -z "$WHEEL" ]]; then
  [[ -f "$SRC_DIR/pyproject.toml" && -f "$SRC_DIR/setup.py" ]] \
    || die "源码目录缺少 pyproject.toml 或 setup.py: $SRC_DIR"
  bad_entry=$(find "$SRC_DIR" \( ! -user 0 -o -type f -perm /022 -o -type d -perm /022 \) -print -quit)
  [[ -z "$bad_entry" ]] || die "源码目录存在非 root 属主或组/其他可写的文件/目录: $bad_entry"
  if command -v git >/dev/null 2>&1 && [[ -e "$SRC_DIR/.git" ]]; then
    OPENVIKING_COMMIT=$(git -C "$SRC_DIR" rev-parse HEAD)
    OPENVIKING_BRANCH=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)
    [[ "$OPENVIKING_BRANCH" == "HEAD" ]] && OPENVIKING_BRANCH="detached:${OPENVIKING_COMMIT:0:12}"
  else
    OPENVIKING_COMMIT=source-dir
    OPENVIKING_BRANCH=source-dir
  fi
fi
[[ -n "$OPENVIKING_BRANCH" ]] || OPENVIKING_BRANCH=wheel

if [[ "$VERIFY_SIGNATURE" == "1" ]]; then
  [[ -z "$WHEEL" ]] || die "--verify-signature 不适用于 --wheel（请在源码部署时校验 tag/commit）"
  if [[ -n "$SOURCE_DIR" ]]; then
    log_info "校验源码签名（source-dir HEAD）"
  else
    log_info "校验源码签名（ref: $OPENVIKING_VERSION）"
  fi
  if [[ -n "$SOURCE_DIR" ]]; then
    [[ -e "$SRC_DIR/.git" ]] \
      || die "--verify-signature 要求 --source-dir 是包含 .git 的 Git 工作树"
    tag_at_head=""
    for tag in $(git -C "$SRC_DIR" tag --points-at HEAD); do
      if git -C "$SRC_DIR" verify-tag "$tag" 2>/dev/null; then
        tag_at_head=$tag
        break
      fi
    done
    if [[ -z "$tag_at_head" ]]; then
      git -C "$SRC_DIR" verify-commit HEAD || die "commit 签名校验失败: $OPENVIKING_COMMIT"
    fi
    clean_src="$WORK_DIR/clean-src"
    mkdir -p "$clean_src"
    git -C "$SRC_DIR" archive --format=tar HEAD | tar -x -C "$clean_src" \
      || die "从验签对象生成干净构建树失败"
    bad_entry=$(find "$clean_src" \( ! -user 0 -o -type f -perm /022 -o -type d -perm /022 \) -print -quit)
    [[ -z "$bad_entry" ]] || die "干净构建树存在非 root 属主或组/其他可写: $bad_entry"
    SRC_DIR="$clean_src"
    if [[ "$tag_at_head" =~ ^v?[0-9]+(\.[0-9]+)*$ ]]; then
      SCM_PRETEND_VERSION=${tag_at_head#v}
    else
      SCM_PRETEND_VERSION="0.0.0+source"
    fi
    log_info "从验签对象生成干净构建树（commit: $OPENVIKING_COMMIT，版本: $SCM_PRETEND_VERSION）"
  else
    target_commit=$(git -C "$SRC_DIR" rev-parse "$OPENVIKING_VERSION^{commit}") \
      || die "无法把 ref 解析为 commit: $OPENVIKING_VERSION"
    head_commit=$(git -C "$SRC_DIR" rev-parse HEAD)
    [[ "$target_commit" == "$head_commit" ]] \
      || die "checkout HEAD 与请求 ref 不一致（ref=$target_commit head=$head_commit）"
    if git -C "$SRC_DIR" show-ref --verify --quiet "refs/tags/$OPENVIKING_VERSION"; then
      git -C "$SRC_DIR" verify-tag "$OPENVIKING_VERSION" || die "tag 签名校验失败: $OPENVIKING_VERSION"
    else
      git -C "$SRC_DIR" verify-commit HEAD || die "commit 签名校验失败: $OPENVIKING_VERSION"
    fi
  fi
  SIGNATURE_VERIFIED=1
fi

if [[ "$OPENVIKING_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  release_id=${OPENVIKING_COMMIT:0:12}
elif [[ -n "$WHEEL" ]]; then
  release_id="wheel-$(sha256sum "$WHEEL" | awk '{print substr($1,1,12)}')"
else
  release_id="source-$(date +%Y%m%d-%H%M%S)"
fi
RELEASE_DIR="$PREFIX/releases/$release_id"
VENV_DIR="$RELEASE_DIR/venv"

# ---------- 构建 ----------
CURRENT_PHASE=build
log_info "阶段 3/7: 构建并安装 OpenViking（release: $release_id）"
if [[ -e "$PREFIX/current" && ! -L "$PREFIX/current" ]]; then
  die "$PREFIX/current 已存在且不是符号链接；请人工确认后移走，避免覆盖安装"
fi
install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/releases" "$RELEASE_DIR"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  log_info "创建 venv: $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || die "创建 venv 失败: $VENV_DIR"
fi
VENV_BIN="$VENV_DIR/bin"
PIP_ENV=(env "PATH=$VENV_BIN:$PATH" "PIP_DISABLE_PIP_VERSION_CHECK=1")
[[ -n "$PIP_INDEX_URL" ]] && PIP_ENV+=("PIP_INDEX_URL=$PIP_INDEX_URL")
[[ -d /usr/local/rustup ]] && PIP_ENV+=("RUSTUP_HOME=/usr/local/rustup")
[[ -d /usr/local/cargo ]] && PIP_ENV+=("CARGO_HOME=/usr/local/cargo")
if ! command -v g++ >/dev/null 2>&1 && command -v clang++ >/dev/null 2>&1; then
  PIP_ENV+=("CC=clang" "CXX=clang++")
  log_info "未找到 g++，使用 clang++ 构建原生扩展"
fi

if [[ -z "$WHEEL" ]]; then
  build_deps=(pip setuptools wheel setuptools-scm maturin)
  if ! "${PIP_ENV[@]}" "$VENV_BIN/python" -m pip install --upgrade "${build_deps[@]}" \
    >"$RESULT_DIR/logs/pip-build-deps.log" 2>&1; then
    tail -n 60 "$RESULT_DIR/logs/pip-build-deps.log" >&2 || true
    die "安装构建依赖失败，完整日志: $RESULT_DIR/logs/pip-build-deps.log"
  fi
else
  "$VENV_BIN/python" -m pip --version >/dev/null 2>&1 || die "venv 缺少 pip: $VENV_BIN"
  log_info "wheel 模式：跳过源码构建依赖安装"
fi

if [[ -z "$WHEEL_PATH" ]]; then
  mkdir -p "$WORK_DIR/artifacts"
  build_env=("${PIP_ENV[@]}")
  [[ "$BUILD_STUDIO" == "1" ]] || build_env+=("OV_SKIP_STUDIO_BUILD=1")
  if [[ -n "$SCM_PRETEND_VERSION" ]]; then
    build_env+=("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENVIKING=$SCM_PRETEND_VERSION" \
      "OPENVIKING_VERSION=$SCM_PRETEND_VERSION")
    log_info "构建包版本: $SCM_PRETEND_VERSION"
  elif [[ -n "$SOURCE_DIR" && ! -e "$SRC_DIR/.git" ]]; then
    build_env+=("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENVIKING=0.0.0+source" \
      "OPENVIKING_VERSION=0.0.0+source")
    log_warn "离线源码目录不含 .git，包版本记录为 0.0.0+source；需要精确版本请使用 tag 源码或 --wheel"
  fi
  if ! "${build_env[@]}" "$VENV_BIN/python" -m pip wheel --no-deps --no-build-isolation \
    -w "$WORK_DIR/artifacts" "$SRC_DIR" >"$RESULT_DIR/logs/build.log" 2>&1; then
    tail -n 80 "$RESULT_DIR/logs/build.log" >&2 || true
    die "OpenViking 构建失败，完整日志: $RESULT_DIR/logs/build.log"
  fi
  WHEEL_PATH=$(find "$WORK_DIR/artifacts" -maxdepth 1 -name 'openviking-*.whl' -print | sort | tail -n 1)
  [[ -n "$WHEEL_PATH" ]] || die "构建未产出 openviking-*.whl，查看: $RESULT_DIR/logs/build.log"
fi
log_ok "wheel 产物: $WHEEL_PATH"

if ! "${PIP_ENV[@]}" "$VENV_BIN/python" -m pip install --upgrade "$WHEEL_PATH" \
  >"$RESULT_DIR/logs/pip-install.log" 2>&1; then
  tail -n 80 "$RESULT_DIR/logs/pip-install.log" >&2 || true
  die "OpenViking 安装失败，完整日志: $RESULT_DIR/logs/pip-install.log"
fi
if ! "${PIP_ENV[@]}" "$VENV_BIN/python" -m pip install --force-reinstall --no-deps "$WHEEL_PATH" \
  >>"$RESULT_DIR/logs/pip-install.log" 2>&1; then
  tail -n 80 "$RESULT_DIR/logs/pip-install.log" >&2 || true
  die "OpenViking wheel 强制重装失败，完整日志: $RESULT_DIR/logs/pip-install.log"
fi
for bin in openviking-server ov openviking; do
  [[ -x "$VENV_BIN/$bin" ]] || die "安装后缺少入口: $VENV_BIN/$bin"
done
OPENVIKING_PACKAGE_VERSION=$("$VENV_BIN/python" -c 'import importlib.metadata as m; print(m.version("openviking"))' 2>/dev/null || true)
log_ok "OpenViking 安装完成（package: ${OPENVIKING_PACKAGE_VERSION:-unknown}）"

# ---------- 用户与目录 ----------
CURRENT_PHASE=config
log_info "阶段 4/7: 准备运行用户、目录与配置"
if [[ "$RUN_AS" == "root" ]]; then
  RUN_GROUP=root
  log_warn "运行用户为 root，不符合最小权限基线；建议使用专用 openviking 用户"
else
  require_cmd runuser
  if command -v getent >/dev/null 2>&1; then
    have_group=$(getent group "$RUN_AS" >/dev/null 2>&1 && echo 1 || echo 0)
  else
    have_group=$(grep -q "^$RUN_AS:" /etc/group 2>/dev/null && echo 1 || echo 0)
  fi
  if [[ "$have_group" != "1" ]]; then
    groupadd --system "$RUN_AS" 2>/dev/null || die "创建系统组失败: $RUN_AS"
  fi
  if ! id -u "$RUN_AS" >/dev/null 2>&1; then
    NOLOGIN=/sbin/nologin
    [[ -x /usr/sbin/nologin ]] && NOLOGIN=/usr/sbin/nologin
    [[ -x "$NOLOGIN" ]] || NOLOGIN=/bin/false
    useradd --system -g "$RUN_AS" -d "$DATA_DIR" -s "$NOLOGIN" "$RUN_AS" 2>/dev/null \
      || die "创建系统用户失败: $RUN_AS"
  fi
  RUN_GROUP=$(id -gn "$RUN_AS")
fi

prepare_workspace() {
  local ws=$1
  local ws_uid
  local reason
  case "$ws" in
    "$DATA_DIR"|"$DATA_DIR"/*) ;;
    *)
      if ! reason=$(untrusted_parent_reason "$ws" "$(id -u "$RUN_AS")"); then
        die "workspace 的父目录不可信: $reason"
      fi
      ;;
  esac
  if [[ ! -e "$ws" ]]; then
    mkdir -p "$(dirname "$ws")" || die "创建 workspace 父目录失败: $(dirname "$ws")"
    install -d -m 0750 -o "$RUN_AS" -g "$RUN_GROUP" "$ws" || die "创建 workspace 失败: $ws"
    return 0
  fi
  [[ -d "$ws" ]] || die "workspace 不是目录: $ws"
  ws_uid=$(stat -c '%u' "$ws")
  if [[ "$ws_uid" != "0" && "$ws_uid" != "$(id -u "$RUN_AS")" ]]; then
    die "workspace 属于其他用户（uid=$ws_uid），拒绝改属主: $ws"
  fi
  if [[ "$ws_uid" == "0" ]]; then
    log_warn "workspace 属主为 root，改属主为 $RUN_AS；请确认目录内数据权限: $ws"
    chown "$RUN_AS:$RUN_GROUP" "$ws" || die "无法把 workspace 改属主: $ws"
  fi
  chmod 0750 "$ws" || die "无法设置 workspace 权限: $ws"
}

install -d -m 0750 -o root -g "$RUN_GROUP" "$CONF_DIR"
install -d -m 0750 -o "$RUN_AS" -g "$RUN_GROUP" "$DATA_DIR" /var/log/openviking
install -d -m 0750 -o root -g "$RUN_GROUP" "$DATA_DIR/run"
prepare_workspace "$WORKSPACE"

if [[ -n "$OV_CONF" ]]; then
  if [[ "$OV_CONF" != "$CONF_FILE" ]]; then
    if [[ -f "$CONF_FILE" ]]; then
      cp -a "$CONF_FILE" "$CONF_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    fi
    install -m 0640 -o root -g "$RUN_GROUP" "$OV_CONF" "$CONF_FILE"
  else
    chown "root:$RUN_GROUP" "$CONF_FILE"
    chmod 0640 "$CONF_FILE"
  fi
  CONFIG_SOURCE=file
else
  if [[ -f "$CONF_FILE" ]]; then
    cp -a "$CONF_FILE" "$CONF_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  # 非回环监听时由 gen_ov_conf.py 写入 server.root_api_key；key 不进入日志
  TMP_CONF="$WORK_DIR/ov.conf.generated"
  gen_args=(--out "$TMP_CONF" --workspace "$WORKSPACE" --host "$HOST" --port "$PORT"
    --provider "$PROVIDER" --embedding-model "$EMBEDDING_MODEL" --vlm-model "$VLM_MODEL")
  [[ -n "$API_BASE" ]] && gen_args+=(--api-base "$API_BASE")
  [[ -n "$DIMENSION" && "$DIMENSION" != "0" ]] && gen_args+=(--dimension "$DIMENSION")
  [[ -n "$INPUT_MODE" ]] && gen_args+=(--input-mode "$INPUT_MODE")
  [[ -n "$API_VERSION" ]] && gen_args+=(--api-version "$API_VERSION")
  [[ -n "$API_KEY_FILE" ]] && gen_args+=(--api-key-file "$API_KEY_FILE")
  [[ -n "$ROOT_API_KEY_FILE" ]] && gen_args+=(--root-api-key-file "$ROOT_API_KEY_FILE")
  python3 "$SKILL_ROOT/scripts/gen_ov_conf.py" "${gen_args[@]}" >/dev/null \
    || die "生成 ov.conf 失败（检查模型参数与密钥文件）"
  install -m 0640 -o root -g "$RUN_GROUP" "$TMP_CONF" "$CONF_FILE"
  CONFIG_SOURCE=generated
fi
log_ok "配置就绪: $CONF_FILE（source=$CONFIG_SOURCE）"

# ---------- doctor ----------
CURRENT_PHASE=doctor
log_info "阶段 5/7: openviking-server doctor"
doctor_log="$RESULT_DIR/logs/doctor.log"
doctor_cmd=(timeout 300 "$VENV_BIN/openviking-server" doctor --config "$CONF_FILE")
if [[ "$RUN_AS" == "root" ]]; then
  if ! ( cd "$DATA_DIR" && env HOME="$DATA_DIR" OPENVIKING_CONFIG_FILE="$CONF_FILE" \
    PATH="$VENV_BIN:/usr/local/bin:/usr/bin:/bin" "${doctor_cmd[@]}" ) >"$doctor_log" 2>&1; then
    tail -n 60 "$doctor_log" >&2 || true
    die "doctor 检查失败，完整日志: $doctor_log"
  fi
else
  if ! runuser -u "$RUN_AS" -- bash -c \
    "cd '$DATA_DIR' && exec env HOME='$DATA_DIR' OPENVIKING_CONFIG_FILE='$CONF_FILE' PATH='$VENV_BIN:/usr/local/bin:/usr/bin:/bin' timeout 300 '$VENV_BIN/openviking-server' doctor --config '$CONF_FILE'" \
    >"$doctor_log" 2>&1; then
    tail -n 60 "$doctor_log" >&2 || true
    die "doctor 检查失败，完整日志: $doctor_log"
  fi
fi
log_ok "doctor 检查通过"

# ---------- 服务 ----------
CURRENT_PHASE=service
log_info "阶段 6/7: 安装并启动服务"
SERVER_BIN="$PREFIX/current/venv/bin/openviking-server"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

verify_openviking_pid() {
  local pid=$1
  local recorded_start=$2
  local current_start pid_uid expected_uid pid_exe expected_exe pid_cmd
  [[ "$pid" =~ ^[0-9]+$ ]] || die "pid 文件非法: ${pid:-empty}"
  kill -0 "$pid" 2>/dev/null || die "后台 OpenViking 进程未存活（pid=$pid）"
  current_start=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true)
  if [[ -n "$recorded_start" && -n "$current_start" && "$recorded_start" != "$current_start" ]]; then
    die "pid $pid 启动时间不匹配（PID 可能被复用），停止部署"
  fi
  pid_uid=$(awk '/^Uid:/ {print $2}' "/proc/$pid/status" 2>/dev/null || true)
  expected_uid=$(id -u "$RUN_AS")
  [[ "$pid_uid" == "$expected_uid" ]] \
    || die "pid $pid 属主不是 $RUN_AS（uid=${pid_uid:-unknown}），停止部署"
  pid_exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
  expected_exe=$(readlink -f "$PREFIX/current/venv/bin/python" 2>/dev/null || true)
  [[ -n "$expected_exe" && "$pid_exe" == "$expected_exe" ]] \
    || die "pid $pid 可执行文件不是当前 release 的 python，停止部署"
  pid_cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  [[ "$pid_cmd" == *"$SERVER_BIN"* ]] \
    || die "pid $pid 命令行不是 $SERVER_BIN，停止部署"
}

stop_existing() {
  if [[ "$NO_SYSTEMD" == "1" ]]; then
    local pid_file="$DATA_DIR/run/openviking.pid"
    if [[ -f "$pid_file" ]]; then
      local pid recorded_start
      read -r pid recorded_start <"$pid_file" || true
      [[ "$pid" =~ ^[0-9]+$ ]] || die "pid 文件非法: $pid_file"
      if kill -0 "$pid" 2>/dev/null; then
        verify_openviking_pid "$pid" "$recorded_start"
        log_info "停止既有后台进程: $pid"
        kill "$pid" 2>/dev/null || die "发送 TERM 失败: pid=$pid"
        for _ in $(seq 1 30); do
          kill -0 "$pid" 2>/dev/null || break
          sleep 1
        done
        kill -0 "$pid" 2>/dev/null && die "既有后台进程无法停止（pid=$pid），请人工处理后重跑"
      fi
      rm -f "$pid_file"
    fi
  else
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
      log_info "停止既有服务: $SERVICE_NAME"
      systemctl stop "$SERVICE_NAME"
    fi
  fi
}

if [[ "$NO_SYSTEMD" == "1" ]]; then
  stop_existing
  ln -sfn "$RELEASE_DIR" "$PREFIX/current"
  SERVER_LOG=/var/log/openviking/server.log
  chown "$RUN_AS:$RUN_GROUP" "$SERVER_LOG" 2>/dev/null || true
  if [[ "$RUN_AS" == "root" ]]; then
    ( cd "$DATA_DIR" && exec env HOME="$DATA_DIR" OPENVIKING_CONFIG_FILE="$CONF_FILE" \
      PATH="$VENV_BIN:/usr/local/bin:/usr/bin:/bin" nohup "$SERVER_BIN" >>"$SERVER_LOG" 2>&1 ) &
    pid=$!
  else
    pid=$(runuser -u "$RUN_AS" -- bash -c \
      "cd '$DATA_DIR' && HOME='$DATA_DIR' OPENVIKING_CONFIG_FILE='$CONF_FILE' PATH='$VENV_BIN:/usr/local/bin:/usr/bin:/bin' nohup '$SERVER_BIN' >>'$SERVER_LOG' 2>&1 & echo \$!") \
      || die "启动后台进程失败"
  fi
  [[ "$pid" =~ ^[0-9]+$ ]] || die "无法获取后台进程 PID: ${pid:-empty}"
  sleep 1
  start_time=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true)
  printf '%s %s\n' "$pid" "$start_time" >"$DATA_DIR/run/openviking.pid"
  chown "root:$RUN_GROUP" "$DATA_DIR/run/openviking.pid" 2>/dev/null || true
  chmod 0640 "$DATA_DIR/run/openviking.pid" 2>/dev/null || true
  sleep 1
else
  unit_tmp="$WORK_DIR/openviking.service"
  sed -e "s|__RUN_USER__|$RUN_AS|g" \
      -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
      -e "s|__DATA_DIR__|$DATA_DIR|g" \
      -e "s|__CONF_FILE__|$CONF_FILE|g" \
      -e "s|__SERVER_BIN__|$SERVER_BIN|g" \
      "$SKILL_ROOT/assets/openviking.service.template" >"$unit_tmp" \
    || die "渲染 systemd unit 失败"
  if [[ -f "$SERVICE_FILE" ]]; then
    cp -a "$SERVICE_FILE" "$SERVICE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  install -m 0644 -o root -g root "$unit_tmp" "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || log_warn "systemctl enable 失败，请手工确认开机自启"
  stop_existing
  ln -sfn "$RELEASE_DIR" "$PREFIX/current"
  if ! systemctl start "$SERVICE_NAME"; then
    systemctl status "$SERVICE_NAME" --no-pager -l >&2 || true
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
    die "服务启动失败: $SERVICE_NAME"
  fi
fi
log_ok "服务已启动（release: $release_id）"

# ---------- 验收 ----------
CURRENT_PHASE=verify
log_info "阶段 7/7: 健康验收"
case "$HOST" in
  0.0.0.0) PROBE_HOST=127.0.0.1 ;;
  ::|::1) PROBE_HOST='[::1]' ;;
  *:*) PROBE_HOST="[$HOST]" ;;
  *) PROBE_HOST="$HOST" ;;
esac
BASE_URL="http://$PROBE_HOST:$PORT"
wait_http() {
  local url=$1
  local out=$2
  local deadline=$((SECONDS + READY_TIMEOUT))
  while ((SECONDS < deadline)); do
    if curl -fsS --max-time 5 "$url" -o "$out" 2>/dev/null; then
      return 0
    fi
    sleep 3
  done
  return 1
}

health_body="$RESULT_DIR/logs/health.json"
ready_body="$RESULT_DIR/logs/ready.json"
if ! wait_http "$BASE_URL/health" "$health_body"; then
  [[ "$NO_SYSTEMD" == "1" ]] && tail -n 80 /var/log/openviking/server.log >&2 || true
  [[ "$NO_SYSTEMD" == "1" ]] || journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
  die "/health 在 ${READY_TIMEOUT}s 内未就绪: $BASE_URL/health"
fi
if ! python3 - "$health_body" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
ok = doc.get("status") == "ok" and doc.get("healthy") is True
raise SystemExit(0 if ok else 1)
PY
then
  die "/health 返回不符合预期: $health_body"
fi
log_ok "/health 通过"

if ! wait_http "$BASE_URL/ready" "$ready_body"; then
  [[ "$NO_SYSTEMD" == "1" ]] && tail -n 80 /var/log/openviking/server.log >&2 || true
  [[ "$NO_SYSTEMD" == "1" ]] || journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
  die "/ready 在 ${READY_TIMEOUT}s 内未就绪: $BASE_URL/ready（检查模型配置与网络）"
fi
if ! python3 - "$ready_body" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
raise SystemExit(0 if doc.get("status") == "ready" else 1)
PY
then
  die "/ready 返回不符合预期: $ready_body"
fi
log_ok "/ready 通过"

if [[ "$NO_SYSTEMD" == "1" ]]; then
  pid_file="$DATA_DIR/run/openviking.pid"
  [[ -f "$pid_file" ]] || die "缺少 pid 文件: $pid_file"
  read -r pid recorded_start <"$pid_file" || true
  verify_openviking_pid "$pid" "$recorded_start"
else
  systemctl is-active --quiet "$SERVICE_NAME" || die "服务不是 active: $SERVICE_NAME"
fi
log_ok "进程/服务状态正常"

API_KEY_SET=0
if python3 - "$CONF_FILE" <<'PY' >/dev/null 2>&1
import json, sys

def has_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "api_key" and isinstance(item, str) and item.strip():
                return True
            if has_key(item):
                return True
    elif isinstance(value, list):
        return any(has_key(item) for item in value)
    return False

with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
raise SystemExit(0 if has_key(doc) else 1)
PY
then
  API_KEY_SET=1
fi

CURRENT_PHASE=finalize
if [[ -n "$WHEEL" && "$OPENVIKING_VERSION" == "main" ]]; then
  OPENVIKING_VERSION=wheel
fi
if [[ -n "$SOURCE_DIR" && "$OPENVIKING_VERSION" == "main" ]]; then
  OPENVIKING_VERSION=source-dir
fi
{
  printf 'DEPLOY_STATUS=ok\n'
  printf 'DEPLOY_RUN_ID=%s\n' "$run_id"
  printf 'OPENVIKING_VERSION=%s\n' "$OPENVIKING_VERSION"
  printf 'OPENVIKING_BRANCH=%s\n' "$OPENVIKING_BRANCH"
  printf 'OPENVIKING_COMMIT=%s\n' "$OPENVIKING_COMMIT"
  printf 'OPENVIKING_SIGNATURE_VERIFIED=%s\n' "$SIGNATURE_VERIFIED"
  printf 'OPENVIKING_PACKAGE_VERSION=%s\n' "${OPENVIKING_PACKAGE_VERSION:-unknown}"
  printf 'OPENVIKING_SOURCE=%s\n' "${SOURCE_DIR:-${WHEEL:-$GIT_URL}}"
  printf 'OPENVIKING_WORK_DIR=%s\n' "$WORK_DIR"
  printf 'OPENVIKING_RELEASE_DIR=%s\n' "$RELEASE_DIR"
  printf 'OPENVIKING_CURRENT_LINK=%s\n' "$PREFIX/current"
  printf 'OPENVIKING_VENV=%s\n' "$PREFIX/current/venv"
  printf 'OPENVIKING_SERVER_BIN=%s\n' "$SERVER_BIN"
  printf 'OPENVIKING_CONF=%s\n' "$CONF_FILE"
  printf 'OPENVIKING_CONFIG_SOURCE=%s\n' "$CONFIG_SOURCE"
  printf 'OPENVIKING_WORKSPACE=%s\n' "$WORKSPACE"
  printf 'OPENVIKING_HOST=%s\n' "$HOST"
  printf 'OPENVIKING_PORT=%s\n' "$PORT"
  printf 'OPENVIKING_SERVICE=%s\n' "$([[ "$NO_SYSTEMD" == "1" ]] && echo "background" || echo "$SERVICE_NAME.service")"
  printf 'OPENVIKING_RUN_USER=%s\n' "$RUN_AS"
  printf 'OPENVIKING_PROVIDER=%s\n' "${PROVIDER:-from-conf}"
  printf 'OPENVIKING_EMBEDDING_MODEL=%s\n' "${EMBEDDING_MODEL:-from-conf}"
  printf 'OPENVIKING_VLM_MODEL=%s\n' "${VLM_MODEL:-from-conf}"
  printf 'OPENVIKING_WHEEL=%s\n' "$WHEEL_PATH"
  printf 'OPENVIKING_HEALTH=ok\n'
  printf 'OPENVIKING_READY=ok\n'
  printf 'OPENVIKING_API_KEY_SET=%s\n' "$API_KEY_SET"
  printf 'DEPLOY_TIME=%s\n' "$(date -Is)"
} >"$DEPLOY_ENV"
chmod 0640 "$DEPLOY_ENV" 2>/dev/null || true
chmod -R u=rwX,g=rX,o= "$RESULT_DIR/logs" "$RESULT_DIR/env" 2>/dev/null || true

log_ok "OpenViking 环境部署完成"
log_info "版本: $OPENVIKING_VERSION（commit: $OPENVIKING_COMMIT, package: ${OPENVIKING_PACKAGE_VERSION:-unknown}）"
log_info "release: $RELEASE_DIR"
log_info "配置: $CONF_FILE（source=$CONFIG_SOURCE）"
log_info "服务: $([[ "$NO_SYSTEMD" == "1" ]] && echo "background" || echo "$SERVICE_NAME.service")；监听: $BASE_URL"
log_info "产物: $DEPLOY_ENV"
exit 0
