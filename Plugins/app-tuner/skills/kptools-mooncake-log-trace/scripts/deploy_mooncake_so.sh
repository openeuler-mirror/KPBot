#!/usr/bin/env bash
# deploy_mooncake_so.sh — 把编译好的打点 .so 部署替换到容器内现役路径
# 用法:
#   部署:  bash deploy_mooncake_so.sh <容器名> <新.so目录> --manifest <清单>      # 按文件名自动匹配（要求容器 running）
#          bash deploy_mooncake_so.sh <容器名> --pairs <pairs.txt> --manifest <清单>   # 显式映射，每行: <新.so本地路径>=<容器内路径>
#          bash deploy_mooncake_so.sh ... --allow-no-manifest                    # 显式跳过清单核对（危险，仅限应急）
#   回滚:  bash deploy_mooncake_so.sh --rollback <备份目录> <容器名>
# 清单格式: sha256sum 输出，每行 "<sha256>  <文件名>"（按 basename 匹配），
#           由 kptools-mooncake-instrumentation 交付（产物契约）
# 纪律: 先校验后写入（MKT 串 + sha256 清单双重门禁）；备份不全即停；
#       回读（docker cp 拉回宿主本地哈希，stopped 容器同样可用）不符即自动恢复并停；不自动重启。

# Windows CRLF 自检必须在 set -euo pipefail 之前执行：
# 整文件 CRLF 时 `set -euo pipefail\r` 会被 bash 先以 invalid option 终止，
# 自检反而不可达——守卫先于任何可能被 CR 破坏的语句（用原生 printf，不经 die）
if grep -q $'\r' "$0" 2>/dev/null; then
    printf '[FATAL] %s 含 CR（Windows 换行）：先执行 tr -d 去 CR 后重跑（见 SKILL.md 第 8 节），示例：sed -i s/\r// %s\n' "$0" "$0" >&2
    exit 9
fi

set -euo pipefail

die() { printf '[FATAL] %b\n' "$*" >&2; exit 1; }
info() { echo "[INFO] $*"; }

usage() {
    sed -n '2,11p' "$0"; exit 2
}

[ $# -ge 1 ] || usage

ROLLBACK_DIR=""
CONTAINER=""
NEW_DIR=""
PAIRS=""
MANIFEST=""
ALLOW_NO_MANIFEST=0

mode_deploy=1
if [ "$1" = "--rollback" ]; then
    mode_deploy=0
    [ $# -ge 3 ] || usage
    ROLLBACK_DIR="$2"; CONTAINER="$3"
else
    CONTAINER="$1"; shift || true
    while [ $# -gt 0 ]; do
        case "$1" in
            --pairs)    [ $# -ge 2 ] || usage; PAIRS="$2"; shift 2 ;;
            --manifest) [ $# -ge 2 ] || usage; MANIFEST="$2"; shift 2 ;;
            --allow-no-manifest) ALLOW_NO_MANIFEST=1; shift ;;
            *)  if [ -n "$NEW_DIR" ]; then usage; fi
                NEW_DIR="$1"; shift ;;
        esac
    done
fi

docker inspect "$CONTAINER" >/dev/null 2>&1 || die "容器不存在: $CONTAINER"
# 哈希口径 sha256 优先（与产物清单/仓内备份校验先例一致），宿主机缺 sha256sum 时回退 md5sum
CKSUM="sha256sum"; command -v sha256sum >/dev/null 2>&1 || CKSUM="md5sum"
hash_of() { $CKSUM "$1" | awk '{print $1}'; }
# 容器状态：stopped 容器 docker exec 不可用（find 定位/容器内校验都跑不了），
# 自动匹配模式要求 running；--pairs 显式指定 + docker cp 拉回本地校验则 stopped 亦可部署
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; }
# 容器内文件哈希：docker cp 拉回宿主本地计算（stopped 容器同样可用，docker exec 对 stopped 必败）
hash_in_container() {  # $1=容器内路径 → stdout=哈希
    local t
    t=$(mktemp) || return 1
    if ! docker cp "$CONTAINER:$1" "$t" >/dev/null 2>&1; then rm -f "$t"; return 1; fi
    hash_of "$t"; rm -f "$t"
}

