#!/bin/bash
# run.sh — tier A：compiler-optimization BRBE/CSSPGO 契约用例
# 覆盖：brbe_env_check.sh（workdir 注入白名单、mock 全绿 ready、缺 NPU blocked + next_steps、JSON 契约）
#       brbe_ab_benchmark.sh（run 统计/提取失败/label 白名单、compare 四态判定）
# 运行（技能根目录）: bash cases/brbe-contract/run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d /tmp/brbe-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_fail()    { if [ "$1" -ne 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (期望非零)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }
jget() { python3 -c "import json;d=json.loads('''$1''');print($2)" 2>/dev/null; }

# —— mock bin：全绿 ready 场景（uname/lscpu/ls/clang/llvm-profgen/npu-smi/perf）——
MOCK="$WORK/bin"; mkdir -p "$MOCK"
printf '#!/bin/bash\necho aarch64\n' > "$MOCK/uname"
printf '#!/bin/bash\necho "Vendor ID: HiSilicon"\n' > "$MOCK/lscpu"
printf '#!/bin/bash\ncase "$*" in\n  /dev/davinci*) echo "/dev/davinci0"; exit 0;;\n  "-d /usr/local/Ascend"*) echo "/usr/local/Ascend"; exit 0;;\n  *) /usr/bin/ls "$@";;\nesac\n' > "$MOCK/ls"
printf '#!/bin/bash\necho "clang version 4.0.0 (bisheng)"\n' > "$MOCK/clang"
printf '#!/bin/bash\necho "/mock/bin/llvm-profgen"\n' > "$MOCK/llvm-profgen"
printf '#!/bin/bash\necho "/mock/bin/npu-smi"\n' > "$MOCK/npu-smi"
printf '#!/bin/bash\ncase "$1" in\n  record) exit 0;;\n  script) for i in $(seq 1 25); do echo "0x1a2b3c/0x4d5e6f/COND/0/"; done; exit 0;;\n  *) echo "/mock/bin/perf"; exit 0;;\nesac\n' > "$MOCK/perf"
printf '#!/bin/bash\necho mock-host\n' > "$MOCK/hostname"
# df mock：磁盘检查（hard 项）与宿主真实磁盘解耦，固定 1024G 可用
printf '#!/bin/bash\necho "Avail"\necho " 1024G"\n' > "$MOCK/df"
chmod +x "$MOCK"/*

echo "===== BRBE/CSSPGO 契约用例 ====="

echo "BE-01: brbe_env_check --workdir 注入白名单"
PATH="$MOCK:$PATH" bash "$SCRIPT_DIR/brbe_env_check.sh" --workdir "/tmp;rm -rf /" >/dev/null 2>&1
assert_fail $? "BE-01: 含元字符 workdir → exit 1"

echo "BE-02: mock 全绿 → ready + JSON 契约"
OUT=$(PATH="$MOCK:$PATH" bash "$SCRIPT_DIR/brbe_env_check.sh" --json-only 2>/dev/null); RC=$?
assert_success $RC "BE-02: rc=0"
assert_eq "$(jget "$OUT" "d['status']")" "ready" "BE-02: status=ready"
assert_eq "$(jget "$OUT" "d['checks']['cpu_arch']")" "pass" "BE-02: cpu_arch=pass（HiSilicon 判据）"
assert_eq "$(jget "$OUT" "d['checks']['npu']")" "pass" "BE-02: npu=pass（davinci 判据，非 lspci）"
assert_eq "$(jget "$OUT" "d['checks']['brbe_hw']")" "pass" "BE-02: brbe_hw=pass（真实分支对 ≥20）"
assert_eq "$(jget "$OUT" "len(d['blocked_reasons'])")" "0" "BE-02: blocked_reasons 空"

echo "BE-03: 无 NPU → blocked + next_steps"
MOCK2="$WORK/bin2"; mkdir -p "$MOCK2"
# no-NPU 场景须与宿主无关（PR#194 检视 + mxj1314 复核 #1）：宿主 /usr/bin、/bin 不得进 PATH——
# 宿主即便把 npu-smi 装在 /usr/bin 也会被 command -v 命中翻转场景；ls 拦截 davinci/Ascend，
# npu-smi 不放（command -v 找不到），clang/llvm-profgen 放但 exit 1，df 固定 1024G；
# KPBOT_SOURCE_CANN=0 关闭 CANN set_env 深度探测（真 NPU 宿主上绝对路径 source 不可 mock）；
# 脚本运行所需 coreutils 以受控符号链接白名单提供（与 DB-03 的 STUB 同法）
cp "$MOCK/uname" "$MOCK/lscpu" "$MOCK2/"
printf '#!/bin/bash\ncase "$*" in\n  /dev/davinci*) exit 0;;\n  "-d /usr/local/Ascend"*) exit 0;;\n  *) /usr/bin/ls "$@";;\nesac\n' > "$MOCK2/ls"
for t in clang llvm-profgen; do printf '#!/bin/bash\nexit 1\n' > "$MOCK2/$t"; done
printf '#!/bin/bash\necho "Filesystem 1G-blocks Used Available Use%% Mounted on\n/dev/mock 1024 100 924 10%% /tmp"\n' > "$MOCK2/df"
chmod +x "$MOCK2"/*
STUB2="$WORK/stub2-bin"; mkdir -p "$STUB2"
for t in bash grep awk sed cat hostname date head tail tr sort wc python3 readelf; do
    ln -sf "$(command -v $t 2>/dev/null)" "$STUB2/$t" 2>/dev/null
done
OUT=$(PATH="$MOCK2:$STUB2" ASCEND_HOME_PATH="" KPBOT_SOURCE_CANN=0 bash "$SCRIPT_DIR/brbe_env_check.sh" --json-only 2>/dev/null); RC=$?
assert_fail $RC "BE-03: rc=1"
assert_eq "$(jget "$OUT" "d['status']")" "blocked" "BE-03: status=blocked"
assert_contains "$(jget "$OUT" "str(d['blocked_reasons'])")" "npu" "BE-03: blocked_reasons 含 npu"
assert_contains "$(jget "$OUT" "str(d['next_steps'])")" "鲲鹏+昇腾" "BE-03: next_steps 提示换节点"

echo "BE-04: --container 模式——KPBOT_SOURCE_CANN 显式传入 docker exec（锁 mxj1314 检视 #1）"
export DOCKER_ARGS_STATE="$WORK/docker.args"; : > "$DOCKER_ARGS_STATE"
cat > "$MOCK/docker" <<'EOF'
#!/bin/bash
# docker mock：记录完整调用参数；按探测命令关键字返回 canned 输出（末参 = bash -c 的命令串）。
# 注意：容器模式下整个命令串（含管道）都在"容器内"执行，mock 必须模拟管道的最终结果，
# 而非管道首命令的原始输出（df 分支给 1024 而非 "Avail/1024G" 两行；perf script 给计数 25）。
printf '%s\n' "$*" >> "$DOCKER_ARGS_STATE"
cmd="${@: -1}"
case "$cmd" in
  *"uname -m"*) echo "aarch64";;
  *"lscpu"*) echo "Vendor ID: HiSilicon";;
  *"command -v npu-smi"*) ;;                        # 容器内 npu-smi 不可见（深度探测空手而归）
  *"ls /dev/davinci"*) ;;                           # 容器内无设备节点
  *"ls -d /usr/local/Ascend"*) ;;                   # 容器内无 CANN
  *"clang --version"*) echo "clang version 4.0.0 (bisheng)";;
  *"command -v llvm-profgen"*) echo "/mock/bin/llvm-profgen";;
  *"command -v perf"*) echo "/mock/bin/perf";;
  *"perf script"*) echo "25";;                      # brbe 探测串的最终结果（grep -c 计数）
  *"perf record"*) exit 0;;
  *"perf_event_paranoid"*) echo "1";;
  *"df"*) echo "1024";;                             # df|tail|tr 管道的最终结果（纯数字 GB）
  *"python3"*) ;;                                  # import torch 失败 → warn（soft）
  *) ;;
esac
exit 0
EOF
chmod +x "$MOCK/docker"
OUT=$(PATH="$MOCK:$PATH" KPBOT_SOURCE_CANN=1 bash "$SCRIPT_DIR/brbe_env_check.sh" --container mock-cont --json-only 2>/dev/null); RC=$?
assert_fail $RC "BE-04: 容器内无 NPU/CANN → rc=1"
assert_eq "$(jget "$OUT" "d['status']")" "blocked" "BE-04: status=blocked"
assert_contains "$(jget "$OUT" "str(d['blocked_reasons'])")" "npu" "BE-04: blocked_reasons 含 npu（深度探测在容器侧执行且空手而归）"
assert_contains "$(cat "$DOCKER_ARGS_STATE")" "-e KPBOT_SOURCE_CANN=1" "BE-04: 宿主开关以 -e 显式传入容器探测命令"
: > "$DOCKER_ARGS_STATE"
OUT=$(PATH="$MOCK:$PATH" bash "$SCRIPT_DIR/brbe_env_check.sh" --container mock-cont --json-only 2>/dev/null); RC=$?
assert_contains "$(cat "$DOCKER_ARGS_STATE")" "-e KPBOT_SOURCE_CANN=0" "BE-04: 未设置开关时以默认 0 传入（容器内不误启深度探测）"

echo "AB-01: run 模式统计——固定值 3 轮"
OUT=$(bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode run --label base --cmd "echo 'tok/s: 100.5'" \
      --rounds 3 --warmup 1 --out-dir "$WORK/ab" 2>/dev/null); RC=$?
assert_success $RC "AB-01: rc=0"
assert_eq "$(python3 -c "import json;print(json.load(open('$WORK/ab/base.json'))['mean'])")" "100.5" "AB-01: mean=100.5"
assert_eq "$(python3 -c "import json;print(len(json.load(open('$WORK/ab/base.json'))['values']))")" "3" "AB-01: values=3 轮"
assert_eq "$(python3 -c "import json;print(json.load(open('$WORK/ab/base.json'))['cv_pct'])")" "0.0" "AB-01: cv=0（无抖动）"

echo "AB-02: run 提取失败 → exit 1"
bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode run --label bad --cmd "echo 'nothing'" \
     --rounds 2 --out-dir "$WORK/ab" >/dev/null 2>&1
assert_fail $? "AB-02: 指标未提取 → exit 1"

echo "AB-03: label 路径逃逸白名单"
bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode run --label "../evil" --cmd "true" >/dev/null 2>&1
assert_fail $? "AB-03: label 含 / → exit 1"

echo "AB-04: compare 四态判定"
mkj() { python3 -c "import json,sys;json.dump({'label':sys.argv[1],'mean':float(sys.argv[2]),'cv_pct':float(sys.argv[3]),'values':[1.0]},open(sys.argv[4],'w'))" "$1" "$2" "$3" "$4"; }
mkj base 100 1.0 "$WORK/ab/base.json"; mkj cand 105 1.0 "$WORK/ab/cand.json"
OUT=$(bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode compare --baseline "$WORK/ab/base.json" --candidate "$WORK/ab/cand.json" --min-gain 1.0 --out-dir "$WORK/ab" 2>/dev/null)
assert_contains "$OUT" "verdict=accept" "AB-04a: gain 5% > noise → accept"
mkj base 100 5.0 "$WORK/ab/base.json"; mkj cand 100 5.0 "$WORK/ab/cand.json"
OUT=$(bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode compare --baseline "$WORK/ab/base.json" --candidate "$WORK/ab/cand.json" --out-dir "$WORK/ab" 2>/dev/null)
assert_contains "$OUT" "verdict=reject" "AB-04b: 持平且噪声大 → reject(gain<=noise)"
mkj base 100 0.2 "$WORK/ab/base.json"; mkj cand 100.5 0.2 "$WORK/ab/cand.json"
OUT=$(bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode compare --baseline "$WORK/ab/base.json" --candidate "$WORK/ab/cand.json" --out-dir "$WORK/ab" 2>/dev/null)
assert_contains "$OUT" "below_min_gain" "AB-04c: 增益低于门槛 → reject(below_min_gain)"
mkj base 100 0.2 "$WORK/ab/base.json"; mkj cand 90 0.2 "$WORK/ab/cand.json"
OUT=$(bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode compare --baseline "$WORK/ab/base.json" --candidate "$WORK/ab/cand.json" --out-dir "$WORK/ab" 2>/dev/null)
assert_contains "$OUT" "verdict=regression" "AB-04d: 退化 → regression"

echo "AB-04e: 噪声公式回归——sqrt(a²+b²) vs 线性 a+b"
mkj base2 100 3.0 "$WORK/ab/base2.json"; mkj cand2 106 4.0 "$WORK/ab/cand2.json"
OUT=$(bash "$SCRIPT_DIR/brbe_ab_benchmark.sh" --mode compare --baseline "$WORK/ab/base2.json" --candidate "$WORK/ab/cand2.json" --min-gain 1.0 --out-dir "$WORK/ab" 2>/dev/null)
assert_contains "$OUT" "verdict=accept" "AB-04e: gain 6% > sqrt(9+16)=5 → accept（sqrt 公式回归看护）"

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

mkdir -p "$CASE_DIR/results"
python3 - "$CASE_DIR/results/result.json" "$TEST_PASSED" "$TEST_FAILED" "$TEST_SKIPPED" <<'RESULTPY'
import json, sys
p, passed, failed, skipped = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
json.dump({"success": failed == 0, "passed": passed, "checks_ran": passed + failed,
           "checks_failures": failed, "skipped": skipped},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
sys.exit(0 if failed == 0 else 1)
RESULTPY
exit $?
