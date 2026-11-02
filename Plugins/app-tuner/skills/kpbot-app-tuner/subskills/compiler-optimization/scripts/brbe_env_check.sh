#!/usr/bin/env bash
set -uo pipefail

# brbe_env_check.sh — BRBE/CSSPGO 收益验证前置门控（只读，不修改环境）
# 用法: brbe_env_check.sh [--container <name>] [--workdir <dir>] [--json-only]
# 判定: hard 项全部 PASS → ready；任一 FAIL → blocked（附 blocked_reasons 与 next_steps）
# 依据: compiler-optimization/references/csspgo-brbe-playbook.md 决策矩阵与关键坑
# 定位: 在鲲鹏+昇腾目标节点上，跑 A/B 收益验证（brbe_ab_benchmark.sh）之前先跑本脚本。

CONTAINER=""
WORKDIR="/tmp"
JSON_ONLY=false

usage() {
  cat <<'EOF'
Usage: brbe_env_check.sh [--container <name>] [--workdir <dir>] [--json-only]
  --container <name>  在指定容器内执行检查（docker exec）
  --workdir <dir>     磁盘空间检查目录（默认 /tmp，gen/use 产物落盘处）
  --json-only         只输出 JSON 总结
  -h, --help          显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --workdir) WORKDIR="${2:?}"; shift 2 ;;
    --json-only) JSON_ONLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage >&2; exit 1 ;;
  esac
done

run_t() {
  if [[ -n "${CONTAINER}" ]]; then
    docker exec "${CONTAINER}" bash -c "$1" 2>/dev/null
  else
    bash -c "$1" 2>/dev/null
  fi
}

declare -a CHECK_NAMES=()
declare -a CHECK_STATUS=()
declare -a CHECK_DETAIL=()
declare -a BLOCKED=()

add_check() { # <name> <pass|fail|warn|skip> <detail> <hard:0|1>
  CHECK_NAMES+=("$1"); CHECK_STATUS+=("$2"); CHECK_DETAIL+=("$3")
  if [[ "$2" == "fail" && "$4" == "1" ]]; then BLOCKED+=("$1"); fi
}

show() { [[ "${JSON_ONLY}" == true ]] || printf '%s\n' "$*"; }

# ── 1. CPU 架构与厂商（hard） ──
ARCH=$(run_t 'uname -m')
if [[ "${ARCH}" == "aarch64" ]]; then
  VENDOR=$(run_t 'lscpu | grep -i "vendor id" | head -1')
  IMPL=$(run_t 'grep -m1 "CPU implementer" /proc/cpuinfo')
  if [[ "${VENDOR}" == *HiSilicon* || "${IMPL}" == *"0x48"* ]]; then
    add_check cpu_arch pass "aarch64, ${VENDOR:-}${IMPL:-}" 1
  else
    add_check cpu_arch fail "aarch64 但非 HiSilicon/鲲鹏（${VENDOR:-unknown}）" 1
  fi
else
  add_check cpu_arch fail "非 aarch64: ${ARCH:-unknown}（BRBE/CSSPGO 仅 ARM）" 1
fi

# ── 2. 昇腾 NPU（hard，证据驱动） ──
# 注意：lspci 的 "Processing accelerators" 是通用 PCI 类别，会命中 HiSilicon ZIP 压缩引擎（非 NPU），不能作判据。
# 可靠判据：存在 /dev/davinci* 设备节点，或 npu-smi 可见（必要时 source CANN set_env）。ZIP 引擎不会产生 /dev/davinci*。
NPU_DEVS=$(run_t 'ls /dev/davinci[0-9]* 2>/dev/null | tr "\n" " "')
NPU_SMI=$(run_t 'command -v npu-smi 2>/dev/null || { for e in /usr/local/Ascend/ascend-toolkit/set_env.sh /usr/local/Ascend/*/set_env.sh /home/*/cann/set_env.sh; do [ -f "$e" ] && source "$e" >/dev/null 2>&1; done; command -v npu-smi 2>/dev/null; }')
if [[ -n "${NPU_DEVS}" || -n "${NPU_SMI}" ]]; then
  add_check npu pass "davinci=[${NPU_DEVS:-none}] npu-smi=${NPU_SMI:-none}" 1
else
  add_check npu fail "无 /dev/davinci* 且无 npu-smi（lspci 命中的可能是 ZIP 压缩引擎，非 NPU）" 1
fi

# ── 3. CANN（hard，跑 NPU 负载必需） ──
CANN_DIR=$(run_t 'ls -d /usr/local/Ascend 2>/dev/null; echo "${ASCEND_HOME_PATH:-}" | head -1')
if [[ -n "${CANN_DIR}" ]]; then
  add_check cann pass "${CANN_DIR}" 1
else
  add_check cann fail "无 /usr/local/Ascend 且未设 ASCEND_HOME_PATH" 1
fi

