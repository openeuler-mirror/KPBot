#!/usr/bin/env bash
set -uo pipefail

cd /workspace
mkdir -p build results

# ---- 完整性自校验（fail-closed）：pristine 源被篡改即拒绝跑分 ----
# 清单构建期生成（镜像层），覆盖 bench/main.rs、src/kernel.rs、verify.sh、entrypoint.sh
if [ -f .pristine.sha256 ]; then
    if ! sha256sum -c .pristine.sha256 > results/integrity.log 2>&1; then
        SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"integrity check failed: pristine sources modified","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
        exit 1
    fi
else
    echo "警告: .pristine.sha256 缺失（非容器部署），跳过完整性校验" >&2
fi

SKILL_MODE="${SKILL_MODE:-with}"
N=${N:-4000000}
ITERS=${ITERS:-20}
VOCAB=${VOCAB:-1024}

# ---- baseline ----
mkdir -p build/base
cp bench/main.rs build/base/main.rs
cp src/kernel.rs build/base/kernel.rs
rustc -O -C lto -C codegen-units=1 -C overflow-checks=on -C debug-assertions=on --edition 2021 -o build/baseline build/base/main.rs 2>build/base_compile.log || {
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"baseline compile failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
}

# ---- optimized ----
if [ ! -f opt/kernel_opt.rs ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/kernel_opt.rs not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# 安全 lint：LLM 产物仅允许纯计算。deny 进程/FFI/内联汇编/网络/文件系统。
# 注意：std::arch::aarch64 的 NEON/SVE intrinsic 是合法路径（skill 推荐它们），
# intrinsic 经普通函数调用不经过 asm! 宏，deny asm! 不影响 intrinsic 产物。
if grep -nEq 'std::process|extern[[:space:]]+"C"|asm!|std::net|std::fs|Command::new|std::ptr::' opt/kernel_opt.rs; then
    echo "安全检查失败: opt/kernel_opt.rs 含禁止调用 (进程/FFI/内联汇编/网络/文件系统/裸指针)" >&2
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"security lint failed: forbidden call in kernel_opt.rs","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

mkdir -p build/opt
cp bench/main.rs build/opt/main.rs
sed -i 's/^mod kernel;$/mod kernel_opt;/' build/opt/main.rs
sed -i 's/kernel::/kernel_opt::/g' build/opt/main.rs
cp opt/kernel_opt.rs build/opt/kernel_opt.rs
if ! rustc -O -C lto -C codegen-units=1 -C overflow-checks=on -C debug-assertions=on --edition 2021 -o build/optimized build/opt/main.rs 2>build/opt_compile.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"optimized compile failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 跑分（bench 第 3 个 argv 是 VOCAB）----
./build/baseline "$N" "$ITERS" "$VOCAB" > results/baseline.out
base_rc=$?
./build/optimized "$N" "$ITERS" "$VOCAB" > results/optimized.out
opt_rc=$?

export SKILL_MODE="$SKILL_MODE"
export BASE_TIME="$(awk -F= '/^TIME_MS=/{print $2}' results/baseline.out)"
export OPT_TIME="$(awk -F= '/^TIME_MS=/{print $2}' results/optimized.out)"
export OPT_OK="$(awk -F= '/^CORRECT=/{print $2}' results/optimized.out)"
export REL_ERR="$(awk -F= '/^REL_ERR=/{print $2}' results/optimized.out)"
export OPT_RC="$opt_rc"

python3 - <<'PY'
import json, os
base_time = float(os.environ["BASE_TIME"] or 0)
opt_time = float(os.environ["OPT_TIME"] or 0)
speedup = round(base_time / opt_time, 4) if opt_time > 0 else 0.0
opt_ok = os.environ["OPT_OK"]
success = (os.environ["OPT_RC"] == "0" and opt_ok == "1")
json.dump({
    "success": success,
    "reason": "ok" if success else "correctness_or_run_failed",
    "skill_mode": os.environ["SKILL_MODE"],
    "optimized_correct": int(opt_ok),
    "rel_err": float(os.environ["REL_ERR"] or 0),
    "baseline_time_ms": base_time,
    "optimized_time_ms": opt_time,
    "speedup": speedup,
}, open("results/result.json", "w"), indent=2)
PY

if [ "${opt_rc}" -eq 0 ] && [ "${OPT_OK:-}" = "1" ]; then
    exit 0
else
    exit 1
fi
