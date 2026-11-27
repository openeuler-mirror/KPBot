#!/usr/bin/env bash
#===============================================================
# preflight_check.sh — OpenViking 部署前置检查（只读，无副作用）
#
# 用法:
#   bash scripts/preflight_check.sh [options]
#
# 检查项:
#   1. root 权限
#   2. 操作系统与 CPU 架构
#   3. CPU、内存、构建目录磁盘空间
#   4. Python / git / C++ 编译器 / CMake / Rust / maturin 工具链
#   5. 源码目录或 Git 仓库可达性
#   6. 构建根目录父目录属主与 sticky 位
#   7. 端口占用与既有 OpenViking 安装
#   8. 配置文件与模型参数完整性
#   9. 密钥文件权限
#  10. systemd / SELinux / Node.js 提示
#
# 输出逐项 [PASS]/[WARN]/[FAIL] + 汇总；存在 FAIL 时退出码非零。
# 本脚本不创建、不修改任何文件，不连接模型 API。
#===============================================================
set -u

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

OPENVIKING_VERSION=main
GIT_URL=https://github.com/volcengine/OpenViking.git
SOURCE_DIR=""
WHEEL=""
PREFIX=/opt/openviking
BUILD_ROOT=/var/cache/openviking-deploy
CONF_DIR=/etc/openviking
DATA_DIR=/var/lib/openviking
WORKSPACE=""
HOST=127.0.0.1
PORT=1933
RUN_AS=openviking
OV_CONF=""
PROVIDER=""
API_BASE=""
EMBEDDING_MODEL=""
VLM_MODEL=""
API_KEY_FILE=""
ROOT_API_KEY_FILE=""
WITH_STUDIO=0

usage() {
  cat <<'EOF'
用法: preflight_check.sh [options]

只读检查 OpenViking 部署前置条件，不修改任何配置，不连接模型 API。

  --version REF           版本/分支/commit（默认 main；master 为历史别名）
  --git-url URL           源码仓库（仅支持 http(s)://；本地源码用 --source-dir）
  --source-dir DIR        离线/本地源码目录
  --wheel FILE            预编译 wheel（跳过源码构建）
  --prefix DIR            安装前缀（默认 /opt/openviking）
  --build-root DIR        构建缓存根目录（默认 /var/cache/openviking-deploy）
  --conf-dir DIR          配置目录（默认 /etc/openviking）
  --data-dir DIR          数据根目录（默认 /var/lib/openviking）
  --workspace DIR         storage.workspace（默认 <data-dir>/data）
  --host ADDR             监听地址（默认 127.0.0.1）
  --port PORT             监听端口（默认 1933）
  --run-as USER           运行用户（默认 openviking）
  --ov-conf FILE          已确认的 ov.conf
  --provider NAME         生成配置时的 provider
  --api-base URL          Embedding/VLM API Base
  --embedding-model NAME  Embedding 模型名
  --vlm-model NAME        VLM 模型名
  --api-key-file FILE     API key 文件（检查权限，不读取内容）
  --root-api-key-file FILE root API key 文件（检查权限，不读取内容）
  --with-studio           计划构建 Web Studio（检查 Node.js）
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) OPENVIKING_VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --git-url) GIT_URL=${2:?--git-url 缺少参数}; shift 2 ;;
    --source-dir) SOURCE_DIR=${2:?--source-dir 缺少参数}; shift 2 ;;
    --wheel) WHEEL=${2:?--wheel 缺少参数}; shift 2 ;;
    --prefix) PREFIX=${2:?--prefix 缺少参数}; shift 2 ;;
    --build-root) BUILD_ROOT=${2:?--build-root 缺少参数}; shift 2 ;;
    --conf-dir) CONF_DIR=${2:?--conf-dir 缺少参数}; shift 2 ;;
    --data-dir) DATA_DIR=${2:?--data-dir 缺少参数}; shift 2 ;;
    --workspace) WORKSPACE=${2:?--workspace 缺少参数}; shift 2 ;;
    --host) HOST=${2:?--host 缺少参数}; shift 2 ;;
    --port) PORT=${2:?--port 缺少参数}; shift 2 ;;
    --run-as) RUN_AS=${2:?--run-as 缺少参数}; shift 2 ;;
    --ov-conf) OV_CONF=${2:?--ov-conf 缺少参数}; shift 2 ;;
    --provider) PROVIDER=${2:?--provider 缺少参数}; shift 2 ;;
    --api-base) API_BASE=${2:?--api-base 缺少参数}; shift 2 ;;
    --embedding-model) EMBEDDING_MODEL=${2:?--embedding-model 缺少参数}; shift 2 ;;
    --vlm-model) VLM_MODEL=${2:?--vlm-model 缺少参数}; shift 2 ;;
    --api-key-file) API_KEY_FILE=${2:?--api-key-file 缺少参数}; shift 2 ;;
    --root-api-key-file) ROOT_API_KEY_FILE=${2:?--root-api-key-file 缺少参数}; shift 2 ;;
    --with-studio) WITH_STUDIO=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

