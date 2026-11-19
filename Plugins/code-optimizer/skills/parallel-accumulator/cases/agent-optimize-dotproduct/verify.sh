#!/usr/bin/env bash
set -uo pipefail

cd /workspace
mkdir -p build results
rm -f results/result.json

# ---- 完整性自校验（fail-closed）：pristine 源被篡改即拒绝运行 ----
if [ -f .pristine.sha256 ]; then
    if ! sha256sum -c .pristine.sha256 > results/integrity.log 2>&1; then
        SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"integrity check failed: pristine sources modified","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
        exit 1
    fi
else
    echo "警告: .pristine.sha256 缺失（非容器部署），跳过完整性校验" >&2
fi

SKILL_MODE="${SKILL_MODE:-with}"
N=${N:-1000000}
ITERS=${ITERS:-20}

# 两臂编译 flag 必须一致（A/B 有效性铁律：唯一允许的差异是 SKILL_MODE）
COMMON_FLAGS=(-O2 -std=c11 -ftrapv -fstack-protector-strong)

# ---- baseline ----
if ! gcc "${COMMON_FLAGS[@]}" -Isrc bench/main.c src/kernel.c -o build/baseline -lm 2>build/base_compile.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"baseline compile failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- optimized ----
if [ ! -f opt/kernel_opt.c ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/kernel_opt.c not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# 安全 lint：LLM 产物仅允许纯计算。deny 进程/网络/文件系统/内联汇编/动态加载，
# 以及所有输出与退出控制（printf/puts/fputs/fwrite/exit/_exit/abort/atexit）——
# 纯计算 kernel 不需要输出与退出控制，被测产物不得自报 CORRECT/TIME_MS 或提前退出。
if grep -nEq 'system[[:space:]]*\(|popen|exec[lv]|fork|socket|connect|send|recv|open[[:space:]]*\(|fopen|read[[:space:]]*\(|write[[:space:]]*\(|mmap|unlink|creat|__asm|asm[[:space:]]*\(|dlopen|dlsym|printf|puts|fputs|fwrite|exit[[:space:]]*\(|abort|atexit' opt/kernel_opt.c; then
    echo "安全检查失败: opt/kernel_opt.c 含禁止调用（进程/网络/文件/内联汇编/输出/退出控制）" >&2
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"security lint failed: forbidden call in kernel_opt.c","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

if ! gcc "${COMMON_FLAGS[@]}" -Isrc bench/main.c opt/kernel_opt.c -o build/optimized -lm 2>build/opt_compile.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"optimized compile failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 跑分：计时由 verify.sh 受信方外部墙钟测量（date +%s%N），不采信产物自报 TIME_MS ----
start=$(date +%s%N)
./build/baseline "$N" "$ITERS" > results/baseline.out
base_rc=$?
end=$(date +%s%N)
BASE_TIME_NS=$((end - start))

start=$(date +%s%N)
./build/optimized "$N" "$ITERS" > results/optimized.out
opt_rc=$?
end=$(date +%s%N)
OPT_TIME_NS=$((end - start))

# ---- 结论严格校验：CORRECT/MAX_REL_ERR/SINK 三行缺一即 fail-closed（防提前 exit 造假）----
OPT_OK="$(awk -F= '/^CORRECT=/{print $2}' results/optimized.out)"
MAX_REL_ERR="$(awk -F= '/^MAX_REL_ERR=/{print $2}' results/optimized.out)"
OPT_SINK="$(awk -F= '/^SINK=/{print $2}' results/optimized.out)"
if [ -z "${OPT_OK}" ] || [ -z "${MAX_REL_ERR}" ] || [ -z "${OPT_SINK}" ]; then
    echo "结论不完整: CORRECT/MAX_REL_ERR/SINK 缺失（被测产物可能提前退出）" >&2
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"incomplete conclusion output","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

export SKILL_MODE="$SKILL_MODE"
export BASE_TIME_NS="$BASE_TIME_NS"
export OPT_TIME_NS="$OPT_TIME_NS"
export OPT_OK="$OPT_OK"
export MAX_REL_ERR="$MAX_REL_ERR"
export OPT_RC="$opt_rc"

python3 - <<'PY'
import json, os
base_ms = float(os.environ["BASE_TIME_NS"]) / 1e6
opt_ms = float(os.environ["OPT_TIME_NS"]) / 1e6
speedup = round(base_ms / opt_ms, 4) if opt_ms > 0 else 0.0
opt_ok = os.environ["OPT_OK"]
success = (os.environ["OPT_RC"] == "0" and opt_ok == "1")
json.dump({
    "success": success,
    "reason": "ok" if success else "correctness_or_run_failed",
    "skill_mode": os.environ["SKILL_MODE"],
    "optimized_correct": int(opt_ok),
    "max_rel_err": float(os.environ["MAX_REL_ERR"] or 0),
    "baseline_time_ms": base_ms,
    "optimized_time_ms": opt_ms,
    "speedup": speedup,
}, open("results/result.json", "w"), indent=2)
PY

if [ "${opt_rc}" -eq 0 ] && [ "${OPT_OK:-}" = "1" ]; then
    exit 0
else
    exit 1
fi