# ---------- 回滚模式 ----------
if [ $mode_deploy -eq 0 ]; then
    [ -f "$ROLLBACK_DIR/MANIFEST" ] || die "备份目录无 MANIFEST: $ROLLBACK_DIR"
    info "回滚开始（$ROLLBACK_DIR -> $CONTAINER）"
    while IFS=$'\t' read -r bk cont_path orig_h; do
        [ -f "$ROLLBACK_DIR/$bk" ] || die "备份件缺失: $bk"
        now=$(hash_of "$ROLLBACK_DIR/$bk")
        [ "$now" = "$orig_h" ] || die "备份件损坏: $bk 期望 $orig_h 实际 $now"
        docker cp "$ROLLBACK_DIR/$bk" "$CONTAINER:$cont_path"
        back=$(hash_in_container "$cont_path") || die "回滚回读失败（docker cp 拉取）: $cont_path"
        [ "$back" = "$orig_h" ] || die "回滚回读不符: $cont_path"
        info "已回滚: $cont_path ($orig_h)"
    done < "$ROLLBACK_DIR/MANIFEST"
    info "回滚完成。请重启引擎并 smoke（MKT 日志应消失、服务正常）。"
    exit 0
fi

# ---------- 部署模式 ----------
# 1. 输入预检
if [ -n "$PAIRS" ]; then
    [ -f "$PAIRS" ] || die "pairs 文件不存在: $PAIRS"
else
    [ -n "$NEW_DIR" ] && [ -d "$NEW_DIR" ] || die "新 .so 目录不存在: ${NEW_DIR:-<未给定>}"
fi
NEW_SOS=()
if [ -n "$PAIRS" ]; then
    while IFS='=' read -r lf _; do [ -n "$lf" ] && NEW_SOS+=("$lf"); done < "$PAIRS"
