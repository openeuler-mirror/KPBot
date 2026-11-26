#!/usr/bin/env bash
# preflight_check.sh — PostgreSQL 编译/部署只读预检
#
# 检查：工具链依赖、源码树与版本锚点、安装目录、数据目录状态、端口占用、
# 运行实例状态。不做任何修改；输出 PASS/WARN/FAIL + 退出码（0=可继续，
# 1=存在 FAIL，2=用法错误）。
#
# 用法:
#   PG_SRC=/opt/postgresql PG_INSTALL=/opt/pgsql bash preflight_check.sh
#   # 可选: PGDATA/PG_PORT 指定时一并检查部署项；PG_MODE=build|deploy|both（默认 both）

set -u

PG_SRC="${PG_SRC:-}"
PG_INSTALL="${PG_INSTALL:-}"
PGDATA="${PGDATA:-}"
PG_PORT="${PG_PORT:-5432}"
PG_MODE="${PG_MODE:-both}"

PASS_CNT=0; WARN_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "[PASS] $*"; }
warn() { WARN_CNT=$((WARN_CNT+1)); echo "[WARN] $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "[FAIL] $*"; }

usage() {
  cat <<'EOF'
用法: preflight_check.sh [--help]

只读预检（不修改任何状态）。环境变量：
  PG_SRC      源码树根（编译模式检查）
  PG_INSTALL  安装前缀（编译/部署模式检查）
  PGDATA      数据目录（部署模式检查；空则跳过部署项）
  PG_PORT     监听端口，默认 5432
  PG_MODE     build|deploy|both（默认 both）
退出码: 0 = 无 FAIL；1 = 存在 FAIL；2 = 用法错误
EOF
}

[ "${1:-}" = "--help" ] && { usage; exit 0; } || true
case "$PG_MODE" in build|deploy|both) ;; *) echo "ERROR: PG_MODE 仅支持 build/deploy/both" >&2; usage >&2; exit 2 ;; esac
case "$PG_PORT" in ''|*[!0-9]*) echo "ERROR: PG_PORT 须为数字: ${PG_PORT}" >&2; exit 2 ;; esac
# 端口范围在探测前校验：65536 等能过纯数字校验，随后 ss 探测错误只记作无监听，
# 预检会误退出 0，而实例实际不能绑定该端口
if [ "$PG_PORT" -lt 1 ] || [ "$PG_PORT" -gt 65535 ]; then
  echo "ERROR: PG_PORT 超出合法范围 1-65535: ${PG_PORT}" >&2; exit 2
fi

# ---- 编译侧 ----
if [ "$PG_MODE" = "build" ] || [ "$PG_MODE" = "both" ]; then
  for c in gcc make bison flex perl tar; do
    command -v "$c" >/dev/null 2>&1 && pass "工具链: $c" || fail "缺少工具链: $c"
  done
  # §2 默认 configure（--with-openssl --with-icu --with-llvm）的三个依赖包
  # pkg-config 模块名与 dep 名不同：ICU 注册的是 icu-uc/icu-i18n（无 icu.pc，
  # PG configure 自己也按 icu-uc icu-i18n 探测）；llvm 无 pc 模块、走 -config 兜底
  for dep in openssl icu llvm; do
    case "$dep" in
      openssl) pkg="openssl-devel"; pc="openssl" ;;
      icu)     pkg="libicu-devel";  pc="icu-uc icu-i18n" ;;
      llvm)    pkg="llvm-devel";    pc="" ;;
    esac
    if [ -n "$pc" ] && command -v pkg-config >/dev/null 2>&1 \
       && pkg-config --exists $pc 2>/dev/null; then
      pass "依赖($dep): ${pkg}（pkg-config 检出）"
    elif command -v "$dep-config" >/dev/null 2>&1 \
         || { [ "$dep" = "llvm" ] && command -v llvm-config >/dev/null 2>&1; }; then
      pass "依赖($dep): ${pkg}"
    else
      fail "缺少 $pkg（dnf install -y $pkg；缺则 §2 configure 必败）"
    fi
  done
  if [ -n "$PG_SRC" ]; then
    [ -f "${PG_SRC}/configure" ] && pass "源码树: ${PG_SRC}" || fail "源码树缺 configure: ${PG_SRC}"
    if [ -d "${PG_SRC}/.git" ]; then
      dirty=$(git -C "${PG_SRC}" status --short 2>/dev/null | wc -l)
      [ "$dirty" -eq 0 ] && pass "源码树干净" || warn "源码树有 ${dirty} 处未提交改动（补丁？记录后再编译）"
      tag=$(git -C "${PG_SRC}" describe --tags --exact-match 2>/dev/null || true)
      [ -n "$tag" ] && pass "版本锚点: ${tag}" || warn "HEAD 不在官方 tag 上（版本口径待记录）"
    fi
  else
    warn "未指定 PG_SRC，跳过源码树检查"
  fi
  if [ -n "$PG_INSTALL" ]; then
    [ -d "${PG_INSTALL}" ] && pass "安装前缀存在: ${PG_INSTALL}" || warn "安装前缀不存在（首次安装属正常）: ${PG_INSTALL}"
    [ -x "${PG_INSTALL}/bin/pg_config" ] && pass "pg_config 可执行" || warn "尚无 pg_config（未安装过）"
  fi
