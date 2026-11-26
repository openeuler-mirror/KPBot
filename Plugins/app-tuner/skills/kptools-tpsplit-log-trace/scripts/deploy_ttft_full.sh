#!/usr/bin/env bash
# deploy_ttft_full.sh — TTFT breakdown 打点 patch 一键部署（known-good 2026-11-18 v3.2）
#
# 用法: bash scripts/deploy_ttft_full.sh <CONTAINER> [SRC] [KVXFER]
#   CONTAINER  目标容器名（P 或 D 侧 vllm 引擎容器；stopped 状态也可部署）
#   SRC        patch 包目录，默认 /data/ttft/patched_src（需先 scp 上传到宿主机）
#   KVXFER     SP ascend_store patch 目录，默认 $SRC/ascend_store（可选）
# 可选环境变量:
#   PROXY_TGT  proxy 文件目标路径（宿主机进程），设置后连 proxy patch 一起推（HOST_FP 指纹门禁）
#   BENCH_TGT  压测端目录（宿主机进程），设置后连 bench patch 一起推（HOST_FP 指纹门禁）
#   ALLOW_UNKNOWN_DIGEST=1  现役摘要既非目标也非已知打点版本时，经人工确认差异后放行（默认停止）
#
# 安全红线（对应 skill 踩坑清单）:
#   1. 先核验后写入：输入文件（vllm/SP/KVXFER/proxy/bench）存在、known-good 指纹匹配、py_compile 通过，任一不过禁止进目标
#   2. 备份先行且覆盖本轮全部目标（vllm/SP/KVXFER/proxy/bench），按原路径记录 MANIFEST；备份不完整即停
#   3. 基座指纹不可读或不符即停；现役摘要未知即停（防覆盖现场定制，人工确认后显式放行）
#   4. 只用 docker cp 覆盖（stopped 容器 docker exec 无效）；docker cp 双向均可用
#   5. 不自动重启引擎/proxy——重启顺序由用户手动执行（见脚本尾部提示）

set -euo pipefail

# ---------- 0. CR 自检：Windows 写出的 .sh 带 \r，bash 解析会崩 ----------
if grep -q $'\r' "$0" 2>/dev/null; then
    echo "[FATAL] 本脚本含 CRLF 行尾（Windows 传送导致）。请先执行:"
    echo "         tr -d '\r' < $0 > $0.lf && mv $0.lf $0"
    exit 1
fi

# ---------- 1. 参数 ----------
CONTAINER="${1:?用法: bash deploy_ttft_full.sh <CONTAINER> [SRC] [KVXFER]}"
SRC="${2:-/data/ttft/patched_src}"
KVXFER="${3:-${SRC}/ascend_store}"

VLLM_ROOT="/workspace/vllm"                # 容器内 vllm 源码根（PYTHONPATH 注入）
SP_EXPECT="/usr/local/python3.12.13/lib/python3.12/site-packages"  # vllm_ascend 所在 SP

# ---------- 2. 16 文件清单 ----------
# vllm 侧（SRC 相对路径 -> 容器内 $VLLM_ROOT 下同路径）
VLLM_FILES=(
    "vllm/v1/metrics/stats.py"
    "vllm/v1/engine/__init__.py"
    "vllm/v1/engine/core.py"
    "vllm/v1/engine/input_processor.py"
    "vllm/v1/outputs.py"
    "vllm/v1/request.py"
    "vllm/v1/core/sched/output.py"
    "vllm/v1/core/sched/scheduler.py"
    "vllm/v1/worker/gpu_model_runner.py"
    "vllm/v1/worker/kv_connector_model_runner_mixin.py"
    "vllm/distributed/device_communicators/shm_broadcast.py"
)
# vllm_ascend SP 侧（find 定位实际 SP 路径）
SP_FILES=(
    "vllm_ascend/core/recompute_scheduler.py"
    "vllm_ascend/worker/model_runner_v1.py"
)
# ascend_store KVXFER 侧（find 定位 kv_pool/ascend_store 目录）
KVXFER_FILES=(
    "pool_worker.py"
    "ascend_store_connector.py"
    "kv_transfer.py"
)

