#!/usr/bin/env bash
#===============================================================
# install_deps.sh — OpenViking 源码构建依赖安装
#
# 默认安装:
#   git curl CA 证书 tar findutils coreutils gcc/g++ make cmake
#   python3 / pip / venv / devel 及基础运行库（openssl、cairo、libffi）
#
# 可选:
#   --install-rust  用官方 rustup-init 安装 Rust 1.91.1（下载 .sha256 校验）
#   --with-studio   安装 Node.js/npm 以构建 Web Studio
#
# 安全:
#   - 不把下载内容直接管道给 shell；rustup-init 先下载再校验 sha256，最后执行
#   - 校验失败立即退出，不继续安装
#   - --dry-run 只打印计划
#===============================================================
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

INSTALL_RUST=0
SKIP_RUST=0
WITH_STUDIO=0
DRY_RUN=0
RUST_MIN=1.91.1
RUSTUP_HOME_DEFAULT=/usr/local/rustup
CARGO_HOME_DEFAULT=/usr/local/cargo

usage() {
  cat <<'EOF'
用法: install_deps.sh [options]

  --install-rust   安装 Rust 工具链（默认 1.91.1，sha256 校验）
  --skip-rust      跳过 Rust 检查（仅安装预编译 wheel 时使用）
  --with-studio    安装 Node.js/npm（构建 Web Studio）
  --dry-run        只打印计划
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-rust) INSTALL_RUST=1; shift ;;
    --skip-rust) SKIP_RUST=1; shift ;;
    --with-studio) WITH_STUDIO=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

PM=$(detect_pkg_manager)
[[ "$PM" != "unknown" ]] || die "未识别的包管理器（支持 dnf/yum/apt/zypper）"

BASE_RPMS=(git curl ca-certificates tar findutils coreutils gcc gcc-c++ make cmake python3 python3-pip python3-devel openssl-devel cairo libffi-devel)
BASE_DEBS=(git curl ca-certificates tar findutils coreutils gcc g++ make cmake python3 python3-pip python3-venv python3-dev libssl-dev libcairo2 libffi-dev)
BASE_ZYPPER=(git curl ca-certificates tar findutils coreutils gcc gcc-c++ make cmake python3 python3-pip python3-devel libopenssl-devel cairo libffi-devel)

pm_install() {
  case "$PM" in
    dnf) dnf install -y "$@" ;;
    yum) yum install -y "$@" ;;
    apt)
      DEBIAN_FRONTEND=noninteractive apt-get update -y
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
      ;;
    zypper) zypper --non-interactive install --no-recommends "$@" ;;
    *) die "不支持的包管理器: $PM" ;;
  esac
}

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] 包管理器: $PM"
  case "$PM" in
    dnf|yum|zypper) echo "[dry-run] 基础包: ${BASE_RPMS[*]}" ;;
    apt) echo "[dry-run] 基础包: ${BASE_DEBS[*]}" ;;
  esac
  [[ "$INSTALL_RUST" == "1" ]] && echo "[dry-run] 安装 Rust $RUST_MIN（rustup-init + sha256 校验）"
  [[ "$SKIP_RUST" == "1" ]] && echo "[dry-run] 跳过 Rust 检查（wheel 模式）"
  [[ "$WITH_STUDIO" == "1" ]] && echo "[dry-run] 安装 Node.js/npm"
  exit 0
fi

require_root

log_info "阶段 1/3: 安装基础编译依赖（$PM）"
case "$PM" in
  dnf|yum|zypper) pm_install "${BASE_RPMS[@]}" ;;
  apt) pm_install "${BASE_DEBS[@]}" ;;
esac
log_ok "基础编译依赖安装完成"

# ---------- Rust ----------
if [[ "$SKIP_RUST" == "1" ]]; then
  log_info "阶段 2/3: wheel 模式跳过 Rust 检查"