if [[ "$OPENVIKING_VERSION" == "master" ]]; then
  OPENVIKING_VERSION=main
fi
if [[ "$GIT_URL" != http://* && "$GIT_URL" != https://* ]]; then
  die "git-url 仅支持 http(s):// URL；本地源码请使用 --source-dir"
fi
url_has_userinfo "$GIT_URL" \
  && die "git-url 不得携带用户名/密码（userinfo）；请改用 SSH remote 或凭据助手"
[[ "$GIT_URL" == *"?"* || "$GIT_URL" == *"#"* ]] \
  && die "git-url 不得包含查询串或片段（避免令牌进入日志）；请改用 SSH remote 或凭据助手"
if [[ -n "$SOURCE_DIR" && "$OPENVIKING_VERSION" != "main" ]]; then
  die "--source-dir 使用目录自带版本，不能再指定 --version $OPENVIKING_VERSION（默认 main 即可）"
fi
if [[ -n "$SOURCE_DIR" && -n "$WHEEL" ]]; then
  die "--source-dir 与 --wheel 不能同时使用"
fi
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || die "端口非法: $PORT"
[[ "$RUN_AS" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "运行用户非法: $RUN_AS"

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
  fail "未以 root 运行（系统依赖、/opt、/etc、systemd 都需要 root）"
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
  *) warn "未验证的 CPU 架构: $ARCH（源码构建需自行验证 Rust/CMake 工具链）" ;;
esac
info "内核: $(uname -r)"

# ---------- 3. 资源 ----------
sect "3. 资源"
NPROC=$(nproc 2>/dev/null || echo 1)
if [[ "$NPROC" -ge 4 ]]; then
  pass "可用 CPU 核数: $NPROC"
else
  warn "CPU 核数偏少: $NPROC（源码构建建议 >= 4 核）"
fi
MEM_MB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "${MEM_MB:-0}" -ge 4096 ]]; then
  pass "内存: ${MEM_MB} MiB"
else
  warn "内存偏小: ${MEM_MB} MiB（源码构建与服务运行建议 >= 4 GiB）"
fi
disk_target=$BUILD_ROOT
while [[ ! -d "$disk_target" && "$disk_target" != "/" ]]; do
  disk_target=$(dirname "$disk_target")
done
DISK_AVAIL_MB=$(df -Pk "$disk_target" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1024}')
if [[ "${DISK_AVAIL_MB:-0}" -ge 5120 ]]; then
  pass "构建目录可用空间: ${DISK_AVAIL_MB} MiB（$disk_target）"
else
  warn "构建目录可用空间偏小: ${DISK_AVAIL_MB:-unknown} MiB（建议 >= 5 GiB）"
fi

# ---------- 4. 工具链 ----------
sect "4. 工具链"
PYTHON_BIN=$(command -v python3 2>/dev/null || true)
if [[ -n "$PYTHON_BIN" ]]; then
  PY_VER=$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo 0.0.0)
  if version_ge "$PY_VER" "3.10.0"; then
    pass "Python: $PY_VER（$PYTHON_BIN）"
  else
    fail "Python 版本过低: $PY_VER（要求 >= 3.10）"
  fi
  if "$PYTHON_BIN" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    pass "python3 venv/ensurepip 可用"
  else
    warn "python3 venv 或 ensurepip 不可用（install_deps.sh 会尝试安装对应包）"
  fi
