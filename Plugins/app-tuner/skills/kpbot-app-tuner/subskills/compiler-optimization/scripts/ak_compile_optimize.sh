#!/usr/bin/env bash
set -euo pipefail

# ak_compile_optimize.sh
# A+K 场景编译优化脚本（Python/PyTorch/torch_npu）
# 使用毕昇编译器进行 LTO/PGO 编译优化
#
# 用法:
#   ak_compile_optimize.sh --component <name> --optimize <type> --stage <stage> [options]
#
# 示例:
#   # 检查环境
#   ak_compile_optimize.sh --check-only
#   # Python LTO+PGO
#   ak_compile_optimize.sh --component python --optimize lto_pgo --stage compile --install-dir /opt/python-ak
#   # PyTorch LTO
#   ak_compile_optimize.sh --component pytorch --optimize lto --stage compile --source-dir /src/pytorch
#   # PyTorch PGO 一次编译
#   ak_compile_optimize.sh --component pytorch --optimize lto_pgo --stage profile_gen --source-dir /src/pytorch
#   # PyTorch PGO 二次编译
#   ak_compile_optimize.sh --component pytorch --optimize lto_pgo --stage profile_use --source-dir /src/pytorch
#   # PyTorch CSSPGO(BRBE) gen（伪探针 + 完整 -g）
#   ak_compile_optimize.sh --component pytorch --optimize css_pgo --stage gen --source-dir /src/pytorch
#   # CSSPGO collect（对运行中的 gen 版进程采样，免插桩）
#   ak_compile_optimize.sh --component pytorch --optimize css_pgo --stage collect --pid 12345 --binary /path/libtorch_cpu.so
#   # CSSPGO use（用 sample profile 重编译）
#   ak_compile_optimize.sh --component pytorch --optimize css_pgo --stage use --source-dir /src/pytorch

COMPONENT=""
OPTIMIZE=""
STAGE=""
SOURCE_DIR=""
INSTALL_DIR=""
PYTHON_VERSION=""
PROFILE_DIR="/tmp/profile"
CONTAINER=""
CHECK_ONLY=false
VERIFY=false
DRY_RUN=false
# css_pgo(BRBE/CSSPGO) 专用
PID=""
COLLECT_CMD=""
BINARY_PATH=""
COLLECT_DURATION=90
PAGE_SIZE=""
MCPU="auto"
CSS_PROFILE=""
# 默认命令名（dry-run 打印完整命令用；真实执行前 check_llvm_profgen 会解析为绝对路径）
LLVM_PROFGEN="llvm-profgen"

usage() {
  cat <<'EOF'
Usage:
  ak_compile_optimize.sh --component <name> --optimize <type> --stage <stage> [options]

Required:
  --component <name>       组件: python | pytorch | torch_npu
  --optimize <type>        优化类型: lto | lto_pgo | css_pgo
  --stage <stage>          编译阶段:
                              lto 模式: compile
                              lto_pgo 模式: compile | profile_gen | profile_use
                              css_pgo 模式: gen | collect | use

Options:
  --source-dir <dir>       源码目录（不指定则自动下载）
  --install-dir <dir>       安装目录（Python 必填）
  --python-version <ver>   Python 版本号（torch_npu 编译用，不指定则自动检测）
  --profile-dir <dir>      PGO profile 目录（默认 /tmp/profile）
  --container <name>       在指定容器内执行（docker exec）
  --pid <pid>              css_pgo collect: 附加采样运行中的 gen 进程（与 --cmd 二选一）
  --cmd "<command>"        css_pgo collect: 离线采样——perf 包裹执行短生命周期命令（与 --pid 二选一；
                           命令在目标执行上下文原样运行，--duration 不适用，perf 随命令结束）
  --binary <path>          css_pgo collect: 含完整 DWARF 的 gen 产物（.so/可执行文件）
  --duration <seconds>     css_pgo collect: 采样时长（默认 90 秒）
  --page-size <size>       css_pgo collect: llvm-profgen -page-size（须与目标 OS 页大小一致，如 65536；本机 llvm-profgen 不支持时不传）
  --css-profile <file>     css_pgo use: sample profile 路径（默认 <profile-dir>/<component>.spgo.profraw）
  --mcpu <auto|name>       目标 CPU：auto（默认）用 -mcpu=native 让编译器解析本机 CPU，
                           只有解析出具体型号才采用；解析为 generic 则不添加（实测 generic 反而最快）。
                           也可显式指定，如 hip11（需自行 A/B，本机实测比默认慢）
  --check-only             只检查环境不编译
  --verify                 编译后验证
  --dry-run                只输出命令不实际执行
  -h, --help               显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component) COMPONENT="${2:?}"; shift 2 ;;
    --optimize) OPTIMIZE="${2:?}"; shift 2 ;;
    --stage) STAGE="${2:?}"; shift 2 ;;
    --source-dir) SOURCE_DIR="${2:?}"; shift 2 ;;
    --install-dir) INSTALL_DIR="${2:?}"; shift 2 ;;
    --python-version) PYTHON_VERSION="${2:?}"; shift 2 ;;
    --profile-dir) PROFILE_DIR="${2:?}"; shift 2 ;;
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --pid) PID="${2:?}"; shift 2 ;;
    --cmd) COLLECT_CMD="${2:?}"; shift 2 ;;
    --binary) BINARY_PATH="${2:?}"; shift 2 ;;
    --duration) COLLECT_DURATION="${2:?}"; shift 2 ;;
    --page-size) PAGE_SIZE="${2:?}"; shift 2 ;;
    --css-profile) CSS_PROFILE="${2:?}"; shift 2 ;;
    --mcpu) MCPU="${2:?}"; shift 2 ;;
    --check-only) CHECK_ONLY=true; shift ;;
    --verify) VERIFY=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

fail() { echo "ERROR: $*" >&2; exit 1; }

validate_uint() { # <参数名> <值>
  [[ "${2:-}" =~ ^[0-9]+$ ]] || fail "参数 ${1} 须为非负整数，收到: ${2:-}"
}
validate_path() { # <参数名> <值>
  [[ -n "${2:-}" && "${2:-}" =~ ^[A-Za-z0-9_./@=+-]+$ ]] || fail "参数 ${1} 含空格/引号/;|&\$\` 等元字符或为空（仅允许 [A-Za-z0-9_./@=+-]）"
}