fi

# ---- 部署侧 ----
if [ "$PG_MODE" = "deploy" ] || [ "$PG_MODE" = "both" ]; then
  if [ -z "$PGDATA" ]; then
    warn "未指定 PGDATA，跳过数据目录/实例检查"
  else
    if [ -e "$PGDATA" ]; then
      if [ -f "${PGDATA}/PG_VERSION" ]; then
        pass "已有实例: ${PGDATA} (v$(cat "${PGDATA}/PG_VERSION"))"
      elif [ ! -d "$PGDATA" ]; then
        fail "${PGDATA} 存在但不是目录（文件/失效符号链接？），拒绝 initdb"
      elif ls_out="$(ls -A "$PGDATA" 2>&1)"; then
        # 列举成功才判空：无权列出时 ls 的错误若被丢弃（2>/dev/null），空 stdout
        # 会让非空目录误判为「可 initdb」
        if [ -z "$ls_out" ]; then
          pass "PGDATA 为空目录（可 initdb）: ${PGDATA}"
        else
          fail "${PGDATA} 非空且无 PG_VERSION（非数据目录？拒绝 initdb）"
        fi
      else
        fail "无法列举 ${PGDATA}（权限不足：${ls_out}）——目录可访问性未确认，不能判为可 initdb"
      fi
    else
      pass "数据目录不存在（可 initdb）: ${PGDATA}"
    fi
    PG_CTL="${PG_INSTALL}/bin/pg_ctl"
    if [ -x "$PG_CTL" ] && [ -f "${PGDATA}/PG_VERSION" ]; then
      if "$PG_CTL" -D "$PGDATA" status >/dev/null 2>&1; then
        pass "实例运行中（pg_ctl status）"
      else
        warn "实例未运行"
      fi
    fi
  fi
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn "( sport = :${PG_PORT} )" 2>/dev/null | grep -q LISTEN; then
      pass "端口 ${PG_PORT} 有监听（实例在？核对是否本部署）"
    else
      warn "端口 ${PG_PORT} 无监听"
    fi
  else
    warn "无 ss，跳过端口检查"
  fi
  for c in psql; do command -v "$c" >/dev/null 2>&1 && pass "客户端: $c" || warn "缺少客户端: $c（在 ${PG_INSTALL}/bin）"; done
fi

echo "------------------------------------------------------------"
echo "Summary: PASS=${PASS_CNT} WARN=${WARN_CNT} FAIL=${FAIL_CNT}"
[ "$FAIL_CNT" -eq 0 ] && exit 0 || exit 1