# ── 4. 毕昇编译器（hard） ──
BISHENG=$(run_t 'clang --version | grep -i bisheng | head -1')
if [[ -n "${BISHENG}" ]]; then
  add_check bisheng pass "${BISHENG}" 1
else
  add_check bisheng fail "PATH 中无毕昇 clang（注意：可能已装但未 source profile.d）" 1
fi

# ── 5. llvm-profgen（hard，collect 必需） ──
PROFGEN=$(run_t 'command -v llvm-profgen; c=$(command -v clang); [ -n "$c" ] && ls "${c%/*}/llvm-profgen" 2>/dev/null')
if [[ -n "${PROFGEN}" ]]; then
  add_check llvm_profgen pass "$(echo "${PROFGEN}" | head -1)" 1
else
  add_check llvm_profgen fail "未找到 llvm-profgen（随毕昇/LLVM 发布）" 1
fi

# ── 6. perf（hard，collect 必需） ──
PERF_BIN=$(run_t 'command -v perf')
if [[ -n "${PERF_BIN}" ]]; then
  add_check perf pass "${PERF_BIN}" 1
else
  add_check perf fail "perf 不可用" 1
fi

# ── 7. BRBE 硬件（hard，真实分支栈校验） ──
# 注意：`perf record -j any,u` 退出码为 0 不代表产出真实分支栈——无 FEAT_BRBE 时会退化为 `0x0/.../ERET`。
# 又：部分 ARM 内核即便 BRBE 生效也不在 /proc/cpuinfo 暴露 `brbe` 标志（存在这种情况），故不能用 cpuinfo 判。
# 唯一可靠判据：真实负载采样后 brstack 中存在真实分支对（FROM/TO 均非 0x0，含 COND/CALL/UNCOND）。
if [[ -n "${PERF_BIN}" ]]; then
  BRBE_REAL=$(run_t '
    bash -c "while :; do :; done" >/dev/null 2>&1 & wp=$!
    sleep 0.3
    perf record -g --call-graph fp -e cycles:u -j any,u -F 4000 -o /tmp/brbe_env_probe_$$.data -p $wp -- sleep 3 >/dev/null 2>&1
    kill $wp 2>/dev/null; wait $wp 2>/dev/null
    perf script -F brstack -i /tmp/brbe_env_probe_$$.data 2>/dev/null | grep -cE "0x[1-9a-f][0-9a-f]+/0x[1-9a-f][0-9a-f]+"
    rm -f /tmp/brbe_env_probe_$$.data
  ')
  if [[ "${BRBE_REAL:-0}" =~ ^[0-9]+$ && "${BRBE_REAL:-0}" -ge 20 ]]; then
    add_check brbe_hw pass "真实分支栈 ${BRBE_REAL} 条（BRBE 生效）" 1
  else
    add_check brbe_hw fail "brstack 无真实分支对（实得 ${BRBE_REAL:-0} 条；仅 0x0/ERET 退化 → 无 FEAT_BRBE 或 perf 不支持 -j any,u）" 1
  fi
else
  add_check brbe_hw skip "perf 不可用，跳过" 1
fi

# ── 8. 当前栈身份（soft：信息采集，对应 skill「先证实当前二进制」） ──
TORCH_VER=$(run_t 'python3 -c "import torch; print(torch.__version__)" | tail -1')
NPU_VER=$(run_t 'python3 -c "import torch_npu; print(torch_npu.__version__)" 2>/dev/null | tail -1')
classify_so() { # <lib-path>
  local comment
  comment=$(run_t "readelf -p .comment '$1' 2>/dev/null")
  local low
  low=$(echo "${comment}" | tr 'A-Z' 'a-z')
  if [[ "${low}" == *bisheng* ]]; then echo "bisheng"
  elif [[ "${low}" == *clang* ]]; then echo "clang"
  elif [[ "${low}" == *gcc* ]]; then echo "gcc"
  else echo "unknown"; fi
}
TORCH_CMP="unknown"; NPU_CMP="absent"; ABI=""
if [[ -n "${TORCH_VER}" ]]; then
  TORCH_DIR=$(run_t 'python3 -c "import torch,os; print(os.path.dirname(torch.__file__))"')
  TORCH_CMP=$(classify_so "${TORCH_DIR}/lib/libtorch_cpu.so")
  ABI=$(run_t 'python3 -c "import torch; print(torch._C._GLIBCXX_USE_CXX11_ABI)"')
  if [[ -n "${NPU_VER}" ]]; then
    NPU_DIR=$(run_t 'python3 -c "import torch_npu,os; print(os.path.dirname(torch_npu.__file__))"')
    NPU_CMP=$(classify_so "${NPU_DIR}/lib/libtorch_npu.so")
  fi
  add_check stack_identity pass "torch=${TORCH_VER}(${TORCH_CMP}) torch_npu=${NPU_VER:-none}(${NPU_CMP}) ABI=${ABI:-?}" 0
else
  add_check stack_identity warn "python3 无法 import torch（栈身份未采集）" 0
fi

# ── 9. 磁盘空间（hard，gen 产物 torch whl 972MB/.so 2.9GB + 构建目录） ──
AVAIL_GB=$(run_t "df -BG --output=avail ${WORKDIR} | tail -1 | tr -dc '0-9'")
if [[ -n "${AVAIL_GB}" && "${AVAIL_GB}" -ge 10 ]]; then
  add_check disk pass "${WORKDIR} 可用 ${AVAIL_GB}GB（需 ≥10GB）" 1
else
  add_check disk fail "${WORKDIR} 可用 ${AVAIL_GB:-?}GB < 10GB（gen 完整 -g 产物体积大）" 1
fi

# ── 10. perf_event_paranoid（soft） ──
PARANOID=$(run_t 'cat /proc/sys/kernel/perf_event_paranoid')
if [[ -n "${PARANOID}" && "${PARANOID}" -gt 2 ]]; then
  add_check perf_paranoid warn "paranoid=${PARANOID}，perf -p 附加采样可能受限（建议 sysctl -w kernel.perf_event_paranoid=1）" 0
else
  add_check perf_paranoid pass "paranoid=${PARANOID:-n/a}" 0
fi

# ── 汇总输出 ──
show "=== BRBE 环境门控检查（$(hostname)${CONTAINER:+ @container:${CONTAINER}}）==="
for i in "${!CHECK_NAMES[@]}"; do
  show "$(printf '%-6s %-16s %s' "${CHECK_STATUS[$i]}" "${CHECK_NAMES[$i]}" "${CHECK_DETAIL[$i]}")"
done

OVERALL="ready"
if [[ ${#BLOCKED[@]} -gt 0 ]]; then OVERALL="blocked"; fi

# next_steps 提示
declare -a NEXT=()
for r in "${BLOCKED[@]}"; do
  case "$r" in
    cpu_arch) NEXT+=("非 ARM 节点：BRBE/CSSPGO 不可用") ;;
    npu|cann) NEXT+=("非 A+K 节点：换鲲鹏+昇腾目标节点执行") ;;
    bisheng) NEXT+=("安装毕昇编译器（步骤见 ak-compiler-playbook.md），安装后 source profile.d 再检测") ;;
    llvm_profgen) NEXT+=("llvm-profgen 随毕昇发布：source 毕昇环境后重试") ;;
    brbe_hw) NEXT+=("无 BRBE 硬件：改走插桩 PGO（ak_compile_optimize.sh --optimize lto_pgo）") ;;
    perf) NEXT+=("安装 perf（yum/dnf install perf）") ;;
    disk) NEXT+=("清理 ${WORKDIR} 或换 --workdir") ;;
  esac