else
  fail "未找到 python3（要求 >= 3.10）"
fi

for cmd in git make tar find; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "命令存在: $cmd"
  else
    warn "缺少 $cmd（install_deps.sh 会安装）"
  fi
done

if [[ -n "$WHEEL" ]]; then
  info "wheel 模式：跳过 C++/CMake/Rust 工具链检查"
else
if command -v g++ >/dev/null 2>&1; then
  GCC_VER=$(g++ -dumpversion 2>/dev/null || echo 0)
  if version_ge "$GCC_VER" "9"; then
    pass "g++: $GCC_VER"
  else
    fail "g++ 版本过低: $GCC_VER（要求 >= 9）"
  fi
elif command -v clang++ >/dev/null 2>&1; then
  CLANG_VER=$(clang++ --version | head -n1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || echo 0)
  if version_ge "$CLANG_VER" "11"; then
    pass "clang++: $CLANG_VER"
  else
    fail "clang++ 版本过低: $CLANG_VER（要求 >= 11）"
  fi
else
  fail "未找到 C++ 编译器（要求 g++ >= 9 或 clang++ >= 11）"
fi

if command -v cmake >/dev/null 2>&1; then
  CMAKE_VER=$(cmake --version | head -n1 | awk '{print $3}')
  if version_ge "$CMAKE_VER" "3.15"; then
    pass "CMake: $CMAKE_VER"
  else
    fail "CMake 版本过低: $CMAKE_VER（要求 >= 3.15）"
  fi
else
  fail "未找到 cmake（要求 >= 3.15）"
fi

if command -v cargo >/dev/null 2>&1; then
  RUST_VER=$(cargo --version | awk '{print $2}')
  if version_ge "$RUST_VER" "1.91.1"; then
    pass "Rust/cargo: $RUST_VER"
  else
    fail "Rust 版本过低: $RUST_VER（要求 >= 1.91.1；可用 --install-rust 安装）"
  fi
else
  fail "未找到 cargo（要求 Rust >= 1.91.1；安装时可用 --install-rust）"
fi

if command -v maturin >/dev/null 2>&1; then
  pass "maturin: $(maturin --version 2>/dev/null)"
else
  info "系统未安装 maturin（install_openviking.sh 会在 venv 内安装）"
fi
fi

# ---------- 5. 源码 / 网络 ----------
sect "5. 源码与下载源"
if [[ -n "$WHEEL" ]]; then
  if ! valid_abs_path "$WHEEL"; then
    fail "wheel 必须是绝对路径且不含 .、..、// 段: $WHEEL"
  elif [[ -f "$WHEEL" ]]; then
    owner=$(stat -c '%u' "$WHEEL" 2>/dev/null || echo unknown)
    mode=$(stat -c '%a' "$WHEEL" 2>/dev/null || echo unknown)
    if [[ "$owner" == "0" ]] && [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0022) == 0 )); then
      pass "wheel 属于 root 且组/其他不可写（$mode）: $WHEEL"
    else
      fail "wheel 必须属于 root 且组/其他不可写（owner=$owner mode=$mode）: $WHEEL"
    fi
    if ! reason=$(untrusted_parent_reason "$WHEEL"); then
      fail "wheel 的父目录不可信: $reason"
    else
      pass "wheel 的父目录属 root 且不可被普通用户控制"
    fi
  else
    fail "wheel 不存在: $WHEEL"
  fi
