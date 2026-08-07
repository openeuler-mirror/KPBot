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

usage() {
  cat <<'EOF'
Usage:
  ak_compile_optimize.sh --component <name> --optimize <type> --stage <stage> [options]

Required:
  --component <name>       组件: python | pytorch | torch_npu
  --optimize <type>        优化类型: lto | lto_pgo
  --stage <stage>          编译阶段:
                             lto 模式: compile
                             lto_pgo 模式: profile_gen | profile_use

Options:
  --source-dir <dir>       源码目录（不指定则自动下载）
  --install-dir <dir>      安装目录（Python 必填）
  --python-version <ver>   Python 版本号（torch_npu 编译用，不指定则自动检测）
  --profile-dir <dir>      PGO profile 目录（默认 /tmp/profile）
  --container <name>       在指定容器内执行（docker exec）
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

  local profraw_count
  profraw_count=$(run_in_target "ls ${profile_dir}/*.profraw 2>/dev/null | wc -l")
  log "找到 ${profraw_count} 个 .profraw 文件"

  local empty_count
  empty_count=$(run_in_target "find ${profile_dir} -name '*.profraw' -size 0 2>/dev/null | wc -l")
  if [[ "${profraw_count}" -eq "${empty_count}" ]]; then
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

  local profdata_size
  profdata_size=$(run_in_target "stat -c %s ${profile_dir}/default.profdata 2>/dev/null || echo 0")
  if [[ "${profdata_size}" -eq 0 ]]; then
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

  log "编译 Python (${stage})..."

  run_in_target "cd ${source_dir} && export CC=clang CXX=clang++ && ./configure --prefix=${install_dir} --with-lto --enable-optimizations"
  run_in_target "cd ${source_dir} && make -j \$(nproc)"
  run_in_target "cd ${source_dir} && make install"

  log "Python 编译安装完成: ${install_dir}"
}

compile_pytorch() {
  local source_dir="$1"
  local stage="$2"
  local profile_dir="$3"

  local extra_flags=""
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

# 前置检查
log "=== 前置检查 ==="
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

# 参数校验
[[ -n "${COMPONENT}" ]] || { usage >&2; fail "--component is required"; }
[[ -n "${OPTIMIZE}" ]] || { usage >&2; fail "--optimize is required"; }
[[ -n "${STAGE}" ]] || { usage >&2; fail "--stage is required"; }

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
    [[ -n "${INSTALL_DIR}" ]] || fail "Python 编译需要 --install-dir"
    compile_python "${SOURCE_DIR}" "${INSTALL_DIR}" "${STAGE}"
    ;;
  pytorch)
    if [[ "${STAGE}" == "profile_use" ]]; then
      check_profile "${PROFILE_DIR}" || exit 1
      merge_profile "${PROFILE_DIR}" || exit 1
    fi
    compile_pytorch "${SOURCE_DIR}" "${STAGE}" "${PROFILE_DIR}"
    ;;
  torch_npu)
    if [[ "${STAGE}" == "profile_use" ]]; then
      check_profile "${PROFILE_DIR}" || exit 1
      merge_profile "${PROFILE_DIR}" || exit 1
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

log "=== 完成 (${DURATION}s) ==="
output_json "success" \
  "\"duration_seconds\":${DURATION}" \
  "\"next_stage\":\"${NEXT_STAGE}\"" \
  "\"next_stage_instruction\":\"${NEXT_INSTRUCTION}\""