# 版本指纹（known-good v3.2，md5 前 8 位）——16 文件全量登记；登记不全脚本拒绝运行（拒绝绕过内容核验）
# 写入前比对上传件 md5（步骤 1/7），写入后回读比对（步骤 6/7 deploy_one）
declare -A PATCH_FP=(
    ["vllm/v1/metrics/stats.py"]="9f9f5276"
    ["vllm/v1/engine/__init__.py"]="70fdbb10"
    ["vllm/v1/engine/core.py"]="d05f9ff2"
    ["vllm/v1/engine/input_processor.py"]="e8021a2d"
    ["vllm/v1/outputs.py"]="cfb892e8"
    ["vllm/v1/request.py"]="dfc95660"
    ["vllm/v1/core/sched/output.py"]="c6d9b049"
    ["vllm/v1/core/sched/scheduler.py"]="bbaa8a28"
    ["vllm/v1/worker/gpu_model_runner.py"]="0a59cde4"
    ["vllm/v1/worker/kv_connector_model_runner_mixin.py"]="5f69d00f"
    ["vllm/distributed/device_communicators/shm_broadcast.py"]="55821b06"
    ["vllm_ascend/core/recompute_scheduler.py"]="8e37abd1"
    ["vllm_ascend/worker/model_runner_v1.py"]="2e7db5f3"
    ["kvxfer/pool_worker.py"]="f5b54a27"
    ["kvxfer/ascend_store_connector.py"]="de0ea38f"
    ["kvxfer/kv_transfer.py"]="cbdb71ec"
)
# 基座指纹（patch 未触碰文件）——不可读或不符 = 基座不同/未知，停下确认
declare -A BASE_FP=(
    ["detokenizer.py"]="3fe8bf88"
    ["kv_cache_manager.py"]="d1a6c67c"
    ["version.py"]="2b4efcd3"
)
# 宿主机侧（proxy/压测端）known-good 指纹——与 PATCH_FP 同一门禁语义：
# 设置 PROXY_TGT/BENCH_TGT 时纳入输入预检（md5 前缀比对 + py_compile），登记不全/不符即拒；
# 写入后对宿主机目标回读 md5 并比对指纹（步骤 6/7 尾部）
declare -A HOST_FP=(
    ["proxy/load_balance_proxy_server_example.py"]="b7061c0b"
    ["utils/bench_serving/backend_request_func.py"]="03c3c37f"
    ["utils/bench_serving/benchmark_serving.py"]="487540b8"
)
# 部署后 grep 校验关键字段（容器内现役文件中应 >0）
GREP_KEYS=(frontend_ts schedule_total_time queue_wait step_other add_req_iq \
           TTFT_DECODE TTFT_KVXFER start_load_kv TTFT_DRAFT _ttft_in_draft _TTFT_SHM_DQ)

# ---------- 3. 工作目录 + 私有备份目录（排他创建，并发部署 P/D 互不覆盖） ----------
WORK="$(mktemp -d /tmp/ttft_deploy.XXXXXX)" || { echo "[FATAL] 工作目录创建失败"; exit 1; }
BAK="$(mktemp -d "/tmp/ttft_bak.${CONTAINER}.XXXXXX")" || { echo "[FATAL] 备份目录创建失败"; exit 1; }
VERIFY="$WORK/verify"
MANIFEST="$BAK/MANIFEST.txt"
mkdir -p "$VERIFY" "$BAK/vllm" "$BAK/sp" "$BAK/kvxfer" "$BAK/host" || { echo "[FATAL] 目录初始化失败"; exit 1; }
{ echo "container=$CONTAINER"; echo "created=$(date '+%F %T')"; } > "$MANIFEST"
ASSUMED_NEW_LIST=()   # stopped 容器上拉取失败、按原不存在处理的路径（备份后复核，防无备份覆盖）

log()  { echo "[deploy] $*"; }
die()  { echo "[FATAL] $*"; echo "[deploy] 备份目录: $BAK（原路径映射见 $MANIFEST；未覆盖或已部分覆盖均可从此回滚）"; exit 1; }

# 容器内文件 -> 宿主机 md5（stopped 容器：docker cp 拉出再算；docker cp 失败回退 exec）
container_md5() {
    local cpath="$1" t="$WORK/cur_$(basename "$cpath")"
    if docker cp "$CONTAINER:$cpath" "$t" >/dev/null 2>&1; then
        md5sum "$t" | awk '{print $1}'
    elif docker exec "$CONTAINER" md5sum "$cpath" >/dev/null 2>&1; then
        docker exec "$CONTAINER" md5sum "$cpath" | awk '{print $1}' || echo ""
    else
        echo ""
    fi
}