run() {
  if [[ "${DRY_RUN}" == true ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

run_in_target() {
  if [[ "${DRY_RUN}" == true ]]; then
    echo "[dry-run] $*"
    return 0
  fi
  if [[ -n "${CONTAINER}" ]]; then
    docker exec "${CONTAINER}" bash -c "$*"
  else
    bash -c "$*"
  fi
}

# 目标 CPU 自适应：完全以"编译器自己的判定"为准，绝不猜型号
#   ① 显式 --mcpu <name>：原样使用
#   ② auto：用 `-mcpu=native -###` 看编译器把本机解析成什么
#        - 解析出真实 CPU 名（非 generic）→ 用 -mcpu=native
#        - 解析为 generic / 无法解析 → 不添加 -mcpu（等于编译器默认），并打印可选名单
#        （实测数据与决策记录见 references/csspgo-brbe-playbook.md）
mcpu_flag() {
  if [[ -n "${_MCPU_FLAG_CACHE+x}" ]]; then
    printf '%s' "${_MCPU_FLAG_CACHE}"
    return 0
  fi
  local cc="${CC:-clang}"
  local want="${MCPU:-auto}" resolved="" supported=""
  _MCPU_FLAG_CACHE=""
  if [[ "${want}" != "auto" ]]; then
    _MCPU_FLAG_CACHE="-mcpu=${want}"
    printf '%s' "${_MCPU_FLAG_CACHE}"
    return 0
  fi
  if [[ "${DRY_RUN}" == true ]]; then
    _MCPU_FLAG_CACHE="-mcpu=<auto>"
    printf '%s' "${_MCPU_FLAG_CACHE}"
    return 0
  fi

  if run_in_target "${cc} -mcpu=native -c -x c /dev/null -o /dev/null" >/dev/null 2>&1; then
    resolved=$(run_in_target "${cc} -mcpu=native -### -c -x c /dev/null 2>&1 | tr ' ' '\n' | grep -A1 -- '-target-cpu' | tail -1 | tr -d '\042'")
    resolved=$(printf '%s' "${resolved}" | tr -d '[:space:]')
  fi

  if [[ -n "${resolved}" && "${resolved}" != "generic" ]]; then
    _MCPU_FLAG_CACHE="-mcpu=native"
    log "CPU 自适应: -mcpu=native 解析为 ${resolved}，采用 -mcpu=native" >&2
  else
    supported=$(run_in_target "${cc} --print-supported-cpus 2>&1 | sed -n '/Available CPUs/,\$p' | sed '/Use -mcpu/,\$d' | tr -d '\t' | tr '\n' ' '")
    # clang 19+ 的 --print-supported-cpus 不再输出 "Available CPUs" 标题，仅日志回退用
    [[ -n "${supported}" ]] || supported="（编译器未提供列表）"
    log "CPU 自适应: native 解析为 ${resolved:-unknown}（非具体型号），不添加 -mcpu；编译器可选: ${supported}" >&2
  fi
  printf '%s' "${_MCPU_FLAG_CACHE}"
}

output_json() {
  local status="$1"; shift
  local json="{\"component\":\"${COMPONENT}\",\"optimize\":\"${OPTIMIZE}\",\"stage\":\"${STAGE}\",\"status\":\"${status}\""
  while [[ $# -gt 0 ]]; do
    json="${json},$1"
    shift
  done
  json="${json}}"
  echo ""
  echo "${json}"
}

# ── 前置检查 ─────────────────────────────────────────────────

check_bisheng() {
  log "检测毕昇编译器..."
  local result
  result=$(run_in_target 'clang --version 2>/dev/null | grep -i "bisheng"' || true)
  if [[ -z "${result}" ]]; then
    echo '{"status":"failed","fail_reason":"bisheng_not_found","diagnostics":{"message":"未检测到毕昇编译器，系统自带的 clang 不是毕昇编译器","recovery_steps":["参考昇腾官方文档安装毕昇编译器","安装后执行: clang --version | grep bisheng 确认"]},"retryable":false}'
    return 1
  fi
  log "毕昇编译器: ${result}"
  return 0
}

# ── CSSPGO(BRBE) 专用 ────────────────────────────────────────

check_llvm_profgen() {
  log "检测 llvm-profgen（CSSPGO sample profile 生成工具）..."
  # 在执行上下文探测（--container 时为容器内），与后续 run_in_target 实际执行处一致；
  # 宿主机上探测会把宿主机绝对路径带进容器，路径不存在 → 必然误报
  local profgen=""
  profgen="$(run_in_target "command -v llvm-profgen 2>/dev/null")"
  profgen="${profgen%%[[:space:]]*}"
  if [[ -z "${profgen}" ]]; then
    local clang_bin
    clang_bin="$(run_in_target "command -v clang 2>/dev/null")"
    clang_bin="${clang_bin%%[[:space:]]*}"
    if [[ -n "${clang_bin}" ]]; then
      local cand="${clang_bin%/*}/llvm-profgen"
      if [[ "$(run_in_target "[ -x ${cand} ] && echo x")" == "x" ]]; then
        profgen="${cand}"
      fi
    fi
  fi
  if [[ -z "${profgen}" ]]; then
    echo '{"status":"failed","fail_reason":"llvm_profgen_not_found","diagnostics":{"message":"未找到 llvm-profgen（随毕昇编译器/LLVM 发布，用于 perf script -> sample profile 转换）","recovery_steps":["安装毕昇编译器并 source /etc/profile.d/bisheng.sh 后重试","或确认 llvm-profgen 所在目录已加入 PATH"]},"retryable":false}'
    return 1
  fi
  LLVM_PROFGEN="${profgen}"
  log "llvm-profgen: ${profgen}"
  return 0
}

css_profile_path() {
  if [[ -n "${CSS_PROFILE}" ]]; then
    echo "${CSS_PROFILE}"
  else
    echo "${PROFILE_DIR}/${COMPONENT}.spgo.profraw"
  fi
}

check_css_profile() {
  local profile_file
  profile_file="$(css_profile_path)"
  log "检查 CSS sample profile: ${profile_file}"
  if ! run_in_target "[ -s \"${profile_file}\" ]"; then
    cat <<EOF
{"status":"failed","fail_reason":"css_profile_not_found","diagnostics":{"profile":"${profile_file}","possible_causes":["gen 阶段产物未安装/未运行（collect 采样的是旧二进制则 profile 无效）","collect 阶段未执行，或 llvm-profgen 产出为空","gen 构建缺完整 -g（共享库 -gline-tables-only 不够，DWARF 缺失导致空 profile）","--css-profile 路径配错"],"recovery_steps":["执行采集: ak_compile_optimize.sh --component ${COMPONENT} --optimize css_pgo --stage collect --pid <pid> --binary <gen产物>","确认 gen 构建含 -fpseudo-probe-for-profiling -g","重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize css_pgo --stage use --css-profile <file>"]},"retryable":true}
EOF
    return 1
  fi
  local size=0 func_count=0
  if [[ "${DRY_RUN}" != true ]]; then
    size=$(run_in_target "stat -c %s ${profile_file} 2>/dev/null || echo 0")
    func_count=$(run_in_target "grep -cE '^[^[:space:]!][^:]*:[0-9]+:[0-9]+' ${profile_file} 2>/dev/null")
    func_count="${func_count:-0}"
  fi
  if [[ "${DRY_RUN}" != true && "${func_count}" -eq 0 ]]; then
    log "⚠️  profile 无函数归属（${size} 字节），疑似空 profile（常见根因: gen 构建缺完整 -g / DWARF，llvm-profgen 枚举不到函数）"
  fi
  log "CSS profile 检查通过: ${profile_file} (${size} bytes, ${func_count} 个函数)"
  return 0
}

check_brbe_support() {
  log "检测 BRBE 分支采样（真实负载采样 + brstack 真实分支对校验）..."
  if [[ "${DRY_RUN}" == true ]]; then
    log "BRBE 检测（dry-run 跳过）"
    return 0
  fi
  run_in_target "mkdir -p ${PROFILE_DIR}"
  local real
  real=$(run_in_target "
    bash -c 'while :; do :; done' >/dev/null 2>&1 & wp=\$!
    sleep 0.3
    perf record -g --call-graph fp -e cycles:u -j any,u -o ${PROFILE_DIR}/brbe_probe_\$\$.data -p \$wp -- sleep 3 >/dev/null 2>&1
    kill \$wp 2>/dev/null; wait \$wp 2>/dev/null
    perf script -F brstack -i ${PROFILE_DIR}/brbe_probe_\$\$.data 2>/dev/null | grep -cE '0x[1-9a-f][0-9a-f]+/0x[1-9a-f][0-9a-f]+'
    rm -f ${PROFILE_DIR}/brbe_probe_\$\$.data
  ")
  if [[ "${real:-0}" =~ ^[0-9]+$ && "${real:-0}" -ge 20 ]]; then
    log "BRBE 分支采样确认（真实分支栈 ${real} 条）"
    return 0
  fi
  echo "{\"status\":\"failed\",\"fail_reason\":\"brbe_not_supported\",\"diagnostics\":{\"message\":\"brstack 无真实分支对（仅 0x0/ERET 退化，或 perf 不支持 -j any,u）：该节点无 FEAT_BRBE 或内核/perf 限制\",\"real_branch_lines\":\"${real:-0}\",\"hint\":\"/proc/cpuinfo 是否暴露 brbe 标志不可靠（部分内核不暴露但 BRBE 生效），以真实采样为准\",\"recovery_steps\":[\"换用支持 BRBE 的节点\",\"无 BRBE 时改走插桩 PGO: --optimize lto_pgo\"]},\"retryable\":false}"
  return 1
}

collect_css_profile() {
  # --pid 附加采样 / --cmd 离线包裹采样（短生命周期命令：perf record -- <cmd>，
  # 命令执行期间采样，进程退出即结束——附加模式对短命令永远采不到热点）
  if [[ -z "${PID}" && -z "${COLLECT_CMD}" ]]; then
    fail "--stage collect 需要 --pid（附加运行中的 gen 进程）或 --cmd（离线包裹短生命周期命令）"
  fi
  [[ -n "${BINARY_PATH}" ]] || fail "--stage collect 需要 --binary（含完整 DWARF 的 gen 产物 .so/可执行文件）"
  local profile_file
  profile_file="$(css_profile_path)"
  local perf_tag="${PID:-cmd_$$}"
  local perf_data="${PROFILE_DIR}/css_perf_${perf_tag}.data"
  local perf_script="${PROFILE_DIR}/css_perf_${perf_tag}.script"

  if [[ -n "${COLLECT_CMD}" ]]; then
    log "离线包裹采样: perf record -- ${COLLECT_CMD} | 二进制: ${BINARY_PATH}（perf 随命令结束，--duration 不适用）"
  else
    log "目标进程: PID=${PID}, 采样二进制: ${BINARY_PATH}, 时长: ${COLLECT_DURATION}s"
  fi

  if [[ -n "${PID}" && "${DRY_RUN}" != true ]] && ! run_in_target "[ -d \"/proc/${PID}\" ]"; then
    fail "目标进程不存在: ${PID}（须先用 gen 产物启动真实负载）"
  fi
  if ! run_in_target "[ -f \"${BINARY_PATH}\" ]"; then
    fail "采样二进制不存在: ${BINARY_PATH}（须为 gen 阶段产物，含完整 DWARF，否则 profile 为空）"
  fi
  if [[ "${DRY_RUN}" != true ]]; then
    # llvm-profgen 依据 perf mmap 事件里的文件名匹配二进制；若 --binary 是软链
    # （如 python3 -> python3.10），basename 与 mmap 名不一致会静默产出 0 样本空 profile。
    local resolved_binary
    resolved_binary="$(run_in_target "readlink -f ${BINARY_PATH}")"
    # 存在性判断同样在目标上下文执行（--container 时宿主机看不到容器内路径）
    if [[ -n "${resolved_binary}" && "${resolved_binary}" != "${BINARY_PATH}" && "$(run_in_target "[ -f \"${resolved_binary}\" ] && echo x")" == "x" ]]; then
      log "采样二进制为软链，解析为真实路径: ${resolved_binary}"
      BINARY_PATH="${resolved_binary}"
    fi
  fi
  run_in_target "mkdir -p ${PROFILE_DIR}"
  run_in_target "rm -f ${perf_data} ${perf_script}"

  check_brbe_support || return 1

  log "perf record（BRBE 分支采样，帧指针调用栈）..."
  local record_rc=0
  if [[ -n "${COLLECT_CMD}" ]]; then
    if ! run_in_target "perf record -g --call-graph fp -e cycles:u -j any,u -o ${perf_data} -- ${COLLECT_CMD}"; then
      record_rc=1
    fi
  else
    if ! run_in_target "perf record -g --call-graph fp -e cycles:u -j any,u -o ${perf_data} -p ${PID} -- sleep ${COLLECT_DURATION}"; then
      record_rc=1
    fi
  fi
  if [[ "${record_rc}" -ne 0 ]]; then
    cat <<EOF
{"status":"failed","fail_reason":"perf_record_failed","diagnostics":{"pid":"${PID:-}","mode":"${COLLECT_CMD:+offline_cmd_wrapped}${PID:+attach_pid}","possible_causes":["perf_event_paranoid 权限限制（尝试 sysctl -w kernel.perf_event_paranoid=1）","目标进程中途退出（附加模式）或包裹命令非零退出（离线模式）","BRBE 资源被其他会话占用"],"retryable":true}
EOF
    return 1
  fi

  log "perf script 导出 ip+brstack..."
  if ! run_in_target "perf script -F ip,brstack --show-mmap-event -i ${perf_data} > ${perf_script}"; then
    fail "perf script 导出失败"
  fi

  # 前置校验：perf mmap 事件中必须出现该二进制名，否则 llvm-profgen 会静默产出空 profile
  # （grep 必须走 run_in_target：perf.script 写在目标上下文，--container 时宿主机路径不存在，
  #  宿主机 grep 会恒定误报 binary_mmap_mismatch）
  if [[ "${DRY_RUN}" != true ]]; then
    local bin_base
    bin_base="$(basename "${BINARY_PATH}")"
    if ! run_in_target "grep -aq -- ${bin_base} ${perf_script}"; then
      cat <<EOF
{"status":"failed","fail_reason":"binary_mmap_mismatch","diagnostics":{"binary":"${BINARY_PATH}","perfscript":"${perf_script}","hint":"perf script 的 mmap 事件中未找到 ${bin_base}，llvm-profgen 无法把样本映射到二进制；常见原因是 --binary 与进程实际加载的文件名不一致（软链/重命名/tmp 副本）"},"retryable":true}
EOF
      return 1
    fi
  fi

  local page_size_arg=""
  if [[ -n "${PAGE_SIZE}" ]]; then
    page_size_arg="-page-size=${PAGE_SIZE}"
  fi

  log "llvm-profgen 生成 sample profile..."
  if ! run_in_target "${LLVM_PROFGEN} --binary=${BINARY_PATH} --perfscript=${perf_script} --output=${profile_file} --format=text ${page_size_arg}"; then
    cat <<EOF
{"status":"failed","fail_reason":"profgen_failed","diagnostics":{"binary":"${BINARY_PATH}","perfscript":"${perf_script}","hints":["共享库必须完整 -g（-gline-tables-only 不够）","部分 llvm-profgen 版本无 -page-size 选项，此时不传 --page-size","Invalid instructions 告警可忽略，整体失败需检查 perfscript 格式"],"retryable":true}
EOF
    return 1
  fi

  # 空 profile 判定按函数归属（顶层 "name:total:entry" 行数），不按文件大小：
  # 小二进制/小组件的有效 profile 可远小于 10KB，按大小误判会拒绝有效产物。
  local size=0 func_count=0
  if [[ "${DRY_RUN}" != true ]]; then
    size=$(run_in_target "stat -c %s ${profile_file} 2>/dev/null || echo 0")
    func_count=$(run_in_target "grep -cE '^[^[:space:]!][^:]*:[0-9]+:[0-9]+' ${profile_file} 2>/dev/null")
    func_count="${func_count:-0}"
  fi
  if [[ "${DRY_RUN}" != true && "${func_count}" -eq 0 ]]; then
    cat <<EOF
{"status":"failed","fail_reason":"empty_css_profile","diagnostics":{"profile":"${profile_file}","size":${size},"attributed_functions":0,"possible_causes":["gen 构建缺完整 -g（DWARF 缺失，llvm-profgen 枚举不到函数）","采样时长不足或目标进程空闲","--binary 指向了错误二进制（非 gen 产物）"],"recovery_steps":["重新 gen: ak_compile_optimize.sh --component ${COMPONENT} --optimize css_pgo --stage gen","用 --duration 延长采样后重新采集"],"retryable":true}
EOF
    return 1
  fi

  log "CSS profile 生成完成: ${profile_file} (${size} bytes, ${func_count} 个函数)"
  log "（perf.data/perf.script 保留于 ${PROFILE_DIR} 供排查）"
  return 0
}

detect_python_version() {
  if [[ -z "${PYTHON_VERSION}" ]]; then
    if [[ -n "${CONTAINER}" ]]; then
      PYTHON_VERSION=$(docker exec "${CONTAINER}" python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "")
    else
      PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "")
    fi
    if [[ -z "${PYTHON_VERSION}" ]]; then
      fail "无法检测 Python 版本，请使用 --python-version 指定"
    fi
  fi
  log "Python 版本: ${PYTHON_VERSION}"
}

check_container() {
  if [[ -n "${CONTAINER}" ]]; then
    log "检测容器 ${CONTAINER}..."
    if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
      fail "容器 ${CONTAINER} 不存在"
    fi
    log "容器 ${CONTAINER} 存在"
  fi
}

check_source() {
  local dir="$1"
  local component="$2"
  log "检查源码目录: ${dir}"

  if ! run_in_target "[ -d \"${dir}\" ]"; then
    fail "源码目录不存在: ${dir}"
  fi

  log "检查子模块完整性..."
  local sub_status
  sub_status=$(run_in_target "cd ${dir} && git submodule status 2>/dev/null || echo 'NO_GIT'")
  if [[ "${sub_status}" == "NO_GIT" ]]; then
    log "非 git 仓库，跳过子模块检查"
  else
    local incomplete
    incomplete=$(echo "${sub_status}" | grep -c '^-' || true)
    if [[ "${incomplete}" -gt 0 ]]; then
      log "发现 ${incomplete} 个未初始化的子模块，尝试修复..."
      run_in_target "cd ${dir} && git submodule update --init --recursive" || {
        log "子模块更新失败，重试..."
        run_in_target "cd ${dir} && git submodule deinit --all && git submodule update --init --recursive" || fail "子模块更新失败"
      }
    fi
  fi

  case "${component}" in
    pytorch)
      run_in_target "[ -d \"${dir}/aten/src\" ] && [ -d \"${dir}/third_party\" ] && [ -f \"${dir}/requirements.txt\" ]" || fail "源码预检失败: 缺少关键目录或文件"
      ;;
    torch_npu)
      run_in_target "[ -f \"${dir}/ci/build.sh\" ]" || fail "源码预检失败: 缺少 ci/build.sh"
      ;;
  esac
  log "源码完整性检查通过"
}

check_disk_space() {
  local required_gb="$1"
  local target_path="${SOURCE_DIR:-/tmp}"
  local available_kb
  available_kb=$(df -k "${target_path}" 2>/dev/null | tail -1 | awk '{print $4}')
  local available_gb=$((available_kb / 1024 / 1024))
  log "磁盘空间检查: ${target_path} 可用 ${available_gb}GB, 需要 ${required_gb}GB"
  if [[ "${available_gb}" -lt "${required_gb}" ]]; then
    fail "磁盘空间不足: 可用 ${available_gb}GB, 编译需要至少 ${required_gb}GB, 请清理磁盘后重试"
  fi
}

download_source() {
  local component="$1"
  local version="$2"
  local target_dir="$3"

  log "下载 ${component} ${version} 源码到 ${target_dir}..."

  case "${component}" in
    python)
      local tarball="Python-${version}.tgz"
      run wget -q --timeout=30 "https://mirrors.huaweicloud.com/python/${version}/${tarball}" -O "/tmp/${tarball}" \
        || run wget -q --timeout=30 "https://www.python.org/ftp/python/${version}/${tarball}" -O "/tmp/${tarball}"
      run tar -xf "/tmp/${tarball}" -C "$(dirname ${target_dir})"
      run mv "$(dirname ${target_dir})/Python-${version}" "${target_dir}"
      ;;
    pytorch)
      run git clone -b "v${version}" --depth 1 --shallow-submodules https://gitee.com/mirrors/pytorch.git "${target_dir}" 2>/dev/null \
        || run git clone -b "v${version}" --depth 1 --shallow-submodules https://github.com/pytorch/pytorch.git "${target_dir}"
      run_in_target "cd ${target_dir} && git submodule sync && git submodule update --init --recursive --depth 1"
      ;;
    torch_npu)
      # torch_npu 分支名格式: v<PyTorch版本>-<CANN版本>，如 v2.1.0-6.0.0
      # version 参数应已包含完整分支名（如 v2.1.0-6.0.0），若只有 CANN 版本需结合 PyTorch 版本拼接
      local branch="${version}"
      if [[ "${branch}" != v* ]]; then
        branch="v${branch}"
      fi
      run git clone -b "${branch}" https://gitee.com/ascend/pytorch.git "${target_dir}"
      run_in_target "cd ${target_dir} && git submodule update --init --recursive"
      ;;
  esac
}

detect_current_version() {
  local component="$1"
  local version=""
  case "${component}" in
    python)
      version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || echo "")
      if [[ -z "${version}" && -n "${CONTAINER}" ]]; then
        version=$(docker exec "${CONTAINER}" python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || echo "")
      fi
      ;;
    pytorch)
      version=$(python3 -c "import torch; print(torch.__version__)" 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' || echo "")
      if [[ -z "${version}" && -n "${CONTAINER}" ]]; then
        version=$(docker exec "${CONTAINER}" python3 -c "import torch; print(torch.__version__)" 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' || echo "")
      fi
      ;;
    torch_npu)
      # torch_npu 版本格式: <PyTorch版本>-<CANN版本>，如 2.1.0-6.0.0
      # 需要同时采集 PyTorch 版本和 torch_npu 版本拼接为分支名
      local torch_ver npu_ver
      torch_ver=$(python3 -c "import torch; print(torch.__version__)" 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' || echo "")
      npu_ver=$(python3 -c "import torch_npu; print(torch_npu.__version__)" 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' || echo "")
      if [[ -z "${torch_ver}" || -z "${npu_ver}" ]]; then
        torch_ver=$(docker exec "${CONTAINER}" python3 -c "import torch; print(torch.__version__)" 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' || echo "")
        npu_ver=$(docker exec "${CONTAINER}" python3 -c "import torch_npu; print(torch_npu.__version__)" 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' || echo "")
      fi
      # 返回完整分支名格式: v<PyTorch版本>-<CANN版本>
      version="${torch_ver}-${npu_ver}"
      ;;
  esac
  echo "${version}"
}

# ── Profile 检查 ─────────────────────────────────────────────

check_profile() {
  local profile_dir="$1"
  log "检查 profile 目录: ${profile_dir}"

  local profraw_files
  profraw_files=$(run_in_target "ls -la ${profile_dir}/*.profraw 2>/dev/null || echo 'NONE'")

  if [[ "${profraw_files}" == "NONE" ]]; then
    cat <<EOF
{"status":"failed","fail_reason":"no_profraw","diagnostics":{"profile_dir":"${profile_dir}","profraw_count":0,"expected_env":"LLVM_PROFILE_FILE=${profile_dir}/default_%m.profraw","possible_causes":["模型未运行或运行时间过短，profile 未生成","LLVM_PROFILE_FILE 环境变量未设置或指向了其他目录","插桩版未正确安装，运行的不是插桩版二进制"],"recovery_steps":["确认插桩版已安装: pip show torch | grep Version","确认环境变量: echo \$LLVM_PROFILE_FILE","重新运行模型: export OMP_PROC_BIND=false; export LLVM_PROFILE_FILE=${profile_dir}/default_%m.profraw; python3 your_script.py","确认 profile 生成: ls -la ${profile_dir}/*.profraw","重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize lto_pgo --stage profile_use"]},"retryable":true}
EOF
    return 1
  fi

  local profraw_count=0
  local empty_count=0
  if [[ "${DRY_RUN}" != true ]]; then
    profraw_count=$(run_in_target "ls ${profile_dir}/*.profraw 2>/dev/null | wc -l")
    log "找到 ${profraw_count} 个 .profraw 文件"
    empty_count=$(run_in_target "find ${profile_dir} -name '*.profraw' -size 0 2>/dev/null | wc -l")
  fi
  if [[ "${DRY_RUN}" != true && "${profraw_count}" -eq "${empty_count}" ]]; then
    cat <<EOF
{"status":"failed","fail_reason":"empty_profraw","diagnostics":{"profile_dir":"${profile_dir}","profraw_count":${profraw_count},"empty_count":${empty_count},"possible_causes":["模型运行时间过短，profile 数据未写入","进程异常退出，profile 未正常 flush"],"recovery_steps":["重新运行模型并确保正常完成","确认 profile 生成: ls -la ${profile_dir}/*.profraw","重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize lto_pgo --stage profile_use"]},"retryable":true}
EOF
    return 1
  fi

  log "Profile 文件检查通过"
  return 0
}

merge_profile() {
  local profile_dir="$1"
  log "转换 profile 数据..."
  run_in_target "llvm-profdata merge ${profile_dir} -o ${profile_dir}/default.profdata"

  local profdata_size=0
  if [[ "${DRY_RUN}" != true ]]; then
    profdata_size=$(run_in_target "stat -c %s ${profile_dir}/default.profdata 2>/dev/null || echo 0")
  fi
  if [[ "${DRY_RUN}" != true && "${profdata_size}" -eq 0 ]]; then
    cat <<EOF
{"status":"failed","fail_reason":"profdata_merge_failed","diagnostics":{"profile_dir":"${profile_dir}","profdata_path":"${profile_dir}/default.profdata","profdata_size":0,"recovery_steps":["检查 .profraw 文件是否有效","手动执行: llvm-profdata merge ${profile_dir} -o ${profile_dir}/default.profdata","重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize lto_pgo --stage profile_use"]},"retryable":true}
EOF
    return 1
  fi
  log "Profile 转换完成: ${profile_dir}/default.profdata (${profdata_size} bytes)"
  return 0
}

# ── 编译函数 ─────────────────────────────────────────────────

compile_python() {
  local source_dir="$1"
  local install_dir="$2"
  local stage="$3"

  if [[ "${OPTIMIZE}" == "css_pgo" ]]; then
    # CSSPGO(BRBE) Python：--with-lto 不加 --enable-optimizations（BRBE 采样 profile 替代内置 PGO）
    # gen: 伪探针 + 完整 -g（llvm-profgen 依赖 DWARF 枚举函数）
    # use: 必须保留 -fpseudo-probe-for-profiling，且 LDFLAGS 也要带（LTO 链接期绑定 profile）
    local cflags="-O3 -fno-omit-frame-pointer -fpseudo-probe-for-profiling $(mcpu_flag)"
    local ldflags="-zcommon-page-size=2097152 -zmax-page-size=2097152 -fno-plt -flto -fuse-ld=lld -Wl,-z,noseparate-code -Wl,-z,norelro $(mcpu_flag)"
    local profile_file
    profile_file="$(css_profile_path)"
    case "${stage}" in
      gen)
        log "编译 Python CSSPGO gen（伪探针 + 完整 -g + LTO）..."
        run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ && ./configure --prefix=${install_dir} --with-lto CFLAGS='${cflags} -g' LDFLAGS='${ldflags}'"
        ;;
      use)
        log "编译 Python CSSPGO use（sample profile）..."
        run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ && ./configure --prefix=${install_dir} --with-lto CFLAGS='${cflags} -fprofile-sample-use=${profile_file}' LDFLAGS='${ldflags} -fpseudo-probe-for-profiling -fprofile-sample-use=${profile_file}'"
        ;;
    esac
  else
    log "编译 Python (${stage})..."
    run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ && ./configure --prefix=${install_dir} --with-lto --enable-optimizations"
  fi
  # 强制清理旧目标文件：复用同一源码树时（gen 后直接跑 use），make 会认为 .o 已最新
  # 而跳过重编，导致 use 阶段静默沿用 gen 产物、完全忽略 sample profile 却仍报成功。
  run_in_target "cd ${source_dir} && make clean >/dev/null 2>&1 || true"
  run_in_target "cd ${source_dir} && make -j \$(nproc)"
  run_in_target "cd ${source_dir} && make install"
  log "Python 编译安装完成: ${install_dir}"
}

compile_pytorch() {
  local source_dir="$1"
  local stage="$2"
  local profile_dir="$3"

  local extra_flags=""
  if [[ "${OPTIMIZE}" == "css_pgo" ]]; then
    local profile_file
    profile_file="$(css_profile_path)"
    case "${stage}" in
      gen)
        # gen: 伪探针 + 完整 -g（共享库 -gline-tables-only 不够，llvm-profgen 需完整 DWARF）
        extra_flags="-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g $(mcpu_flag)"
        ;;
      use)
        # use: 去 -g；必须保留 -fpseudo-probe-for-profiling（SampleProfileProbePass 绑定，否则 profile 被忽略）
        extra_flags="-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -fprofile-sample-use=${profile_file} $(mcpu_flag)"
        ;;
    esac
  else
    case "${stage}" in
      compile)
        extra_flags="-flto=thin -fuse-ld=lld"
        ;;
      profile_gen)
        extra_flags="-flto=thin -fuse-ld=lld -fprofile-generate=${profile_dir}"
        ;;
      profile_use)
        extra_flags="-flto=thin -fuse-ld=lld -fprofile-use=${profile_dir}/default.profdata"
        ;;
    esac
  fi

  log "编译 PyTorch (${stage})..."

  run_in_target "cd ${source_dir} && git clean -dfx"
  run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ USE_XNNPACK=0 USE_NNPACK=0 USE_PYTORCH_QNNPACK=0 USE_TENSORPIPE=0 USE_KINETO=0 BUILD_CUSTOM_PROTOBUF=OFF CMAKE_C_FLAGS='${extra_flags}' CMAKE_CXX_FLAGS='${extra_flags}' && python3 setup.py bdist_wheel"

  local whl
  whl=$(run_in_target "ls ${source_dir}/dist/*.whl 2>/dev/null | head -1")
  if [[ -z "${whl}" ]]; then
    fail "编译失败: 未找到 whl 包"
  fi
  log "编译成功: ${whl}"

  run_in_target "pip3 install ${whl} --force-reinstall --no-deps"
  log "PyTorch 安装完成"
}

