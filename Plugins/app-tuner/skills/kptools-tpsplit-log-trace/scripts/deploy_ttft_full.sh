#!/usr/bin/env bash
# deploy_ttft_full.sh — TTFT breakdown 打点 patch 一键部署（known-good 2026-11-18 v3.2）
#
# 用法: bash scripts/deploy_ttft_full.sh <CONTAINER> [SRC] [KVXFER]
#   CONTAINER  目标容器名（P 或 D 侧 vllm 引擎容器；stopped 状态也可部署）
#   SRC        patch 包目录，默认 /data/ttft/patched_src（需先 scp 上传到宿主机）
#   KVXFER     SP ascend_store patch 目录，默认 $SRC/ascend_store（可选）
# 可选环境变量:
#   PROXY_TGT  proxy 文件目标路径（宿主机进程），设置后连 proxy patch 一起推
#   BENCH_TGT  压测端目录（宿主机进程），设置后连 bench patch 一起推
#
# 安全红线（对应 skill 踩坑清单）:
#   1. 备份先行；基座指纹不一致 = 基座被改过，立即退出，禁止强推
#   2. 只用 docker cp 覆盖（stopped 容器 docker exec 无效）；docker cp 双向均可用
#   3. 每个文件部署后逐字节 md5 比对；清单内文件 md5 必须与已验证版本一致
#   4. 不自动重启引擎/proxy——重启顺序由用户手动执行（见脚本尾部提示）

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
    "vllm/v1/engine/outputs.py"
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

# 版本指纹（known-good v3.2，md5 前 8 位）——部署后必须逐字节一致
declare -A PATCH_FP=(
    ["vllm_ascend/worker/model_runner_v1.py"]="2e7db5f3"
    ["vllm/distributed/device_communicators/shm_broadcast.py"]="55821b06"
    ["vllm/v1/worker/gpu_model_runner.py"]="0a59cde4"
    ["vllm/v1/metrics/stats.py"]="9f9f5276"
    ["vllm/v1/core/sched/output.py"]="c6d9b049"
)
# 基座指纹（patch 未触碰文件）——不符 = 基座不同，停下确认
declare -A BASE_FP=(
    ["detokenizer.py"]="3fe8bf88"
    ["kv_cache_manager.py"]="d1a6c67c"
    ["version.py"]="2b4efcd3"
)
# 部署后 grep 校验关键字段（容器内现役文件中应 >0）
GREP_KEYS=(frontend_ts schedule_total_time queue_wait step_other add_req_iq \
           TTFT_DECODE TTFT_KVXFER start_load_kv TTFT_DRAFT _ttft_in_draft _TTFT_SHM_DQ)

WORK="$(mktemp -d /tmp/ttft_deploy.XXXXXX)"
BAK="/tmp/ttft_bak.$(date +%s)"
VERIFY="$WORK/verify"
mkdir -p "$VERIFY"

log()  { echo "[deploy] $*"; }
die()  { echo "[FATAL] $*"; echo "[deploy] 备份目录: $BAK（未做任何覆盖或已部分覆盖，可从此回滚）"; exit 1; }

# 容器内文件 -> 宿主机 md5（stopped 容器：docker cp 拉出再算；docker cp 失败回退 exec）
container_md5() {
    local cpath="$1" t="$WORK/cur_$(basename "$cpath")"
    if docker cp "$CONTAINER:$cpath" "$t" >/dev/null 2>&1; then
        md5sum "$t" | awk '{print $1}'
    elif docker exec "$CONTAINER" md5sum "$cpath" >/dev/null 2>&1; then
        docker exec "$CONTAINER" md5sum "$cpath" | awk '{print $1}'
    else
        echo ""
    fi
}

log "目标容器: $CONTAINER  SRC: $SRC  KVXFER: $KVXFER"
log "工作目录: $WORK   备份目录: $BAK"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "容器不存在: $CONTAINER"
[ -d "$SRC" ] || die "SRC 不存在: $SRC（先 scp -r patched_src root@宿主:/data/<user>/）"