# 备份容器内现役文件：成功 / NEW（原不存在）/ 读取失败即停；stopped 容器无法 exec 区分时记 assumed-new
backup_container_file() {  # $1=容器内路径 $2=BAK 子目录
    local cpath="$1" dst="$BAK/$2/$(basename "$1")"
    if docker cp "$CONTAINER:$cpath" "$dst" >/dev/null 2>&1; then
        echo "$cpath -> $dst" >> "$MANIFEST"
        return 0
    fi
    if docker exec "$CONTAINER" true >/dev/null 2>&1; then
        if docker exec "$CONTAINER" test -e "$cpath" >/dev/null 2>&1; then
            die "备份读取失败（文件存在但拉取不到）: $cpath —— 备份不完整，禁止继续覆盖"
        fi
        echo "$cpath -> NEW（容器内原不存在）" >> "$MANIFEST"
        log "  [NEW] $cpath（容器内原不存在）"
    else
        echo "$cpath -> NEW-assumed（容器 stopped，docker cp 未取到，按原不存在处理）" >> "$MANIFEST"
        log "  [NEW?] $cpath（stopped 容器拉取失败，按原不存在处理；如非预期请人工核对）"
        ASSUMED_NEW_LIST+=("$cpath")
    fi
}

# 备份宿主机文件（proxy/压测端原件）
backup_host_file() {  # $1=原文件路径
    local orig="$1" dst="$BAK/host/$(basename "$1")"
    if [ -f "$orig" ]; then
        cp -f "$orig" "$dst" || die "备份复制失败: $orig"
        echo "$orig -> $dst" >> "$MANIFEST"
    else
        echo "$orig -> NEW（宿主机原不存在）" >> "$MANIFEST"
        log "  [NEW] $orig（宿主机原不存在）"
    fi
}

# 解析宿主机最终目标文件路径：目标为已存在目录时按 cp 落地语义指向目录内同名文件，
# 备份与回读校验均按该最终路径进行（防止目录目标被登记为 NEW 后，目录内同名原件被覆盖且无备份可回滚）
resolve_host_tgt() {  # $1=目标路径 $2=源文件相对路径
    if [ -d "$1" ]; then
        printf '%s/%s\n' "$1" "$(basename "$2")"
    else
        printf '%s\n' "$1"
    fi
}

log "目标容器: $CONTAINER  SRC: $SRC  KVXFER: $KVXFER"
log "工作目录: $WORK   备份目录: $BAK"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "容器不存在: $CONTAINER"
[ -d "$SRC" ] || die "SRC 不存在: $SRC（先 scp -r patched_src root@宿主:/data/<user>/）"

# ---------- 4. 步骤 1/7: 输入预检（存在性 / known-good 指纹 / py_compile，全部在写入前完成） ----------
log "== 步骤 1/7: 输入预检（文件存在 / known-good 指纹全量比对 / py_compile） =="
for f in "${VLLM_FILES[@]}" "${SP_FILES[@]}"; do
    [ -f "$SRC/$f" ] || die "patch 包缺文件: $SRC/$f"
    [ -n "${PATCH_FP[$f]:-}" ] || die "指纹登记不全: $f 无 known-good 指纹 —— 拒绝绕过内容核验，请先在 PATCH_FP 登记"
    in_md5="$(md5sum "$SRC/$f" | awk '{print $1}')"
    [[ "$in_md5" = "${PATCH_FP[$f]}"* ]] \
        || die "输入指纹不符: $f md5=${in_md5:0:8} 期望=${PATCH_FP[$f]} —— 禁止携带未验证版本"
done
KVXFER_MISSING=0
for f in "${KVXFER_FILES[@]}"; do
    if [ ! -f "$KVXFER/$f" ]; then
        KVXFER_MISSING=1
        log "  [WARN] KVXFER 包缺 $f —— 三件套整体跳过（不做部分部署）；grep 校验仍要求现役已含其打点"
    fi