else
log_info "阶段 2/3: 检查 Rust 工具链"
RUST_OK=0
if command -v cargo >/dev/null 2>&1; then
  RUST_VER=$(cargo --version | awk '{print $2}')
  if version_ge "$RUST_VER" "$RUST_MIN"; then
    log_ok "已满足 Rust 版本要求: $RUST_VER"
    RUST_OK=1
  else
    log_warn "Rust 版本过低: $RUST_VER（要求 >= $RUST_MIN）"
  fi
else
  log_warn "未找到 cargo"
fi

if [[ "$RUST_OK" == "0" ]]; then
  if [[ "$INSTALL_RUST" != "1" ]]; then
    die "Rust 不满足要求；请手工安装 Rust >= $RUST_MIN，或重跑并显式加 --install-rust（脚本会下载 rustup-init 并校验 sha256）"
  fi
  case "$(uname -m)" in
    x86_64) RUSTUP_TARGET=x86_64-unknown-linux-gnu ;;
    aarch64) RUSTUP_TARGET=aarch64-unknown-linux-gnu ;;
    *) die "rustup 不支持的 CPU 架构: $(uname -m)" ;;
  esac
  require_cmd curl
  require_cmd sha256sum
  tmp_dir=$(mktemp -d)
  cleanup_rust() { rm -f "$tmp_dir/rustup-init" "$tmp_dir/rustup-init.sha256"; rmdir "$tmp_dir" 2>/dev/null || true; }
  trap cleanup_rust EXIT
  rustup_url="https://static.rust-lang.org/rustup/dist/${RUSTUP_TARGET}/rustup-init"
  log_info "下载 rustup-init（$RUSTUP_TARGET）"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$tmp_dir/rustup-init" "$rustup_url" \
    || die "rustup-init 下载失败: $rustup_url"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$tmp_dir/rustup-init.sha256" "${rustup_url}.sha256" \
    || die "rustup-init.sha256 下载失败"
  expected=$(awk '{print $1}' "$tmp_dir/rustup-init.sha256")
  actual=$(sha256sum "$tmp_dir/rustup-init" | awk '{print $1}')
  [[ -n "$expected" && "$expected" == "$actual" ]] \
    || die "rustup-init sha256 校验失败（expected=${expected:-empty} actual=$actual），拒绝执行"
  log_ok "rustup-init sha256 校验通过"
  chmod 0755 "$tmp_dir/rustup-init"
  install -d -m 0755 "$RUSTUP_HOME_DEFAULT" "$CARGO_HOME_DEFAULT"
  RUSTUP_HOME="$RUSTUP_HOME_DEFAULT" CARGO_HOME="$CARGO_HOME_DEFAULT" \
    "$tmp_dir/rustup-init" -y --profile minimal --default-toolchain "$RUST_MIN" --no-modify-path \
    || die "rustup-init 执行失败"
  export RUSTUP_HOME="$RUSTUP_HOME_DEFAULT"
  export CARGO_HOME="$CARGO_HOME_DEFAULT"
  export PATH="$CARGO_HOME/bin:$PATH"
  cat >/etc/profile.d/rust.sh <<'EOF'
export RUSTUP_HOME=/usr/local/rustup
export CARGO_HOME=/usr/local/cargo
export PATH=/usr/local/cargo/bin:$PATH
EOF
  chmod 0644 /etc/profile.d/rust.sh
  log_ok "Rust 安装完成: $(cargo --version)"
fi
fi

# ---------- Node.js ----------
log_info "阶段 3/3: Web Studio 依赖"
if [[ "$WITH_STUDIO" == "1" ]]; then
  if command -v npm >/dev/null 2>&1; then
    log_ok "npm 已存在: $(npm --version)"
  else
    log_info "安装 Node.js/npm"
    if ! pm_install nodejs npm; then
      log_warn "nodejs/npm 组合安装失败，尝试仅安装 nodejs"
      pm_install nodejs || log_warn "Node.js 安装失败；安装阶段会跳过 Web Studio"
    fi
    command -v npm >/dev/null 2>&1 || log_warn "npm 安装后仍不可用；安装阶段会跳过 Web Studio"
  fi
else
  log_info "未请求 --with-studio，跳过 Node.js"
fi

log_ok "依赖安装完成"
exit 0