compile_torch_npu() {
  local source_dir="$1"
  local stage="$2"
  local profile_dir="$3"

  # ABI 前置检查: 确认 PyTorch 是毕昇编译版
  log "检查 PyTorch ABI 一致性..."
  local torch_dir
  torch_dir=$(python3 -c "import torch,os; print(os.path.dirname(torch.__file__))" 2>/dev/null || echo "")
  if [[ -n "${torch_dir}" ]]; then
    local bisheng_check
    bisheng_check=$(run_in_target "readelf -p .comment ${torch_dir}/_C*.so 2>/dev/null | grep -ci bisheng" || echo 0)
    if [[ "${bisheng_check}" -eq 0 ]]; then
      log "⚠️  当前 PyTorch 不是毕昇编译版，ABI 可能不兼容"
      log "⚠️  建议先用毕昇编译器重编译 PyTorch，否则 torch_npu 运行时可能崩溃"
      log "⚠️  如确认要继续编译，请在 5 秒内 Ctrl+C 中断，否则继续..."
      sleep 5
    else
      log "PyTorch 毕昇编译版确认通过"
    fi
  fi

  if [[ "${OPTIMIZE}" == "css_pgo" ]]; then
    # CSSPGO(BRBE)：ci/build.sh 不带 --enable_pgo，伪探针/sample-use 经外部 CFLAGS/CXXFLAGS 注入
    # torch_npu 兼容选项: -O3 -fGNU-compatibility -Wno-non-pod-varargs -D_GLIBCXX_USE_CXX11_ABI=1
    local css_common="-O3 -fGNU-compatibility -Wno-non-pod-varargs -D_GLIBCXX_USE_CXX11_ABI=1 -fno-omit-frame-pointer -fpseudo-probe-for-profiling $(mcpu_flag)"
    local profile_file
    profile_file="$(css_profile_path)"
    local css_flags
    case "${stage}" in
      gen)
        css_flags="${css_common} -g -flto=thin -fuse-ld=lld"
        ;;
      use)
        # use 去 -g，保留 -fpseudo-probe-for-profiling
        css_flags="${css_common} -fprofile-sample-use=${profile_file} -flto=thin -fuse-ld=lld"
        ;;
    esac
    log "编译 torch_npu CSSPGO (${stage})..."
    run_in_target "cd ${source_dir} && git clean -dfx"
    run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ CFLAGS='${css_flags}' CXXFLAGS='${css_flags}' && bash ci/build.sh --python=${PYTHON_VERSION} --enable_lto"
  else
    local pgo_flag=""
    case "${stage}" in
      compile)
        pgo_flag="--enable_lto"
        ;;
      profile_gen)
        pgo_flag="--enable_lto --enable_pgo=1"
        ;;
      profile_use)
        pgo_flag="--enable_lto --enable_pgo=2"
        ;;
    esac

    log "编译 torch_npu (${stage})..."

    run_in_target "cd ${source_dir} && git clean -dfx"
    run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ && bash ci/build.sh --python=${PYTHON_VERSION} ${pgo_flag}"
  fi

  local whl
  whl=$(run_in_target "ls ${source_dir}/dist/*.whl 2>/dev/null | head -1")
  if [[ -z "${whl}" ]]; then
    fail "编译失败: 未找到 whl 包"
  fi
  log "编译成功: ${whl}"

  run_in_target "pip3 install ${whl} --force-reinstall --no-deps"
  log "torch_npu 安装完成"
}