done
if [ "$KVXFER_MISSING" -eq 0 ]; then
    for f in "${KVXFER_FILES[@]}"; do
        [ -n "${PATCH_FP[kvxfer/$f]:-}" ] || die "指纹登记不全: kvxfer/$f 无 known-good 指纹 —— 拒绝绕过内容核验"
        in_md5="$(md5sum "$KVXFER/$f" | awk '{print $1}')"
        [[ "$in_md5" = "${PATCH_FP[kvxfer/$f]}"* ]] \
            || die "输入指纹不符: kvxfer/$f md5=${in_md5:0:8} 期望=${PATCH_FP[kvxfer/$f]} —— 禁止携带未验证版本"
    done
fi
# 宿主机侧（proxy/压测端）：与容器侧同一输入预检门禁（存在 + known-good 指纹），写入前完成
HOST_FILES=()
if [ -n "${PROXY_TGT:-}" ]; then
    HOST_FILES+=("proxy/load_balance_proxy_server_example.py")
fi
if [ -n "${BENCH_TGT:-}" ]; then
    HOST_FILES+=("utils/bench_serving/backend_request_func.py"
                 "utils/bench_serving/benchmark_serving.py")
fi
if [ "${#HOST_FILES[@]}" -gt 0 ]; then
    for hf in "${HOST_FILES[@]}"; do
        [ -f "$SRC/$hf" ] || die "设置了 PROXY_TGT/BENCH_TGT 但 patch 包缺文件: $SRC/$hf"
        [ -n "${HOST_FP[$hf]:-}" ] || die "指纹登记不全: $hf 无 known-good 指纹 —— 拒绝绕过内容核验，请先在 HOST_FP 登记"
        in_md5="$(md5sum "$SRC/$hf" | awk '{print $1}')"
        [[ "$in_md5" = "${HOST_FP[$hf]}"* ]] \
            || die "输入指纹不符: $hf md5=${in_md5:0:8} 期望=${HOST_FP[$hf]} —— 禁止携带未验证版本"
    done
    # 目标路径预检（同样在写入前完成）：PROXY_TGT/BENCH_TGT 写错（父目录不存在、
    # BENCH_TGT 不是目录）时，cp 失败会发生在容器 16 文件已被覆盖之后——留下
    # 『容器已打点、proxy/压测端未打点』的半部署。这里提前拦截，任一目标不可用
    # 都在覆盖任何文件之前停止。
    if [ -n "${PROXY_TGT:-}" ]; then
        ptgt_pre="$(resolve_host_tgt "$PROXY_TGT" "proxy/load_balance_proxy_server_example.py")"
        pdir="$(dirname "$ptgt_pre")"
        [ -d "$pdir" ] || die "PROXY_TGT 目标父目录不存在: $pdir（解析自 PROXY_TGT=$PROXY_TGT）—— 写入前停止，未覆盖任何文件"
        [ -w "$pdir" ] || die "PROXY_TGT 目标父目录不可写: $pdir —— 写入前停止，未覆盖任何文件"
        if [ -f "$ptgt_pre" ] && [ ! -w "$ptgt_pre" ]; then
            die "PROXY_TGT 目标文件已存在但不可写: $ptgt_pre —— 写入前停止，未覆盖任何文件"
        fi
    fi
    if [ -n "${BENCH_TGT:-}" ]; then
        [ -d "$BENCH_TGT" ] || die "BENCH_TGT 必须是已存在且可写的目录（当前不存在或不是目录）: $BENCH_TGT —— 写入前停止，未覆盖任何文件"
        [ -w "$BENCH_TGT" ] || die "BENCH_TGT 目录不可写: $BENCH_TGT —— 写入前停止，未覆盖任何文件"
    fi
fi
PYC_ERR=0
for f in "${VLLM_FILES[@]}" "${SP_FILES[@]}"; do
    python3 -m py_compile "$SRC/$f" >/dev/null 2>&1 || { echo "[FATAL] py_compile 失败: $SRC/$f"; PYC_ERR=1; }
done
if [ "$KVXFER_MISSING" -eq 0 ]; then
    for f in "${KVXFER_FILES[@]}"; do
        python3 -m py_compile "$KVXFER/$f" >/dev/null 2>&1 || { echo "[FATAL] py_compile 失败: $KVXFER/$f"; PYC_ERR=1; }
    done
fi
if [ "${#HOST_FILES[@]}" -gt 0 ]; then
    for hf in "${HOST_FILES[@]}"; do
        python3 -m py_compile "$SRC/$hf" >/dev/null 2>&1 || { echo "[FATAL] py_compile 失败: $SRC/$hf"; PYC_ERR=1; }
    done