else
    shopt -s nullglob
    for f in "$NEW_DIR"/*.so "$NEW_DIR"/*.so.*; do NEW_SOS+=("$f"); done
    shopt -u nullglob
fi
[ ${#NEW_SOS[@]} -gt 0 ] || die "输入产物为空（--pairs 或 <新.so目录> 下均无 .so）"

# 打点内容校验（先校验后写入）：四 tag 分别计数各 ≥1——
# get 链 MKT_TTF/MKT_FINE + put 链 MKT_PUT/MKT_PFINE，任一为 0 = 对应链打点
# 没编进目标库（put 链"设计格式串、待实测校准"恰是最易漏编的部分），禁止部署
check_mkt() {
    local f="$1" t n
    for t in MKT_TTF MKT_FINE MKT_PUT MKT_PFINE; do
        n=$(strings "$f" 2>/dev/null | grep -c "$t" || true)
        [ "$n" -ge 1 ] || die "预检失败: $f 缺 $t 打点串 (count=$n)——对应链打点没编进目标库。禁止部署未验证产物。"
    done
    info "预检通过: $(basename "$f") 四 tag（get/put 两链）齐全"
}

# 清单门禁：strings 只能证明"编进了打点"，防不了错版本/篡改/投毒——
# 逐文件与生产者交付的 sha256 清单比对后才允许任何 docker cp。
# 清单缺项/哈希不符即拒收；无清单默认拒绝，--allow-no-manifest 显式跳过（应急用）。
declare -A MANIFEST_H=()
if [ "$MANIFEST" != "" ]; then
    [ -f "$MANIFEST" ] || die "--manifest 清单文件不存在: $MANIFEST"
    while read -r h name; do
        [ -n "$h" ] && [ -n "$name" ] || continue
        MANIFEST_H["$(basename "$name")"]="$h"
    done < <(awk '{h=$1; $1=""; sub(/^[ \t]+/,""); print h "\t" $0}' "$MANIFEST")
fi
check_manifest() {
    local f="$1" b h
    b=$(basename "$f"); h=$(hash_of "$f")
    if [ ${#MANIFEST_H[@]} -eq 0 ]; then
        if [ "$ALLOW_NO_MANIFEST" -ne 1 ]; then
            die "未提供 --manifest 清单：拒绝部署（strings 门禁防不了错版本/篡改/投毒）。请携带 instrumentation skill 交付的 sha256 清单；应急场景显式加 --allow-no-manifest 自担风险。"
        fi
        info "[WARN] --allow-no-manifest：跳过清单核对（仅 MKT 串校验），错版本/篡改风险自担"
        return 0
    fi
    [ -n "${MANIFEST_H[$b]:-}" ] || die "清单不符: $b 不在交付清单中（多出文件）。核对产物目录与清单来源。"
    [ "$h" = "${MANIFEST_H[$b]}" ] || die "清单不符: $b 实际 $h 期望 ${MANIFEST_H[$b]}（错版本/被篡改/清单过期）。核对 instrumentation skill 交付物。"
}

for f in "${NEW_SOS[@]}"; do
    [ -f "$f" ] || die "产物文件不存在: $f"
    check_mkt "$f"
    check_manifest "$f"
done
info "预检通过（MKT 串 + 清单）：${#NEW_SOS[@]} 个 .so"

# 2. 建立映射: 新文件 -> 容器内目标路径
declare -A TARGETS=()
if [ -n "$PAIRS" ]; then
    while IFS='=' read -r local_f cont_p; do
        [ -n "$local_f" ] || continue
        [ -f "$local_f" ] || die "pairs 指向的本地文件不存在: $local_f"
        [ -n "$cont_p" ] || die "pairs 行缺容器路径: $local_f="
        TARGETS["$local_f"]="$cont_p"
    done < "$PAIRS"
else
    container_running || die "容器 $CONTAINER 未运行：自动匹配（docker exec find）不可用。请先启动容器，或改用 --pairs 显式指定容器内目标路径（--pairs + docker cp 校验对 stopped 容器同样有效）。"
    for f in "${NEW_SOS[@]}"; do
        base=$(basename "$f")
        # 容器内按文件名匹配现役 .so（排除 /proc /sys，零匹配/多匹配即停）：
        # 不加 grep -v "^$"——find 无命中时 grep 零选中会以 exit 1 结束管道，
        # set -euo pipefail 下 $() 赋值失败会静默退出脚本，零匹配的 die 反而不可达；
        # find 本就不输出空行，|| true 兜底后统一由 cnt 判定走显式报错
        hits=$(docker exec "$CONTAINER" find / -name "$base" -not -path "/proc/*" -not -path "/sys/*" 2>/dev/null | sort -u || true)
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
    docker cp "$CONTAINER:$cont_p" "$BAK/$bk" >/dev/null || die "备份失败: $cont_p"
    h=$(hash_of "$BAK/$bk")
    printf '%s\t%s\t%s\n' "$bk" "$cont_p" "$h" >> "$BAK/MANIFEST"
    info "已备份: $cont_p -> $BAK/$bk ($h)"
done
[ "$(wc -l < "$BAK/MANIFEST")" -eq "$i" ] || die "MANIFEST 行数不符，备份不完整即停"

# 4. 替换 + 回读校验（docker cp 拉回本地哈希，stopped 容器同样可用）；
#    回读不符自动从备份恢复已动文件，恢复后复验并停（不留半部署状态）
RESTORE_OK=1
restore_all() {
    info "回读不符 → 自动恢复（按 MANIFEST 回拷已动文件）"
    while IFS=$'\t' read -r bk cont_p orig_h; do
        docker cp "$ROLLBACK_TMP_BAK/$bk" "$CONTAINER:$cont_p" >/dev/null 2>&1 || { RESTORE_OK=0; continue; }
        back=$(hash_in_container "$cont_p" 2>/dev/null || echo "?")
        [ "$back" = "$orig_h" ] || RESTORE_OK=0
    done < "$ROLLBACK_TMP_BAK/MANIFEST"
}
ROLLBACK_TMP_BAK=""
for f in "${!TARGETS[@]}"; do
    cont_p="${TARGETS[$f]}"
    want=$(hash_of "$f")
    docker cp "$f" "$CONTAINER:$cont_p" >/dev/null || {
        ROLLBACK_TMP_BAK="$BAK"; restore_all
        if [ "$RESTORE_OK" -eq 1 ]; then die "写入失败: $cont_p 已按备份自动恢复（备份 $BAK）"; else die "写入失败且自动恢复不完整（$BAK 保留，请人工 --rollback）"; fi
    }
    got=$(hash_in_container "$cont_p") || got=""
    if [ "$got" != "$want" ]; then
        ROLLBACK_TMP_BAK="$BAK"; restore_all
        if [ "$RESTORE_OK" -eq 1 ]; then
            die "替换后回读不符: $cont_p 期望 $want 实际 ${got:-<拉取失败>}。已按备份自动恢复，容器回到部署前状态。"
        else
            die "替换后回读不符: $cont_p 期望 $want 实际 ${got:-<拉取失败>}。自动恢复不完整——立即人工回滚: bash $0 --rollback $BAK $CONTAINER"
        fi
    fi
    info "已替换并校验: $cont_p ($want)"
done

echo
info "部署完成。备份: $BAK（回滚: bash $0 --rollback $BAK $CONTAINER）"
info "下一步（人工执行）: 重启引擎使新 .so 生效（运行中进程仍用旧库），然后发 1 条请求，"
info "在引擎日志验证成对输出（D 侧 grep -E '\\[MKT_TTF\\]|\\[MKT_FINE\\]'，P 侧 grep -E '\\[MKT_PUT\\]|\\[MKT_PFINE\\]'）；无输出按 SKILL.md 第 6 节排查。"
