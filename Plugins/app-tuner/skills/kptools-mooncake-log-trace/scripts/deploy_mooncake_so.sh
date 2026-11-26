#!/usr/bin/env bash
# deploy_mooncake_so.sh — 把编译好的打点 .so 部署替换到容器内现役路径
# 用法:
#   部署:  bash deploy_mooncake_so.sh <容器名> <新.so目录>            # 按文件名自动匹配容器内目标
#          bash deploy_mooncake_so.sh <容器名> --pairs <pairs.txt>    # 显式映射，每行: <新.so本地路径>=<容器内路径>
#   回滚:  bash deploy_mooncake_so.sh --rollback <备份目录> <容器名>
# 纪律: 先校验后写入；备份不全即停；回读 md5 不符即回滚；不自动重启。
set -euo pipefail

# Windows CRLF 自检（本脚本由 Windows 写出时可能带 \r）
if grep -q $'\r' "$0" 2>/dev/null; then
  echo "[FATAL] 脚本含 CR（Windows 换行），先执行: tr -d '\r' < \$0 > fixed.sh && bash fixed.sh ..." >&2
  exit 9
fi

die() { printf '[FATAL] %b\n' "$*" >&2; exit 1; }
info() { echo "[INFO] $*"; }

usage() {
  sed -n '2,9p' "$0"; exit 2
}