fi
[ "$PYC_ERR" -eq 0 ] || die "输入 py_compile 未通过，禁止写入目标"
log "  输入预检通过（全部输入已核验，可安全写入）"

# ---------- 5. 步骤 2/7: 定位 SP / KVXFER 路径 ----------
log "== 步骤 2/7: 定位 site-packages 路径 =="
# 容器在跑优先 exec find；stopped 则用默认 SP 并用 docker cp 探测验证。
# find 不限深度 + prune /proc /sys + -print -quit 命中即止：文档声明的 SP 布局下
# model_runner_v1.py 在第 9 层、ascend_store 在第 11 层，原 -maxdepth 8/9 匹配不到
SP_ROOT=""
SP_RUNNING=0
if docker exec "$CONTAINER" true >/dev/null 2>&1; then
    SP_RUNNING=1
    SP_ROOT="$(docker exec "$CONTAINER" find / \( -path /proc -o -path /sys \) -prune -o \
               -path '*site-packages/vllm_ascend/worker/model_runner_v1.py' -print -quit 2>/dev/null \
               | sed 's#vllm_ascend/worker/model_runner_v1.py$##' || true)"
fi
if [ -z "$SP_ROOT" ]; then
    # stopped 容器：按默认 SP 探测
    if docker cp "$CONTAINER:$SP_EXPECT/vllm_ascend/worker/model_runner_v1.py" "$WORK/probe_mr1" >/dev/null 2>&1; then
        SP_ROOT="$SP_EXPECT/"
    elif [ "$SP_RUNNING" -eq 1 ]; then
        die "无法定位 vllm_ascend SP 路径（未找到）：容器在跑但 find 未匹配到 site-packages/vllm_ascend/worker/model_runner_v1.py，且默认 $SP_EXPECT 探测不匹配——布局与预期不符。请手工核对:\n  docker exec $CONTAINER find / -path '*vllm_ascend/worker/model_runner_v1.py'"
    else
        die "无法定位 vllm_ascend SP 路径（路径不匹配）：容器 stopped 且默认 $SP_EXPECT 探测失败。请启动容器后重试，或手工 find:\n  find / -path '*site-packages/vllm_ascend/core/recompute_scheduler.py'"
    fi
fi
log "  SP_ROOT = $SP_ROOT"

KVXFER_DST=""
if [ "$SP_RUNNING" -eq 1 ]; then
    KVXFER_DST="$(docker exec "$CONTAINER" find / \( -path /proc -o -path /sys \) -prune -o \
                   -type d -path '*vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store' -print -quit 2>/dev/null || true)"
fi
if [ -z "$KVXFER_DST" ]; then
    KVXFER_DST="${SP_ROOT}vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store"
    if ! docker cp "$CONTAINER:$KVXFER_DST/kv_transfer.py" "$WORK/probe_kv" >/dev/null 2>&1; then
        # 探测失败必须致命：继续写盘会形成『先覆盖 vllm/SP、KVXFER 才失败』的部分部署；
        # 全部路径探测都发生在任何 docker cp 写盘之前（步骤 2/7 早于步骤 6/7）
        if [ "$SP_RUNNING" -eq 1 ]; then
            die "ascend_store 定位失败（未找到）：容器在跑但 find 未匹配到 kv_pool/ascend_store，且默认 $KVXFER_DST 探测不匹配——引擎版本可能不含 ascend_store KV transfer。路径未验证禁止写盘，请手工核对:\n  docker exec $CONTAINER find / -type d -path '*vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store'"
        else
            die "ascend_store 定位失败（路径不匹配）：容器 stopped 且默认 $KVXFER_DST 探测失败。请启动容器后重试或手工 find 定位——路径未验证禁止写盘（防部分部署后才失败）"
        fi
    fi
fi
log "  KVXFER_DST = $KVXFER_DST"

# ---------- 6. 步骤 3/7: 备份本轮全部目标（vllm / SP / KVXFER / proxy / bench） ----------
log "== 步骤 3/7: 备份本轮全部目标 =="
for f in "${VLLM_FILES[@]}"; do
    backup_container_file "$VLLM_ROOT/$f" vllm
done
for f in "${SP_FILES[@]}"; do
    backup_container_file "${SP_ROOT}${f}" sp
done
if [ "$KVXFER_MISSING" -eq 0 ]; then
    for f in "${KVXFER_FILES[@]}"; do
        backup_container_file "$KVXFER_DST/$f" kvxfer
    done