# ── 验证 ─────────────────────────────────────────────────────

verify_component() {
  local component="$1"
  log "验证 ${component}..."

  case "${component}" in
    python)
      run_in_target "${INSTALL_DIR}/bin/python3 --version" || fail "Python 版本验证失败"
      local bisheng_check
      bisheng_check=$(run_in_target "readelf -p .comment ${INSTALL_DIR}/bin/python3 2>/dev/null | grep -ci bisheng" || echo 0)
      if [[ "${bisheng_check}" -eq 0 ]]; then
        log "⚠️  未检测到毕昇编译器标识，可能未使用毕昇编译"
      else
        log "毕昇编译器验证通过"
      fi
      ;;
    pytorch)
      run_in_target 'python3 -c "import torch; print(torch.__version__)"' || fail "PyTorch import 失败"
      local torch_so_dir
      torch_so_dir=$(python3 -c "import torch,os; print(os.path.dirname(torch.__file__))" 2>/dev/null || echo "")
      if [[ -n "${torch_so_dir}" ]]; then
        local bisheng_check
        bisheng_check=$(run_in_target "readelf -p .comment ${torch_so_dir}/_C*.so 2>/dev/null | grep -ci bisheng" || echo 0)
        if [[ "${bisheng_check}" -eq 0 ]]; then
          log "⚠️  未检测到毕昇编译器标识"
        else
          log "毕昇编译器验证通过"
        fi
      fi
      run_in_target 'ldd $(which python3) | grep omp' || log "⚠️  未检测到 libomp.so 链接"
      ;;
    torch_npu)
      run_in_target 'python3 -c "import torch_npu; print(torch_npu.__version__)"' || fail "torch_npu import 失败"
      local npu_so_dir
      npu_so_dir=$(python3 -c "import torch_npu,os; print(os.path.dirname(torch_npu.__file__))" 2>/dev/null || echo "")
      if [[ -n "${npu_so_dir}" ]]; then
        local bisheng_check
        bisheng_check=$(run_in_target "readelf -p .comment ${npu_so_dir}/*.so 2>/dev/null | grep -ci bisheng" || echo 0)
        if [[ "${bisheng_check}" -eq 0 ]]; then
          log "⚠️  未检测到毕昇编译器标识"
        else
          log "毕昇编译器验证通过"
        fi
      fi
      run_in_target 'ldd $(which python3) | grep omp' || log "⚠️  未检测到 libomp.so 链接"
      ;;
  esac
  log "验证完成"
}