# ---------- 3. 备份 + 基座核对（必须；不一致即退出） ----------
log "== 步骤 1/6: 备份现役文件 + 基座指纹核对 =="
mkdir -p "$BAK/vllm" "$BAK/sp" "$BAK/kvxfer"
for f in "${VLLM_FILES[@]}"; do
    if docker cp "$CONTAINER:$VLLM_ROOT/$f" "$BAK/vllm/$(basename "$f")" >/dev/null 2>&1; then
        log "  已备份 $f"
    else
        log "  [WARN] 容器内不存在 $f（新装环境/全新文件）"
    fi
done

# 基座指纹：patch 未触碰的文件，md5 不符 = 基座不同 -> 停下问用户
declare -A BASE_PATHS=(
    ["detokenizer.py"]="$VLLM_ROOT/vllm/v1/engine/detokenizer.py"
    ["kv_cache_manager.py"]="$VLLM_ROOT/vllm/v1/core/kv_cache_manager.py"
    ["version.py"]="$VLLM_ROOT/vllm/version.py"
)
for name in "${!BASE_FP[@]}"; do
    cur="$(container_md5 "${BASE_PATHS[$name]}")"
    want="${BASE_FP[$name]}"
    if [ -z "$cur" ]; then
        log "  [WARN] 基座文件 $name 取不到 md5（路径可能变化），跳过该项核对"
        continue
    fi
    if [[ "$cur" != "$want"* ]]; then
        die "基座指纹不符: $name 现值=${cur:0:8} 期望=$want —— 基座与已验证环境不同，禁止强推，请人工确认"
    fi
    log "  基座 OK: $name=${cur:0:8}"
done

# 现役 vs 目标 md5 提示（v3.2 指纹命中 = 该文件已是 known-good，可跳过）
log "== 步骤 2/6: 现役 vs 目标 md5 =="
SKIP_LIST=()
for f in "${VLLM_FILES[@]}"; do
    tgt_md5="$(md5sum "$SRC/$f" 2>/dev/null | awk '{print $1}')"
    cur_md5="$(container_md5 "$VLLM_ROOT/$f")"
    if [ "$tgt_md5" = "$cur_md5" ]; then
        log "  $f: 已与目标一致，跳过"
        SKIP_LIST+=("$f")
    elif [ -n "${PATCH_FP[$f]:-}" ] && [[ "$cur_md5" = "${PATCH_FP[$f]}"* ]]; then
        log "  $f: 现役为旧版打点(md5=${cur_md5:0:8})，将升级到目标版本"
    else
        log "  $f: 现役(${cur_md5:0:8:-NONE}) != 目标(${tgt_md5:0:8})，待覆盖（已备份）"
    fi
done

# ---------- 4. 定位 SP 路径（find；SP 实际位置可能与默认不同） ----------
log "== 步骤 3/6: 定位 site-packages 路径 =="
# 容器在跑优先 exec find；stopped 则用默认 SP 并用 docker cp 探测验证
SP_ROOT=""
if docker exec "$CONTAINER" true >/dev/null 2>&1; then
    SP_ROOT="$(docker exec "$CONTAINER" find / -maxdepth 8 -path '*site-packages/vllm_ascend/worker/model_runner_v1.py' 2>/dev/null \
               | sed 's#vllm_ascend/worker/model_runner_v1.py$##' | head -n1)"
fi
if [ -z "$SP_ROOT" ]; then
    # stopped 容器：按默认 SP 探测
    if docker cp "$CONTAINER:$SP_EXPECT/vllm_ascend/worker/model_runner_v1.py" "$WORK/probe_mr1" >/dev/null 2>&1; then
        SP_ROOT="$SP_EXPECT/"
    else
        die "无法定位 vllm_ascend SP 路径（容器未运行且默认 $SP_EXPECT 探测失败）。请启动容器后重试，或手工 find:\n  find / -path '*site-packages/vllm_ascend/core/recompute_scheduler.py'"
    fi
fi
log "  SP_ROOT = $SP_ROOT"

