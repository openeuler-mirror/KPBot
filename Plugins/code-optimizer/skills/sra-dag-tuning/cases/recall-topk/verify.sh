#!/usr/bin/env bash
# verify.sh — recall-topk 验证脚本：编译 baseline、运行、输出 QPS/RT/checksum
# 用法: ./verify.sh [variant.cpp ...]
#   无参数: 只跑 src/dag_baseline.cpp
#   传入变体文件(如 optimized.cpp): 与 baseline A/B 对照(交替多轮取中位数)
# 目标平台: 鲲鹏 aarch64；本机 x86 上用于逻辑验证
set -euo pipefail
cd "$(dirname "$0")"
BUILD=build; mkdir -p "$BUILD"
CXX=${CXX:-g++}
FLAGS="-O2 -g -fno-omit-frame-pointer -pthread"

run_one() {  # $1=cpp  -> 输出 "checksum=... qps=... rt_us=..."
    local cpp=$1 bin out
    bin="$BUILD/$(basename "${cpp%.cpp}")"
    $CXX $FLAGS "$cpp" -o "$bin"
    out=$("$bin") || { echo "RUN FAILED: $cpp"; echo "$out"; return 1; }
    echo "$out"
}

median() { sort -n | awk '{a[NR]=$1} END{print a[int((NR+1)/2)]}'; }

echo "=== baseline: src/dag_baseline.cpp ==="
BASE_OUT=$(run_one src/dag_baseline.cpp)
echo "$BASE_OUT"
BASE_CK=$(echo "$BASE_OUT" | grep -oP 'checksum=\K[0-9]+')
echo "$BASE_OUT" | grep -q 'deterministic=yes' || { echo "baseline 结果不确定，检查代码"; exit 2; }

if [ $# -eq 0 ]; then
    echo
    echo "baseline 验证通过。可用 './verify.sh src/<agent产出>.cpp' 做 A/B 对照。"
    exit 0
fi

# A/B 交替压测（同机连续单跑波动可达±40%，交替取中位数）
echo
echo "=== A/B 交替压测 (5轮取中位数) ==="
: > /tmp/verify_ab_base.txt; : > /tmp/verify_ab_var.txt
for i in 1 2 3 4 5; do
    b=$(run_one src/dag_baseline.cpp | grep -oP 'qps=\K[0-9.]+')
    v=$(run_one "$1" | grep -oP 'qps=\K[0-9.]+')
    echo "$b" >> /tmp/verify_ab_base.txt
    echo "$v" >> /tmp/verify_ab_var.txt
    echo "round $i: baseline=${b} variant=${v}"
done
BM=$(median < /tmp/verify_ab_base.txt); VM=$(median < /tmp/verify_ab_var.txt)
VCK=$(run_one "$1" | grep -oP 'checksum=\K[0-9]+')
if [ "$VCK" != "$BASE_CK" ]; then
    echo "FAIL: checksum 不一致 (base=$BASE_CK variant=$VCK)，优化改变了语义！"; exit 1
fi
echo
echo "median QPS: baseline=$BM variant=$VM"
awk -v b="$BM" -v v="$VM" 'BEGIN{printf "收益: %+.1f%%\n", (v/b-1)*100}'