# ── 主流程 ───────────────────────────────────────────────────

START_TIME=$(date +%s)

# 参数校验（--check-only 除外，保持旧用法兼容）
if [[ "${CHECK_ONLY}" != true ]]; then
  [[ -n "${COMPONENT}" ]] || { usage >&2; fail "--component is required"; }
  [[ -n "${OPTIMIZE}" ]] || { usage >&2; fail "--optimize is required"; }
  [[ -n "${STAGE}" ]] || { usage >&2; fail "--stage is required"; }
  case "${COMPONENT}" in
    python|pytorch|torch_npu) ;;
    *) fail "未知组件: ${COMPONENT}（可选 python|pytorch|torch_npu）" ;;
  esac
  case "${OPTIMIZE}:${STAGE}" in
    lto:compile|lto_pgo:compile|lto_pgo:profile_gen|lto_pgo:profile_use) ;;
    css_pgo:gen|css_pgo:collect|css_pgo:use) ;;
    *) fail "无效组合: --optimize ${OPTIMIZE} 不支持 --stage ${STAGE}（lto: compile; lto_pgo: compile|profile_gen|profile_use; css_pgo: gen|collect|use）" ;;
  esac
  # 拼入 bash -c 的参数全部先过白名单（防命令注入）；--cmd 是设计上的用户命令，不在此列
  [[ -z "${PID}" ]] || validate_uint --pid "${PID}"
  [[ -z "${BINARY_PATH}" ]] || validate_path --binary "${BINARY_PATH}"
  [[ -z "${CSS_PROFILE}" ]] || validate_path --css-profile "${CSS_PROFILE}"
  [[ -z "${PAGE_SIZE}" ]] || validate_uint --page-size "${PAGE_SIZE}"
  [[ -z "${SOURCE_DIR}" ]] || validate_path --source-dir "${SOURCE_DIR}"
  [[ -z "${INSTALL_DIR}" ]] || validate_path --install-dir "${INSTALL_DIR}"
  # --python-version 会拼入 ci/build.sh --python=${PYTHON_VERSION} 的 bash -c 命令串
  [[ -z "${PYTHON_VERSION}" ]] || [[ "${PYTHON_VERSION}" =~ ^[0-9]+(\.[0-9]+)*$ ]] || fail "参数 --python-version 须为形如 3.10 的版本号，收到: ${PYTHON_VERSION}"
  validate_path --profile-dir "${PROFILE_DIR}"
  [[ "${MCPU}" == "auto" ]] || validate_path --mcpu "${MCPU}"
  validate_uint --duration "${COLLECT_DURATION}"
  if [[ -n "${PID}" && -n "${COLLECT_CMD}" ]]; then
    fail "--pid 与 --cmd 互斥（附加采样与离线包裹采样二选一）"
  fi
  # python 编译类阶段需要 --install-dir（提前报错，避免源码检查/下载后才失败；collect 为只读采样不需要）
  if [[ "${COMPONENT}" == "python" && "${STAGE}" != "collect" && -z "${INSTALL_DIR}" ]]; then
    fail "Python 编译需要 --install-dir"
  fi