[ $# -ge 1 ] || usage

ROLLBACK_DIR=""
CONTAINER=""
NEW_DIR=""
PAIRS=""

mode_deploy=1
if [ "$1" = "--rollback" ]; then
  mode_deploy=0
  [ $# -ge 3 ] || usage
  ROLLBACK_DIR="$2"; CONTAINER="$3"
else
  CONTAINER="$1"
  if [ "${2:-}" = "--pairs" ]; then
    [ $# -ge 3 ] || usage
    PAIRS="$3"
  else
    [ $# -ge 2 ] || usage
    NEW_DIR="$2"
  fi
fi

docker inspect "$CONTAINER" >/dev/null 2>&1 || die "容器不存在: $CONTAINER"
CKSUM="md5sum"; command -v md5sum >/dev/null 2>&1 || CKSUM="sha256sum"
hash_of() { $CKSUM "$1" | awk '{print $1}'; }

# ---------- 回滚模式 ----------
if [ $mode_deploy -eq 0 ]; then
  [ -f "$ROLLBACK_DIR/MANIFEST" ] || die "备份目录无 MANIFEST: $ROLLBACK_DIR"
  info "回滚开始（$ROLLBACK_DIR -> $CONTAINER）"
  while IFS=$'\t' read -r bk cont_path orig_md5; do
    [ -f "$ROLLBACK_DIR/$bk" ] || die "备份件缺失: $bk"
    now=$(hash_of "$ROLLBACK_DIR/$bk")
    [ "$now" = "$orig_md5" ] || die "备份件损坏: $bk 期望 $orig_md5 实际 $now"
    docker cp "$ROLLBACK_DIR/$bk" "$CONTAINER:$cont_path"
    back=$(docker exec "$CONTAINER" $CKSUM "$cont_path" 2>/dev/null | awk '{print $1}')
    [ "$back" = "$orig_md5" ] || die "回滚回读不符: $cont_path"
    info "已回滚: $cont_path ($orig_md5)"
  done < "$ROLLBACK_DIR/MANIFEST"
  info "回滚完成。请重启引擎并 smoke（MKT 日志应消失、服务正常）。"
  exit 0
fi

# ---------- 部署模式 ----------
# 1. 输入预检
[ -n "$NEW_DIR" ] && [ -d "$NEW_DIR" ] || die "新 .so 目录不存在: $NEW_DIR"
NEW_SOS=()
if [ -n "$PAIRS" ]; then
  [ -f "$PAIRS" ] || die "pairs 文件不存在: $PAIRS"
else
  shopt -s nullglob
  for f in "$NEW_DIR"/*.so "$NEW_DIR"/*.so.*; do NEW_SOS+=("$f"); done
  shopt -u nullglob
  [ ${#NEW_SOS[@]} -gt 0 ] || die "目录下无 .so: $NEW_DIR"
fi

# 打点内容校验（先校验后写入）
check_mkt() {
  local f="$1"
  n=$(strings "$f" 2>/dev/null | grep -c "MKT_TTF\|MKT_FINE" || true)
  [ "$n" -ge 2 ] || die "预检失败: $f 不含 MKT 打点串 (count=$n)。禁止部署未验证产物。"
  info "预检通过: $(basename "$f") 含 MKT 打点串"
}

# 2. 建立映射: 新文件 -> 容器内目标路径
declare -A TARGETS
if [ -n "$PAIRS" ]; then
  while IFS='=' read -r local_f cont_p; do
    [ -f "$local_f" ] || die "pairs 指向的本地文件不存在: $local_f"
    check_mkt "$local_f"
    TARGETS["$local_f"]="$cont_p"
  done < "$PAIRS"
else
  for f in "${NEW_SOS[@]}"; do
    check_mkt "$f"
    base=$(basename "$f")
    # 容器内按文件名匹配现役 .so（排除 /proc /sys，同名多份即停）
    hits=$(docker exec "$CONTAINER" find / -name "$base" -not -path "/proc/*" -not -path "/sys/*" 2>/dev/null | grep -v "^$" | sort -u)
    cnt=$(echo "$hits" | grep -c . || true)
    [ "$cnt" -eq 1 ] || die "文件名 $base 在容器内匹配到 $cnt 处:\n$hits\n用 --pairs 显式指定目标路径。"
    TARGETS["$f"]="$hits"
    info "匹配: $base -> $hits"
  done
fi

# 3. 备份（排他私有目录 + MANIFEST + 回读校验）
BAK=$(mktemp -d /tmp/mkt_bak.${CONTAINER}.XXXXXX)
info "备份目录: $BAK"
: > "$BAK/MANIFEST"
i=0
for f in "${!TARGETS[@]}"; do
  cont_p="${TARGETS[$f]}"
  i=$((i+1)); bk="file$i.bak"
  docker cp "$CONTAINER:$cont_p" "$BAK/$bk" || die "备份失败: $cont_p"
  h=$(hash_of "$BAK/$bk")
  printf '%s\t%s\t%s\n' "$bk" "$cont_p" "$h" >> "$BAK/MANIFEST"
  info "已备份: $cont_p -> $BAK/$bk ($h)"
done
# MANIFEST 完整性
[ "$(wc -l < "$BAK/MANIFEST")" -eq "$i" ] || die "MANIFEST 行数不符，备份不完整即停"

# 4. 替换 + md5 回读校验
for f in "${!TARGETS[@]}"; do
  cont_p="${TARGETS[$f]}"
  want=$(hash_of "$f")
  docker cp "$f" "$CONTAINER:$cont_p"
  got=$(docker exec "$CONTAINER" $CKSUM "$cont_p" 2>/dev/null | awk '{print $1}')
  [ "$got" = "$want" ] || die "替换后回读不符: $cont_p 期望 $want 实际 $got。备份已保留: $BAK；回滚: bash $0 --rollback $BAK $CONTAINER"
  info "已替换并校验: $cont_p ($want)"
done

echo
info "部署完成。备份: $BAK（回滚: bash $0 --rollback $BAK $CONTAINER）"
info "下一步（人工执行）: 重启引擎使新 .so 生效（运行中进程仍用旧库），然后发 1 条请求，"
info "在引擎日志验证成对输出（D 侧 grep -E '\\[MKT_TTF\\]|\\[MKT_FINE\\]'，P 侧 grep -E '\\[MKT_PUT\\]|\\[MKT_PFINE\\]'）；无输出按 SKILL.md 第 6 节排查。"