fi
if [ -n "${PROXY_TGT:-}" ]; then
    # 先解析最终目标文件（PROXY_TGT 为已存在目录时指向目录内同名文件），备份与校验均按该路径
    backup_host_file "$(resolve_host_tgt "$PROXY_TGT" "proxy/load_balance_proxy_server_example.py")"
fi
if [ -n "${BENCH_TGT:-}" ]; then
    backup_host_file "$BENCH_TGT/backend_request_func.py"
    backup_host_file "$BENCH_TGT/benchmark_serving.py"
fi
# 复核"假定不存在"的文件：若实际可读，说明备份阶段漏了原件，禁止继续（防无备份覆盖）
if [ "${#ASSUMED_NEW_LIST[@]}" -gt 0 ]; then
    for cpath in "${ASSUMED_NEW_LIST[@]}"; do
        if [ -n "$(container_md5 "$cpath")" ]; then
            die "备份阶段按不存在处理的文件实际可读: $cpath —— 备份缺失，禁止覆盖，请先补备份再重跑"
        fi
    done
fi
log "  备份完成，原路径映射见 $MANIFEST"

# ---------- 7. 步骤 4/7: 基座指纹核对（不可读或不符即停） ----------
log "== 步骤 4/7: 基座指纹核对（不可读即停） =="
declare -A BASE_PATHS=(
    ["detokenizer.py"]="$VLLM_ROOT/vllm/v1/engine/detokenizer.py"
    ["kv_cache_manager.py"]="$VLLM_ROOT/vllm/v1/core/kv_cache_manager.py"
    ["version.py"]="$VLLM_ROOT/vllm/version.py"
)
for name in "${!BASE_FP[@]}"; do
    cur="$(container_md5 "${BASE_PATHS[$name]}")"
    want="${BASE_FP[$name]}"
    if [ -z "$cur" ]; then
        die "基座文件 $name 不可读，无法核对基座 —— 先人工确认目标基座，禁止盲目继续"
    fi
    if [[ "$cur" != "$want"* ]]; then
        die "基座指纹不符: $name 现值=${cur:0:8} 期望=$want —— 基座与已验证环境不同，禁止强推，请人工确认"
    fi
    log "  基座 OK: $name=${cur:0:8}"
done

# ---------- 8. 步骤 5/7: 现役 vs 目标 md5（vllm / SP / KVXFER 全量；未知摘要即停） ----------
log "== 步骤 5/7: 现役 vs 目标 md5（vllm / SP / KVXFER 全量） =="
SKIP_LIST=()
UNKNOWN_LIST=()
compare_current() {  # $1=上传文件 $2=容器内路径 $3=登记名
    local lf="$1" cpath="$2" rel="$3" tgt_md5 cur_md5
    tgt_md5="$(md5sum "$lf" | awk '{print $1}')"
    cur_md5="$(container_md5 "$cpath")"
    if [ -n "$cur_md5" ] && [ "$tgt_md5" = "$cur_md5" ]; then
        log "  $rel: 已与目标一致，跳过写入"
        SKIP_LIST+=("$rel")
    elif [ -z "$cur_md5" ]; then
        log "  $rel: 容器内原不存在，首次写入"
    elif [ -n "${PATCH_FP[$rel]:-}" ] && [[ "$cur_md5" = "${PATCH_FP[$rel]}"* ]]; then
        log "  $rel: 现役为已知打点版本(md5=${cur_md5:0:8})，将升级到目标版本"
    else
        log "  $rel: 现役摘要未知(${cur_md5:0:8})，既非目标也非已知打点版本"
        UNKNOWN_LIST+=("$rel=${cur_md5:0:8}")
    fi
}
for f in "${VLLM_FILES[@]}"; do
    compare_current "$SRC/$f" "$VLLM_ROOT/$f" "$f"
done
for f in "${SP_FILES[@]}"; do
    compare_current "$SRC/$f" "${SP_ROOT}${f}" "$f"
done
if [ "$KVXFER_MISSING" -eq 0 ]; then
    for f in "${KVXFER_FILES[@]}"; do
        compare_current "$KVXFER/$f" "$KVXFER_DST/$f" "kvxfer/$f"
    done