fi

# 前置检查
log "=== 前置检查 ==="

# css_pgo collect 分支：只读采样采集，无需源码与编译环境
if [[ "${STAGE}" == "collect" && "${CHECK_ONLY}" != true ]]; then
  if [[ "${DRY_RUN}" != true ]]; then
    check_llvm_profgen || exit 1
  else
    log "dry-run 模式，跳过 llvm-profgen 检查"
  fi
  log "=== CSS profile 采集: ${COMPONENT} ==="
  collect_css_profile || exit 1
  END_TIME=$(date +%s)
  log "=== 采集完成 ($((END_TIME - START_TIME))s) ==="
  output_json "success" \
    "\"css_profile\":\"$(css_profile_path)\"" \
    "\"next_stage\":\"use\"" \
    "\"next_stage_instruction\":\"执行 ak_compile_optimize.sh --component ${COMPONENT} --optimize css_pgo --stage use 使用该 sample profile 重编译\""
  exit 0
fi

if [[ "${DRY_RUN}" != true ]]; then
  check_bisheng || exit 1
else
  log "dry-run 模式，跳过毕昇编译器检查"
fi
detect_python_version
check_container

if [[ "${CHECK_ONLY}" == true ]]; then
  log "=== 环境检查完成 ==="
  output_json "success" "\"bisheng\":\"detected\",\"python_version\":\"${PYTHON_VERSION}\""
  exit 0