elif [[ -n "$SOURCE_DIR" ]]; then
  if ! valid_abs_path "$SOURCE_DIR"; then
    fail "源码目录必须是绝对路径且不含 .、..、// 段: $SOURCE_DIR"
  elif [[ ! -d "$SOURCE_DIR" ]]; then
    fail "源码目录不存在: $SOURCE_DIR"
  else
    owner=$(stat -c '%u' "$SOURCE_DIR")
    mode=$(stat -c '%a' "$SOURCE_DIR")
    [[ "$owner" == "0" ]] && pass "源码目录属于 root" || fail "源码目录必须属于 root（当前 uid=$owner）: $SOURCE_DIR"
    (( (8#$mode & 0022) == 0 )) && pass "源码目录组/其他不可写（$mode）" || fail "源码目录组/其他可写（$mode）: $SOURCE_DIR"
    if ! reason=$(untrusted_parent_reason "$SOURCE_DIR"); then
      fail "源码目录的父目录不可信: $reason"
    else
      pass "源码目录的父目录属 root 且不可被普通用户控制"
    fi
    if [[ -f "$SOURCE_DIR/pyproject.toml" && -f "$SOURCE_DIR/setup.py" ]]; then
      pass "源码目录包含 pyproject.toml 与 setup.py"
    else
      fail "源码目录缺少 pyproject.toml 或 setup.py"
    fi
  fi
else
  if command -v git >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    if timeout 20 git ls-remote --exit-code "$GIT_URL" HEAD >/dev/null 2>&1; then
      pass "源码仓库可达: $GIT_URL"
    else
      warn "无法访问 $GIT_URL；离线部署请用 --source-dir DIR 或 --wheel FILE"
    fi
  else
    warn "git/timeout 不可用，跳过源码仓库可达性检查"
  fi
fi

# ---------- 6. 构建根目录 ----------
sect "6. 构建根目录"
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

# ---------- 7. 端口与既有安装 ----------
sect "7. 端口与既有安装"
if command -v ss >/dev/null 2>&1; then
  if ss -lntp 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${PORT}$"; then
    warn "端口 $PORT 已被监听（可能是既有 OpenViking 或其他服务）"
  else
    pass "端口 $PORT 空闲"
  fi
else
  warn "缺少 ss，跳过端口占用检查"
fi
if [[ -x "$PREFIX/current/venv/bin/openviking-server" ]]; then
  info "检测到既有安装: $PREFIX/current（升级时将创建新 release 并切换 current）"
else
  info "未检测到 $PREFIX/current/venv/bin/openviking-server"
fi
if [[ -f "$CONF_DIR/ov.conf" ]]; then
  info "检测到既有配置: $CONF_DIR/ov.conf（部署时会备份并覆盖或接管）"
fi

# ---------- 8. 配置与模型参数 ----------
sect "8. 配置与模型参数"
if [[ -n "$OV_CONF" ]]; then
  if [[ -f "$OV_CONF" ]]; then
    conf_out=$("${PYTHON_BIN:-python3}" "$SKILL_ROOT/scripts/gen_ov_conf.py" --check "$OV_CONF" 2>&1)
    conf_rc=$?
    if [[ "$conf_rc" -eq 0 ]]; then
      pass "ov.conf 通过 gen_ov_conf.py --check（字段与安装器一致）"
    else
      fail "ov.conf 校验失败: $(printf '%s' "$conf_out" | tr '\n' '; ')"
    fi
    if [[ "$conf_rc" -eq 0 ]]; then
      conf_info=$("${PYTHON_BIN:-python3}" - "$OV_CONF" <<'PY'
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
      info "ov.conf 监听: $conf_host:$conf_port（root_api_key=${conf_has_root}）"
      info "ov.conf storage.workspace: $conf_workspace"
      if [[ "$conf_host" != "127.0.0.1" && "$conf_host" != "localhost" && "$conf_host" != "::1" && "$conf_has_root" != "1" ]]; then
        fail "ov.conf 监听 $conf_host 不是回环地址，但未配置 server.root_api_key"
      fi
    fi
    mode=$(stat -c '%a' "$OV_CONF" 2>/dev/null || echo unknown)
    if [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0077) == 0 )); then
      pass "ov.conf 权限符合要求（$mode）"
    else
      warn "ov.conf 权限过宽（$mode），建议先执行: chmod 600 $OV_CONF"
    fi
  else
    fail "ov.conf 不存在: $OV_CONF"
  fi
else
  if [[ -n "$PROVIDER" && -n "$EMBEDDING_MODEL" && -n "$VLM_MODEL" ]]; then
    pass "已提供生成配置所需参数（provider=$PROVIDER）"
  else
    warn "未提供 --ov-conf，也未给全 --provider/--embedding-model/--vlm-model；部署阶段会要求确认"
  fi
  case "$PROVIDER" in
    ""|volcengine|openai|azure|ollama) ;;
    *) warn "provider $PROVIDER 不在共享参数支持列表内；混合/特殊 provider 请用 --ov-conf" ;;
  esac