fi
if [ "${#UNKNOWN_LIST[@]}" -gt 0 ]; then
    # 仅接受字面值 1（显式 0/false/其他值一律保持停止，防误设放行未知摘要）
    if [ "${ALLOW_UNKNOWN_DIGEST:-}" = "1" ]; then
        log "  [WARN] ALLOW_UNKNOWN_DIGEST=1（字面值），以下未知摘要按人工确认放行："
        printf '    %s\n' "${UNKNOWN_LIST[@]}"
    else
        echo "[FATAL] 以下现役文件摘要未知（可能含现场定制），未经人工确认禁止覆盖："
        printf '    %s\n' "${UNKNOWN_LIST[@]}"
        die "人工确认差异可接受后，设 ALLOW_UNKNOWN_DIGEST=1 重跑放行"
    fi
fi

# ---------- 9. 步骤 6/7: 覆盖（docker cp + 回读校验；stopped 容器同样有效） ----------
log "== 步骤 6/7: docker cp 覆盖（16 文件）+ proxy/压测端 =="
deploy_one() {  # $1=本地文件 $2=容器内绝对路径 $3=相对名（用于指纹）
    local lf="$1" cpath="$2" rel="$3"
    [ -f "$lf" ] || die "patch 包缺文件: $lf"
    docker cp "$lf" "$CONTAINER:$cpath" || die "docker cp 失败: $lf -> $cpath"
    # 逐字节校验：拉回来比 md5
    docker cp "$CONTAINER:$cpath" "$VERIFY/$(basename "$cpath")" >/dev/null 2>&1 \
        || die "回拉校验失败: $cpath"
    local got want
    got="$(md5sum "$VERIFY/$(basename "$cpath")" | awk '{print $1}')"
    want="$(md5sum "$lf" | awk '{print $1}')"
    [ "$got" = "$want" ] || die "md5 不一致(部署后): $cpath got=${got:0:8} want=${want:0:8}"
    if [ -n "${PATCH_FP[$rel]:-}" ]; then
        [[ "$got" = "${PATCH_FP[$rel]}"* ]] \
            || die "known-good 指纹不符: $rel got=${got:0:8} want=${PATCH_FP[$rel]} —— 禁止携带未验证版本"
    fi
    log "  OK $cpath (md5=${got:0:8})"
}
for f in "${VLLM_FILES[@]}"; do
    # shellcheck disable=SC2076
    if [[ " ${SKIP_LIST[*]-} " == *" $f "* ]]; then
        # 跳过写入的文件仍拉取现役内容，纳入 grep 校验范围（防重跑误报缺字段）
        docker cp "$CONTAINER:$VLLM_ROOT/$f" "$VERIFY/$(basename "$f")" >/dev/null 2>&1 \
            || die "拉取现役文件失败: $f"
        continue
    fi
    deploy_one "$SRC/$f" "$VLLM_ROOT/$f" "$f"
done
for f in "${SP_FILES[@]}"; do
    # shellcheck disable=SC2076
    if [[ " ${SKIP_LIST[*]-} " == *" $f "* ]]; then
        docker cp "$CONTAINER:${SP_ROOT}${f}" "$VERIFY/$(basename "$f")" >/dev/null 2>&1 \
            || die "拉取现役文件失败: $f"
        continue
    fi
    deploy_one "$SRC/$f" "${SP_ROOT}${f}" "$f"
done
if [ "$KVXFER_MISSING" -eq 0 ]; then
    for f in "${KVXFER_FILES[@]}"; do
        rel="kvxfer/$f"
        # shellcheck disable=SC2076
        if [[ " ${SKIP_LIST[*]-} " == *" $rel "* ]]; then
            docker cp "$CONTAINER:$KVXFER_DST/$f" "$VERIFY/$(basename "$f")" >/dev/null 2>&1 \
                || die "拉取现役文件失败: $rel"
            continue
        fi
        deploy_one "$KVXFER/$f" "$KVXFER_DST/$f" "$rel"
    done
else
    # KVXFER 包缺失：跳过写入，但拉取现役内容进 VERIFY，保证 grep 校验覆盖现役全量
    #（此前已打点的容器可正常通过；未打点且确实缺包时由 grep 步骤明确报缺）
    for f in "${KVXFER_FILES[@]}"; do
        docker cp "$CONTAINER:$KVXFER_DST/$f" "$VERIFY/$(basename "$f")" >/dev/null 2>&1 \
            || log "  [WARN] 现役 KVXFER 文件拉取失败（未部署过或路径变化）: $f"
    done