fi

# 源码准备
if [[ -z "${SOURCE_DIR}" ]]; then
  SOURCE_DIR="/tmp/ak-build/${COMPONENT}"
  log "未指定源码目录，使用默认: ${SOURCE_DIR}"
  if ! run_in_target "[ -d \"${SOURCE_DIR}\" ]"; then
    CURRENT_VERSION=$(detect_current_version "${COMPONENT}")
    if [[ -z "${CURRENT_VERSION}" ]]; then
      fail "无法检测当前 ${COMPONENT} 版本，请使用 --source-dir 指定源码路径"
    fi
    log "当前版本: ${CURRENT_VERSION}"
    download_source "${COMPONENT}" "${CURRENT_VERSION}" "${SOURCE_DIR}"
  fi
fi
check_source "${SOURCE_DIR}" "${COMPONENT}"

# 磁盘空间检查
case "${COMPONENT}" in
  python)  check_disk_space 2 ;;
  pytorch) check_disk_space 5 ;;
  torch_npu) check_disk_space 3 ;;
esac

# 编译执行
log "=== 编译执行: ${COMPONENT} ${OPTIMIZE} ${STAGE} ==="

case "${COMPONENT}" in
  python)
    if [[ "${OPTIMIZE}" == "css_pgo" && "${STAGE}" == "use" ]]; then
      check_css_profile || exit 1
    fi
    compile_python "${SOURCE_DIR}" "${INSTALL_DIR}" "${STAGE}"
    ;;
  pytorch)
    if [[ "${OPTIMIZE}" == "lto_pgo" && "${STAGE}" == "profile_use" ]]; then
      check_profile "${PROFILE_DIR}" || exit 1
      merge_profile "${PROFILE_DIR}" || exit 1
    fi
    if [[ "${OPTIMIZE}" == "css_pgo" && "${STAGE}" == "use" ]]; then
      check_css_profile || exit 1
    fi
    compile_pytorch "${SOURCE_DIR}" "${STAGE}" "${PROFILE_DIR}"
    ;;
  torch_npu)
    if [[ "${OPTIMIZE}" == "lto_pgo" && "${STAGE}" == "profile_use" ]]; then
      check_profile "${PROFILE_DIR}" || exit 1
      merge_profile "${PROFILE_DIR}" || exit 1
    fi
    if [[ "${OPTIMIZE}" == "css_pgo" && "${STAGE}" == "use" ]]; then
      check_css_profile || exit 1
    fi
    compile_torch_npu "${SOURCE_DIR}" "${STAGE}" "${PROFILE_DIR}"
    ;;
  *)
    fail "未知组件: ${COMPONENT}"
    ;;
esac

# 验证
if [[ "${VERIFY}" == true ]]; then
  log "=== 验证 ==="
  verify_component "${COMPONENT}"
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# 输出结果
NEXT_STAGE=""
NEXT_INSTRUCTION=""
if [[ "${OPTIMIZE}" == "lto_pgo" && "${STAGE}" == "profile_gen" ]]; then
  NEXT_STAGE="profile_use"
  NEXT_INSTRUCTION="请运行模型采集 profile，完成后执行 --stage profile_use"
fi
if [[ "${OPTIMIZE}" == "css_pgo" && "${STAGE}" == "gen" ]]; then
  NEXT_STAGE="collect"
  NEXT_INSTRUCTION="安装 gen 产物并跑真实负载，执行 --stage collect --pid <pid> --binary <gen产物> 采样，再执行 --stage use"
fi

log "=== 完成 (${DURATION}s) ==="
output_json "success" \
  "\"duration_seconds\":${DURATION}" \
  "\"next_stage\":\"${NEXT_STAGE}\"" \
  "\"next_stage_instruction\":\"${NEXT_INSTRUCTION}\""