fi
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
  if [[ -n "$ROOT_API_KEY_FILE" ]]; then
    info "非回环监听 $HOST，将使用 root API key 鉴权"
  else
    warn "非回环监听 $HOST 但未提供 --root-api-key-file；部署阶段会失败（必须开启鉴权）"
  fi
fi
if [[ -n "$WORKSPACE" ]]; then
  info "workspace: $WORKSPACE"
else
  info "workspace 默认: $DATA_DIR/data"
fi

# ---------- 9. 密钥文件 ----------
sect "9. 密钥文件"
if [[ -n "$API_KEY_FILE" ]]; then
  if [[ ! -f "$API_KEY_FILE" ]]; then
    fail "API key 文件不存在: $API_KEY_FILE"
  else
    mode=$(stat -c '%a' "$API_KEY_FILE")
    if (( (8#$mode & 0077) == 0 )); then
      pass "API key 文件权限符合要求（$mode），内容未读取"
    else
      fail "API key 文件权限过宽（$mode），请执行: chmod 600 $API_KEY_FILE"
    fi
  fi
else
  if [[ -n "$OV_CONF" ]]; then
    info "使用已有 ov.conf 内的凭据"
  else
    warn "未提供 --api-key-file；ollama 可无 key，其他 provider 部署阶段会失败"
  fi
fi
if [[ -n "$ROOT_API_KEY_FILE" ]]; then
  if [[ ! -f "$ROOT_API_KEY_FILE" ]]; then
    fail "root API key 文件不存在: $ROOT_API_KEY_FILE"
  else
    mode=$(stat -c '%a' "$ROOT_API_KEY_FILE")
    if (( (8#$mode & 0077) == 0 )); then
      pass "root API key 文件权限符合要求（$mode），内容未读取"
    else
      fail "root API key 文件权限过宽（$mode），请执行: chmod 600 $ROOT_API_KEY_FILE"
    fi
  fi
fi

# ---------- 10. 环境提示 ----------
sect "10. 环境提示"
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  pass "systemd 可用，部署阶段可托管服务"
else
  warn "未检测到 systemd；请加 --no-systemd 以后台进程方式启动"
fi
if command -v getenforce >/dev/null 2>&1; then
  info "SELinux: $(getenforce 2>/dev/null || echo unknown)"
fi
if [[ "$WITH_STUDIO" == "1" ]]; then
  if command -v npm >/dev/null 2>&1; then
    pass "npm 存在: $(npm --version 2>/dev/null)"
  else
    warn "缺少 npm（install_deps.sh --with-studio 会安装 Node.js）"
  fi
else
  info "未请求 --with-studio；部署时默认跳过 Web Studio（/studio 不可用）"
fi
info "默认版本: main（master 为历史别名；部署后记录实际 commit）"

# ---------- 汇总 ----------
printf '\n===== 汇总 =====\n'
printf 'PASS=%d  WARN=%d  FAIL=%d\n' "$PASS_CNT" "$WARN_CNT" "$FAIL_CNT"
if ((FAIL_CNT > 0)); then
  printf '存在 FAIL 项，修复后重跑本脚本再开始部署。\n'
  exit 1
fi
printf '前置检查通过（WARN 项确认影响后可继续）。\n'
exit 0