KVXFER_DST=""
if docker exec "$CONTAINER" true >/dev/null 2>&1; then
    KVXFER_DST="$(docker exec "$CONTAINER" find / -maxdepth 9 -type d -path '*vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store' 2>/dev/null | head -n1)"
fi
if [ -z "$KVXFER_DST" ]; then
    KVXFER_DST="${SP_ROOT}vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store"
    docker cp "$CONTAINER:$KVXFER_DST/kv_transfer.py" "$WORK/probe_kv" >/dev/null 2>&1 \
        || log "  [WARN] 默认 ascend_store 路径探测失败，仍按默认继续（若失败请手工 find 定位）"
fi
log "  KVXFER_DST = $KVXFER_DST"

# ---------- 5. 覆盖（docker cp；stopped 容器同样有效） ----------
log "== 步骤 4/6: docker cp 覆盖（16 文件） =="
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
    if [[ " ${SKIP_LIST[*]-} " == *" $f "* ]]; then continue; fi
    deploy_one "$SRC/$f" "$VLLM_ROOT/$f" "$f"
done
for f in "${SP_FILES[@]}"; do
    deploy_one "$SRC/$f" "${SP_ROOT}${f}" "$f"
done
for f in "${KVXFER_FILES[@]}"; do
    [ -f "$KVXFER/$f" ] && deploy_one "$KVXFER/$f" "$KVXFER_DST/$f" "kvxfer/$f" \
        || log "  [WARN] KVXFER 包无 $f，跳过"
done

# proxy / 压测端（宿主机进程，可选）
if [ -n "${PROXY_TGT:-}" ]; then
    cp -f "$SRC/proxy/load_balance_proxy_server_example.py" "$PROXY_TGT" \
        && log "  proxy patch -> $PROXY_TGT（记得重启宿主机 proxy 进程）"
fi
if [ -n "${BENCH_TGT:-}" ]; then
    cp -f "$SRC/utils/bench_serving/backend_request_func.py" "$BENCH_TGT/" \
        && cp -f "$SRC/utils/bench_serving/benchmark_serving.py" "$BENCH_TGT/" \
        && log "  bench patch -> $BENCH_TGT"
fi

# ---------- 6. 校验：grep 关键字段 + py_compile ----------
log "== 步骤 5/6: grep 关键字段（应全部 >0） =="
FAIL=0
for k in "${GREP_KEYS[@]}"; do
    n=$(grep -r -c -- "$k" "$VERIFY" 2>/dev/null | awk -F: '{s+=$NF} END{print s+0}')
    if [ "$n" -gt 0 ]; then
        log "  OK  $k ($n)"
    else
        echo "[FATAL] 关键字段 $k 在部署文件中 grep=0"
        FAIL=1
    fi
done
[ "$FAIL" -eq 0 ] || die "grep 校验未通过（见上方缺失字段）"

log "== 步骤 6/6: py_compile 语法检查 =="
# py_compile 只做语法解析（不执行 import），宿主机 python3 即可；stopped 容器无需 exec
PYC_ERR=0
for f in "${VLLM_FILES[@]}" "${SP_FILES[@]}"; do
    lf="$SRC/$f"
    if ! python3 -m py_compile "$lf" >/dev/null 2>&1; then
        echo "[FATAL] py_compile 失败: $lf"
        PYC_ERR=1
    fi
done
for f in "${KVXFER_FILES[@]}"; do
    lf="$KVXFER/$f"
    [ -f "$lf" ] || continue
    python3 -m py_compile "$lf" >/dev/null 2>&1 || { echo "[FATAL] py_compile 失败: $lf"; PYC_ERR=1; }
done
[ "$PYC_ERR" -eq 0 ] || die "py_compile 未通过"
log "  py_compile 全部通过"

# ---------- 7. 收尾：不自动重启 ----------
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
回滚：cp $BAK 下对应文件回去（vllm 侧 docker cp 回 $VLLM_ROOT，SP 侧回 SP 路径）
EOF
log "备份目录: $BAK （请妥善保留，回滚依据）"