done

# JSON 总结（清洗引号/反斜杠，保证可解析）
sanitize() { echo "$1" | sed 's/["\]/ /g'; }
CHECKS_JSON=""
for i in "${!CHECK_NAMES[@]}"; do
  [[ -n "${CHECKS_JSON}" ]] && CHECKS_JSON="${CHECKS_JSON},"
  CHECKS_JSON="${CHECKS_JSON}\"${CHECK_NAMES[$i]}\":\"${CHECK_STATUS[$i]}\""
done
BLOCKED_JSON=""
for r in "${BLOCKED[@]:-}"; do
  [[ -z "${r}" ]] && continue
  [[ -n "${BLOCKED_JSON}" ]] && BLOCKED_JSON="${BLOCKED_JSON},"
  BLOCKED_JSON="${BLOCKED_JSON}\"${r}\""
done
NEXT_JSON=""
for n in "${NEXT[@]:-}"; do
  [[ -z "${n}" ]] && continue
  [[ -n "${NEXT_JSON}" ]] && NEXT_JSON="${NEXT_JSON},"
  NEXT_JSON="${NEXT_JSON}\"$(sanitize "${n}")\""
done
STACK_JSON="{\"torch\":\"$(sanitize "${TORCH_VER:-none}")\",\"torch_compiler\":\"${TORCH_CMP}\",\"torch_npu\":\"$(sanitize "${NPU_VER:-none}")\",\"torch_npu_compiler\":\"${NPU_CMP}\",\"abi\":\"$(sanitize "${ABI:-unknown}")\"}"

echo ""
echo "{\"status\":\"${OVERALL}\",\"node\":\"$(sanitize "$(hostname)")\",\"container\":\"$(sanitize "${CONTAINER}")\",\"checks\":{${CHECKS_JSON}},\"stack_identity\":${STACK_JSON},\"disk_available_gb\":${AVAIL_GB:-0},\"blocked_reasons\":[${BLOCKED_JSON}],\"next_steps\":[${NEXT_JSON}]}"

[[ "${OVERALL}" == "ready" ]] && exit 0 || exit 1