fi

# proxy / 压测端（宿主机进程，可选）：逐个复制，回读 md5 比对 + known-good 指纹核对，失败即停
if [ -n "${PROXY_TGT:-}" ]; then
    hf="proxy/load_balance_proxy_server_example.py"
    ptgt="$(resolve_host_tgt "$PROXY_TGT" "$hf")"
    cp -f "$SRC/$hf" "$ptgt" \
        || die "proxy patch 复制失败 -> $ptgt"
    got="$(md5sum "$ptgt" | awk '{print $1}')"
    want="$(md5sum "$SRC/$hf" | awk '{print $1}')"
    [ "$got" = "$want" ] || die "proxy patch 回读 md5 不一致: $ptgt got=${got:0:8} want=${want:0:8}"
    [[ "$got" = "${HOST_FP[$hf]}"* ]] \
        || die "known-good 指纹不符(部署后): $hf got=${got:0:8} want=${HOST_FP[$hf]} —— 禁止携带未验证版本"
    log "  proxy patch -> $ptgt (md5=${got:0:8})（记得重启宿主机 proxy 进程）"
fi
if [ -n "${BENCH_TGT:-}" ]; then
    for bf in backend_request_func.py benchmark_serving.py; do
        hf="utils/bench_serving/$bf"
        cp -f "$SRC/$hf" "$BENCH_TGT/" \
            || die "bench patch 复制失败: $bf -> $BENCH_TGT/"
        got="$(md5sum "$BENCH_TGT/$bf" | awk '{print $1}')"
        want="$(md5sum "$SRC/$hf" | awk '{print $1}')"
        [ "$got" = "$want" ] || die "bench patch 回读 md5 不一致: $BENCH_TGT/$bf got=${got:0:8} want=${want:0:8}"
        [[ "$got" = "${HOST_FP[$hf]}"* ]] \
            || die "known-good 指纹不符(部署后): $hf got=${got:0:8} want=${HOST_FP[$hf]} —— 禁止携带未验证版本"
    done
    log "  bench patch -> $BENCH_TGT"
fi

# ---------- 10. 步骤 7/7: 校验 grep 关键字段（覆盖全部现役文件，应全部 >0） ----------
log "== 步骤 7/7: grep 关键字段（现役全量，应全部 >0） =="
FAIL=0
for k in "${GREP_KEYS[@]}"; do
    # grep 无匹配退出码 1，|| true 防止 pipefail+set -e 在赋值处静默中止（缺失字段交给下方 FAIL 汇总）
    n=$(grep -r -c -- "$k" "$VERIFY" 2>/dev/null | awk -F: '{s+=$NF} END{print s+0}' || true)
    if [ "$n" -gt 0 ]; then
        log "  OK  $k ($n)"
    else
        echo "[FATAL] 关键字段 $k 在现役文件中 grep=0"
        FAIL=1
    fi
done
[ "$FAIL" -eq 0 ] || die "grep 校验未通过（见上方缺失字段）"

# ---------- 11. 收尾：不自动重启 ----------
cat <<'EOF'

[deploy] 部署+校验完成。注意：部署 != 运行，必须重启 + smoke 才算闭环。
本脚本不自动重启引擎/proxy，请按以下顺序手动操作：
  1. pkill launch_online_dp / vllm serve / EngineCore（只杀本集群进程！同机他人容器勿停勿改）
  2. P 侧起 mooncake_master
  3. P 侧 start_p0.sh   （启动 shell 里必须 export VLLM_TTFT_SHM_LOG=1；Ray 场景另需 RAY_DEDUP_LOGS=0）
  4. D 侧 start_d0.sh   （同样 export VLLM_TTFT_SHM_LOG=1）
  5. 102 宿主机 start_proxy.sh
  6. smoke：发 1 条请求，确认 [TTFT][BREAKDOWN] v3 行 / [TTFT_DRAFT] / [SHM_DEQUEUE_BATCH] 有输出、
     端口监听正常、日志出现 Application startup complete
回滚：按备份目录下 MANIFEST.txt 的原路径映射，把对应文件放回原路径
      （容器侧 docker cp 回容器原路径；宿主机侧 cp 回原路径）
EOF
log "备份目录: $BAK  原路径映射: $MANIFEST（请妥善保留，回滚依据）"
