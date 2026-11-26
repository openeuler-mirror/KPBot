#!/usr/bin/env bash
set -euo pipefail

# ak_compile_optimize.sh
# A+K 场景编译优化脚本（Python/PyTorch/torch_npu）
# 使用毕昇编译器进行 LTO/PGO 编译优化
#
# 设计约定（与 references/ak-compiler-playbook.md 保持一致）：
#   1. 毕昇硬门禁：检测（含 profile.d source）→ 绝对路径 CC/CXX → 产物 readelf 标识复核
#   2. 下载加速：submodule insteadOf 镜像重写 + fetchJobs 并行拉取 + 源码缓存
#      （AK_SRC_CACHE）+ pip 内网镜像 + wget 断点续传重试
#   3. PyTorch 编译前置：钉 setuptools/wheel 版本、装 requirements、屏蔽 clang 误报、
#      new_qtensor 强制导出、--enable-shared + rpath 修复
#   4. PGO profile 有效性阈值门（size>50MB 且函数数>100000）后才允许 profile_use
#
# 用法:
#   ak_compile_optimize.sh --component <name> --optimize <type> --stage <stage> [options]
#
# 示例:
#   # 检查环境
#   ak_compile_optimize.sh --check-only
#   # Python LTO+PGO
#   ak_compile_optimize.sh --component python --optimize lto_pgo --stage compile --install-dir /opt/python-ak
#   # PyTorch LTO（指定毕昇版 Python）
#   ak_compile_optimize.sh --component pytorch --optimize lto --stage compile --source-dir /src/pytorch \
#     --python-executable /opt/python-ak/bin/python3
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

PYTHON_EXEC="python3"
BISHENG_PREFIX_OPT=""
INSTALL_BISHENG=""
MAX_JOBS_OPT=""
PIP_INDEX_OPT=""
PIP_TRUSTED="${AK_PIP_TRUSTED_HOST:-}"
PIP_INDEX="${AK_PIP_INDEX:-https://mirrors.huaweicloud.com/repository/pypi/simple}"
# PyPI 源速查表候选（经验库「PyPI 源速查表」同源，按序探测，禁止自行探索其他源）
PIP_CANDIDATES=(
  "https://mirrors.huaweicloud.com/repository/pypi/simple"
  "https://pypi.tuna.tsinghua.edu.cn/simple"
  "https://mirrors.aliyun.com/pypi/simple"
  "https://mirrors.cloud.tencent.com/pypi/simple"
  "https://pypi.org/simple"
)
GITHUB_ACCEL=true
LOG_FILE=""
REUSE_BUILD=false
THINLTO_CACHE="${AK_THINLTO_CACHE:-1}"
CHAIN=""
CHAIN_WORKLOAD=""
CHAIN_RESUME=false
CHAIN_DIR="${AK_CHAIN_DIR:-/tmp/ak-chain}"
CHAIN_PYTHON_EXE=""
BUDGET_MINUTES=120
# css_pgo(BRBE/CSSPGO)
PID=""
COLLECT_CMD=""
BINARY_PATH=""
COLLECT_DURATION=90
PAGE_SIZE=""
CSS_PROFILE=""
css_build_file=""
MCPU=""
AK_CCACHE="${AK_CCACHE:-1}"
ENV_SNAPSHOT_DIR=""
ENV_VERIFY_DIR=""

# git 网络稳健参数：低速判死（60s 内速度 < 1KB/s 视为卡死连接，触发上层重试，
# 避免弱网下 git 无限挂起）；GIT_TERMINAL_PROMPT=0 在各 git 命令处单独前置
GIT_NET_OPTS="-c http.lowSpeedLimit=1024 -c http.lowSpeedTime=60"
# 单次 git 网络操作硬超时（秒）：低速爬行（>1KB/s 但极慢）低速判死抓不住，
# 必须靠墙钟硬上限 failover 到下一镜像；实测 334MB/18s，900s 约 0.37MB/s 下限
GIT_ATTEMPT_TIMEOUT="${AK_GIT_TIMEOUT:-900}"
# 源码缓存目录：首次网络拉取成功后打包源码树（含子模块），同版本重跑、
# chain 断点续跑、重建容器场景直接解包（零网络）。=0 关闭；
# 缓存位于 target 环境（--container 模式即容器内路径）
SRC_CACHE_DIR="${AK_SRC_CACHE:-/tmp/ak-src-cache}"

# 毕昇编译器钉死下载源（经验库「毕昇编译器下载速查表」同源；禁止自行探索其他地址）
BISHENG_TARBALL_URL="https://kunpeng-repo.obs.cn-north-4.myhuaweicloud.com/BiSheng%20Enterprise/BiSheng%20Enterprise%20206.0.0/BiShengCompiler-5.2.0-aarch64-linux.tar.gz"
BISHENG_TARBALL_SHA256="17e2b9b3858a8a2f0591f6f54e0b2bbc1ccbbcb86e4938fb0a10bd3b473a4455"

usage() {
  cat <<'EOF'
Usage:
  ak_compile_optimize.sh --component <name> --optimize <type> --stage <stage> [options]

Required:
  --component <name>       组件: python | pytorch | torch_npu
  --optimize <type>        优化类型: lto | lto_pgo | css_pgo
  --stage <stage>          编译阶段:
                             lto 模式: compile
                             lto_pgo 模式: profile_gen | profile_use
                             （python 组件 PGO 内置，lto_pgo 时 stage=compile）

Options:
  --source-dir <dir>       源码目录（不指定则自动下载到 /tmp/ak-build/<component>）
  --install-dir <dir>      安装目录（Python 必填）
  --python-executable <p>  用于构建/安装的 Python 解释器（默认 python3；
                           编译 PyTorch/torch_npu 必须传毕昇版 Python，
                           --container 时须为容器内路径）
  --python-version <ver>   Python 版本号（torch_npu ci/build.sh 用，不指定则自动检测）
  --bisheng-prefix <dir>   毕昇编译器安装前缀（不指定则依次探测 PATH 与
                           /etc/profile.d/bisheng.sh）
  --install-bisheng <dir>  一键安装毕昇编译器到指定前缀（钉死 OBS 直链 +
                           sha256 校验 + 写 /etc/profile.d/bisheng.sh），
                           不需要 --component 等参数
  --profile-dir <dir>      PGO profile 目录（默认 /tmp/profile）；chain 每次采集新建子目录，
                           实际路径记入 chain state.json 的 profile_run_dir
  --container <name>       在指定容器内执行（docker exec，所有命令均在容器内运行）
  --max-jobs <n>           并行编译任务数（默认 min(nproc, 内存GB/2)，下限 4）
  --pip-index <url>        PyPI 镜像（显式指定则视为唯一源，不可达直接报错；
                           不指定则按速查表候选自动探测选源；
                           可用 AK_PIP_INDEX 环境变量覆盖）
  --pip-trusted-host <h>   pip --trusted-host（私源自签证书时使用，
                           可用 AK_PIP_TRUSTED_HOST 环境变量覆盖）
  --no-github-accelerate   关闭 github 子模块镜像加速（默认经 gitclone.com 重写，
                           失败自动回退直连；子模块默认并行拉取 fetchJobs=8）
  --reuse-build            保留上次构建产物增量续编（跳过 git clean；仅用于
                           编译中断后恢复，或确认配置未变时提速重跑）
  --log-file <file>        追加记录全部输出到该文件（缺省自动落盘
                           /tmp/ak_compile_optimize_<时间戳>.log——防线3指纹
                           审计与 rosegment 自愈依赖构建日志）
  --env-snapshot <dir>     环境指纹快照（解释器/编译器/ABI/关键 so sha256/
                           pip freeze 指纹/CANN 版本/关键环境变量）写入
                           <dir>/env_snapshot.json + pip_freeze.txt
  --env-verify <dir>       当前环境与 <dir>/env_snapshot.json 逐键对比；
                           差异键即"优化前后数据不可复现"的风险来源

Chain 模式（单命令编排全链，含状态检查点/断点续跑/预算护栏）:
  --chain <list>           逗号分隔: python,pytorch[,torch_npu]
  --chain-workload <cmd>   profile 采集负载命令（链含 pytorch/torch_npu 时必填）
  --chain-resume           从上次中断的阶段继续（状态存于 --chain-dir）
  --chain-dir <dir>        链工作目录（默认 /tmp/ak-chain）
  --budget-minutes <n>     预算护栏（默认 120，超支仅告警不中止）
  --check-only             仅探测依赖，不安装软件包或启动编译
  --verify                 编译后验证（毕昇标识复核 + import/功能 smoke test）
  --dry-run                只输出命令不实际执行
  --pid <pid>              css_pgo collect: 附加采样 gen 进程
  --cmd "<command>"        css_pgo collect: 离线包裹短生命周期命令
  --binary <path>          css_pgo collect: 含完整 DWARF 的 gen 产物
  --duration <seconds>     css_pgo collect: 采样时长（默认 90）
  --page-size <size>       css_pgo collect: llvm-profgen -page-size
  --css-profile <file>     css_pgo use: sample profile 路径
  --mcpu <cpu>             编译目标 CPU（默认 auto 探测）
  -h, --help               显示帮助

Environment overrides:
  AK_PYTORCH_VERSION=<ver>       显式指定 PyTorch 版本（未装 torch 时自动下载必需）
  AK_TORCH_NPU_VERSION=<ver>     显式指定 torch_npu 版本对（如 2.1.0-6.0.rc1；
                                 未装 torch_npu 时自动下载必需）
  AK_ALLOW_NON_BISENG_PYTHON=1   允许 PyTorch/torch_npu 前置 Python 非毕昇编译（仅告警）
  AK_PGO_SKIP_THRESHOLD_CHECK=1  跳过 PGO profile 有效性阈值检查（不推荐）
  AK_PIP_INDEX=<url>             PyPI 镜像
  AK_CCACHE=0                    关闭 ccache 编译缓存（单组件与链模式均默认开启；
                                 PGO 场景已设 CCACHE_SLOPPINESS 保证正确失效）
  AK_CCACHE_DIR=<dir>            ccache 缓存目录（链模式默认 <chain-dir>/ccache，
                                 单组件模式默认 /tmp/ak-ccache——重启失效，
                                 长会话建议指到持久卷）
  AK_SRC_CACHE=<dir>             源码缓存目录（默认 /tmp/ak-src-cache，=0 关闭；
                                 首次拉取成功后打包源码树，同版本重跑零网络解包）
   AK_ARCH_FLAGS="<flags>"        架构选项注入（如 -mcpu=native 或
                                  -march=armv9-a+sve2+bf16）：同时进入编译与链接。
                                  产物绑定构建机 ISA，跨型号分发需按鲲鹏微架构
                                  文档复核（0xd01 无 SVE，0xd03/0xd06 有）。
                                  ⚠️ 实测(鲲鹏950/毕昇5.2.0/pytorch2.5.1):
                                  -mcpu=native 使 autograd 路径劣化 3.2×
                                  （MLP 31.5→100.4ms，SVE 自动向量化缺陷家族）。
                                  默认关闭；启用前必须多负载基准验证收益
  AK_GIT_TIMEOUT=<秒>            单次 git 网络操作硬超时（默认 900；防弱网慢速
                                 爬行，超时自动 failover 下一镜像/重试）
  AK_SLOW_NET=accept|skip        慢网门禁：默认 ask——预估超阈值时 TTY 内联询问
                                 （非 TTY 输出 slow_net_confirmation_required 由
                                 Agent 转问用户）；accept=按测速自动放宽超时预算；
                                 skip=关闭门禁
  AK_SLOW_NET_THRESHOLD=<秒>     慢网判定阈值（默认 600s，预估总耗时超过即触发询问）
  AK_PATCH_SCRIPT=<路径>         分版补丁脚本（主脚本不内置分版处理；签名
                                 <脚本> <源码树> <组件> <版本>，须幂等；
                                 --container 时在容器源码树上执行，支持宿主脚本或容器内脚本；
                                 补丁集见 references/patches/）
  AK_PATCH_SCRIPTS=<a.sh:b.sh>   多补丁按序挂载（冒号分隔；torch_npu 2.4.0
                                 需同时挂 missing-includes + abi-env 时使用；
                                 与 AK_PATCH_SCRIPT 可并用，后者最后执行）
  AK_CHAIN_DIR=<dir>             链工作目录
EOF
}

validate_uint() {
  [[ "$2" =~ ^[0-9]+$ ]] || { echo "参数 $1 须为非负整数: $2" >&2; exit 1; }
}

# 可测性：AK_TEST_SOURCE_ONLY=1 时跳过参数解析与日志 exec（source 会劫持
# 测试 shell 的 stdout），仅加载全局变量与函数定义；主流程门在文件底部
if [[ "${AK_TEST_SOURCE_ONLY:-0}" != "1" ]]; then

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component) COMPONENT="${2:?}"; shift 2 ;;
    --optimize) OPTIMIZE="${2:?}"; shift 2 ;;
    --stage) STAGE="${2:?}"; shift 2 ;;
    --source-dir) SOURCE_DIR="${2:?}"; shift 2 ;;
    --install-dir) INSTALL_DIR="${2:?}"; shift 2 ;;
    --python-executable) PYTHON_EXEC="${2:?}"; shift 2 ;;
    --python-version) PYTHON_VERSION="${2:?}"; shift 2 ;;
    --bisheng-prefix) BISHENG_PREFIX_OPT="${2:?}"; shift 2 ;;
    --install-bisheng) INSTALL_BISHENG="${2:?}"; shift 2 ;;
    --profile-dir) PROFILE_DIR="${2:?}"; shift 2 ;;
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --max-jobs) MAX_JOBS_OPT="${2:?}"; shift 2 ;;
    --pip-index) PIP_INDEX_OPT="${2:?}"; shift 2 ;;
    --pip-trusted-host) PIP_TRUSTED="${2:?}"; shift 2 ;;
    --no-github-accelerate) GITHUB_ACCEL=false; shift ;;
    --reuse-build) REUSE_BUILD=true; shift ;;
    --chain) CHAIN="${2:?}"; shift 2 ;;
    --chain-workload) CHAIN_WORKLOAD="${2:?}"; shift 2 ;;
    --chain-resume) CHAIN_RESUME=true; shift ;;
    --chain-dir) CHAIN_DIR="${2:?}"; shift 2 ;;
    --budget-minutes) BUDGET_MINUTES="${2:?}"; shift 2 ;;
    --log-file) LOG_FILE="${2:?}"; shift 2 ;;
    --pid) PID="${2:?}"; [[ "$PID" =~ ^[0-9]+$ && "$PID" =~ [1-9] ]] || { echo "--pid 须为正整数" >&2; exit 1; }; shift 2 ;;
    --cmd) COLLECT_CMD="${2:?}"; shift 2 ;;
    --binary) BINARY_PATH="${2:?}"; shift 2 ;;
    --duration) COLLECT_DURATION="${2:?}"; validate_uint --duration "${COLLECT_DURATION}"; shift 2 ;;
    --page-size) PAGE_SIZE="${2:?}"; validate_uint --page-size "${PAGE_SIZE}"; shift 2 ;;
    --css-profile) CSS_PROFILE="${2:?}"; shift 2 ;;
    --mcpu) MCPU="${2:?}"; [[ "$MCPU" =~ ^[a-zA-Z0-9_.+-]+$ ]] || { echo "无效的 --mcpu" >&2; exit 1; }; shift 2 ;;
    --env-snapshot) ENV_SNAPSHOT_DIR="${2:?}"; shift 2 ;;
    --env-verify) ENV_VERIFY_DIR="${2:?}"; shift 2 ;;
    --check-only) CHECK_ONLY=true; shift ;;
    --verify) VERIFY=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "$CHECK_ONLY" == true && ( -n "$CHAIN" || -n "$INSTALL_BISHENG" ) ]]; then
  echo "--check-only 不能与 --chain 或 --install-bisheng 同用" >&2
  exit 1
fi

# 日志缺省自动落盘：防线3（构建日志编译器指纹审计 audit_build_log）与
# rosegment 自愈重试 retry_build_on_rosegment_overlap 均以 LOG_FILE 存在为
# 前提——历史上用户未传 --log-file 时这两道防线静默失效
if [[ -z "${LOG_FILE}" ]]; then
  LOG_FILE="/tmp/ak_compile_optimize_$(date +%Y%m%d_%H%M%S).log"
  echo "[init] 未指定 --log-file，构建日志自动落盘: ${LOG_FILE}"
fi
mkdir -p "$(dirname "${LOG_FILE}")"
exec > >(tee -a "${LOG_FILE}") 2>&1
# 中断/失败时清理本进程临时文件（set -e 直退路径不再泄漏 /tmp 残留）
trap 'rc=$?; if [[ -n "${css_build_file:-}" ]]; then target_exec rm -f -- "$css_build_file"; fi; rm -rf "/tmp/ak_whl_gate_$$" "/tmp/ak-netprobe.$$" "/tmp/ak-netprobe.$$.log" 2>/dev/null; exit ${rc}' EXIT

fi  # end AK_TEST_SOURCE_ONLY skip（参数解析 + 日志重定向）


# ── Helpers ──────────────────────────────────────────────────

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

fail() { echo "ERROR: $*" >&2; exit 1; }

fail_json() {
  local reason="$1"; shift
  if [[ -n "${COMPONENT}" ]]; then
    output_json "failed" "\"fail_reason\":\"${reason}\"" "$@"
  fi
  echo "ERROR: ${reason}" >&2
  exit 1
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

# Execute argv directly; use bash -c with positional arguments for pipelines or redirects.
target_exec() {
  if [[ "$DRY_RUN" == true ]]; then
    printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
  elif [[ -n "$CONTAINER" ]]; then
    docker exec "$CONTAINER" "$@"
  else
    "$@"
  fi
}

# 组件内按模式选最新 whl。替代裸 `ls dist/*.whl | head -1`（torchnpu-wheel-
# packaging-playbook 实测坑：dist 内多 whl 残留时可能选错旧包）——按组件名
# 前缀（torch-/torch_npu-）收敛，再取 mtime 最新（刚构建产物）
latest_whl() {
  local dir="$1" pattern="$2"
  run_in_target "ls -t ${dir}/${pattern} 2>/dev/null | head -1" || true
}

# 手拼 JSON 字段的值转义（output_json 不做转义；日志内容含 " 或 \ 会产出
# 非法 JSON，Agent 消费方解析失败即丢失恢复指引）
json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ' | sed 's/  */ /g'
}


# 统一重试包装（借鉴 pytorch CI）：全部网络类操作必须经此调用，
# 指数退避（5s/10s/20s），失败信息透传
retry_in_target() {
  local attempts="$1" desc="$2"; shift 2
  local n=1 delay=5 rc
  while true; do
    run_in_target "$@"
    rc=$?
    if (( rc == 0 )); then
      return 0
    fi
    if (( n >= attempts )); then
      log "✗ ${desc}: ${attempts} 次尝试均失败"
      return "${rc}"
    fi
    log "⚠️  ${desc} 第 ${n}/${attempts} 次失败，${delay}s 后重试..."
    sleep "${delay}"
    n=$((n+1)); delay=$((delay*2))
  done
}

dnf_install_retry() {
  local n=1 delay=5
  while true; do
    if dnf install -y -q "$@" >/dev/null 2>&1; then
      return 0
    fi
    if (( n >= 3 )); then
      return 1
    fi
    log "  dnf 安装失败，${delay}s 后重试..."
    sleep "${delay}"
    n=$((n+1)); delay=$((delay*2))
  done
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

# target bash 每次都是非登录 shell（docker exec/bash -c 不加载 profile.d），
# 所有 target 命令统一前置该环境前缀，保证毕昇在 PATH 中
build_tgt_env() {
  if [[ -n "${BISHENG_PREFIX_OPT}" ]]; then
    TGT_ENV=$(printf 'export PATH=%s/bin:$PATH; export LD_LIBRARY_PATH=%s/lib:${LD_LIBRARY_PATH:-};' \
      "${BISHENG_PREFIX_OPT}" "${BISHENG_PREFIX_OPT}")
  else
    TGT_ENV="source /etc/profile.d/bisheng.sh 2>/dev/null;"
  fi
}

# 稳健统计二进制 .comment 段中的 bisheng 标识次数
# 修复点：grep -c 无匹配时输出 "0" 且退出码为 1，
# 旧写法 `$(cmd | grep -ci bisheng || echo 0)` 会捕获到 "0\n0"，
# 导致 [[ -eq ]] 报错走 else 分支，把 GCC 产物误判为毕昇编译（假阳性）
count_bisheng_marks() {
  local out
  out=$(run_in_target "readelf -p .comment ${1} 2>/dev/null | grep -ci bisheng" || true)
  out=$(printf '%s' "${out}" | head -n1)
  [[ "${out}" =~ ^[0-9]+$ ]] || out=0
  printf '%s' "${out}"
}

# ── PyPI 源选择 ──────────────────────────────────────────────

probe_pip_index() {
  local url="$1"
  local out
  out=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c \"import urllib.request as u; import sys
try: sys.stdout.write(str(u.urlopen('${url}', timeout=8).status))
except Exception: sys.stdout.write('0')\"" 2>/dev/null || true)
  [[ "${out}" =~ ^(200|301|302|308)$ ]]
}

# 显式 --pip-index = 唯一权威源；否则按候选表自动探测，首个可达者胜出
select_pip_index() {
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] PyPI 源探测（当前值: ${PIP_INDEX_OPT:-${PIP_INDEX}}）"
    return 0
  fi
  local candidates=()
  if [[ -n "${PIP_INDEX_OPT}" ]]; then
    candidates=("${PIP_INDEX_OPT}")
    log "用户显式指定 PyPI 源（唯一权威源）: ${PIP_INDEX_OPT}"
  else
    candidates=("${PIP_CANDIDATES[@]}")
  fi
  local idx
  for idx in "${candidates[@]}"; do
    log "探测 PyPI 源: ${idx}"
    if probe_pip_index "${idx}"; then
      PIP_INDEX="${idx}"
      log "选用 PyPI 源: ${PIP_INDEX}"
      return 0
    fi
    log "不可达，尝试下一个候选"
  done
  fail_json "pip_index_unreachable" \
    "\"candidates\":\"${candidates[*]}\"" \
    "\"recovery_steps\":[\"环境内有私源/Nexus: 用 --pip-index 显式指定\",\"自签证书私源: 加 --pip-trusted-host <host>\",\"离线环境: 在联网机 pip download 离线包，目标机 pip install --no-index --find-links <离线目录>\"]"
}

pip_install() {
  # $@ = pip 参数；环境变量 PIP_CWD 可选指定工作目录
  # pip 自身 --retries 只覆盖下载重试；此处对整个安装命令做外层重试（指数退避）
  local trusted="" cd_prefix="" n=1 delay=5
  [[ -n "${PIP_TRUSTED}" ]] && trusted="--trusted-host ${PIP_TRUSTED}"
  [[ -n "${PIP_CWD:-}" ]] && cd_prefix="cd '${PIP_CWD}' && "
  while true; do
    if run_in_target "${cd_prefix}${TGT_ENV} ${PYTHON_EXEC} -m pip install -q -i ${PIP_INDEX} ${trusted} --timeout 30 --retries 2 $*"; then
      return 0
    fi
    if (( n >= 3 )); then
      return 1
    fi
    log "⚠️  pip 安装第 ${n}/3 次失败（PyPI 源: ${PIP_INDEX}），${delay}s 后重试..."
    sleep "${delay}"
    n=$((n+1)); delay=$((delay*2))
  done
}

# ── 毕昇编译器一键安装 ───────────────────────────────────────

install_bisheng() {
  local prefix="$1"
  local tarball="/tmp/BiShengCompiler.tar.gz"

  log "下载毕昇编译器（钉死 OBS 直链，禁止探索其他地址）..."
  log "URL: ${BISHENG_TARBALL_URL}"
  retry_in_target 3 "毕昇编译器下载" "wget -c -q --timeout=60 '${BISHENG_TARBALL_URL}' -O ${tarball}"

  if [[ "${DRY_RUN}" != true ]]; then
    log "校验 sha256 ..."
    local actual
    actual=$(run_in_target "sha256sum ${tarball}" | awk '{print $1}')
    if [[ "${actual}" != "${BISHENG_TARBALL_SHA256}" ]]; then
      fail_json "bisheng_checksum_mismatch" \
        "\"expected\":\"${BISHENG_TARBALL_SHA256}\"" \
        "\"actual\":\"${actual}\"" \
        "\"recovery_steps\":[\"重新下载: wget -c（可能下载不完整）\",\"若重试仍不匹配，说明官方更新了包（同 URL 模式探测新版本），更新脚本内 BISHENG_TARBALL_SHA256\"]"
    fi
    log "sha256 校验通过"
  fi

  log "解压安装到 ${prefix} ..."
  run_in_target "mkdir -p ${prefix} && tar -xzf ${tarball} -C ${prefix} --strip-components=1"

  log "写入 /etc/profile.d/bisheng.sh ..."
  run_in_target "cat > /etc/profile.d/bisheng.sh << BISEOF
export PATH=${prefix}/bin:\$PATH
export LD_LIBRARY_PATH=${prefix}/lib:\${LD_LIBRARY_PATH:-}
export CC=clang
export CXX=clang++
BISEOF
source /etc/profile.d/bisheng.sh"

  if [[ "${DRY_RUN}" != true ]]; then
    local version_line
    version_line=$(run_in_target "${TGT_ENV} clang --version 2>/dev/null" | grep -i bisheng | head -1 || true)
    [[ -n "${version_line}" ]] || fail_json "bisheng_install_verify_failed" \
      "\"recovery_steps\":[\"检查 ${prefix}/bin/clang 是否存在\",\"手动执行: source /etc/profile.d/bisheng.sh && clang --version\"]"
    log "安装验证通过: ${version_line}"
    log "libomp: $(run_in_target "ls ${prefix}/lib/libomp.so" 2>/dev/null || echo '未找到（运行时需要）')"
  fi
  log "=== 毕昇编译器安装完成 ==="
}

# ── 毕昇编译器检测 ───────────────────────────────────────────

resolve_bisheng() {
  log "检测毕昇编译器..."
  local candidates candidate clang_path=""

  if [[ -n "${BISHENG_PREFIX_OPT}" ]]; then
    candidates="${BISHENG_PREFIX_OPT}/bin/clang"
  else
    # 探测顺序：source profile.d 后的 PATH → 继承的 PATH
    # 陷阱：系统自带 LLVM clang（如 openEuler 17.0.x）不是毕昇，
    #       非 shell 环境（docker exec/CI/cron）不会加载 profile.d，必须先 source
    candidates=$(run_in_target "${TGT_ENV} command -v clang 2>/dev/null; command -v clang 2>/dev/null" || true)
  fi

  while IFS= read -r candidate; do
    [[ -z "${candidate}" ]] && continue
    if run_in_target "${candidate} --version 2>/dev/null" | grep -qi "bisheng"; then
      clang_path="${candidate}"
      break
    fi
  done <<< "${candidates}"

  if [[ -z "${clang_path}" ]]; then
    output_json "failed" \
      "\"fail_reason\":\"bisheng_not_found\"" \
      "\"diagnostics\":{\"message\":\"未检测到毕昇编译器（系统自带的 clang 不是毕昇编译器）\",\"checked\":[\"PATH 中的 clang\",\"source /etc/profile.d/bisheng.sh 后的 PATH\"${BISHENG_PREFIX_OPT:+,\"${BISHENG_PREFIX_OPT}/bin/clang\"}],\"recovery_steps\":[\"若已安装：确认 /etc/profile.d/bisheng.sh 存在且 PATH/LD_LIBRARY_PATH 正确，或使用 --bisheng-prefix <安装前缀>\",\"若未安装：参考 playbook 安装章节（华为云 OBS 下载约 835MB）\",\"安装后执行: source /etc/profile.d/bisheng.sh && clang --version | grep bisheng 确认\"]}" \
      "\"retryable\":false"
    echo "ERROR: bisheng_not_found" >&2
    return 1
  fi

  BISHENG_CLANG="${clang_path}"
  BISHENG_CXX="${clang_path%clang}clang++"
  BISHENG_PREFIX="$(dirname "$(dirname "${clang_path}")")"

  local version_line
  version_line=$(run_in_target "${BISHENG_CLANG} --version 2>/dev/null" | grep -i bisheng | head -1 || true)
  log "毕昇编译器: ${version_line}"
  log "毕昇前缀: ${BISHENG_PREFIX}"

  # 编译器逃逸防线 1：target shell 的 PATH 显式前置毕昇 bin，
  # 任何子构建裸调 clang/gcc/cc 时不再依赖 profile.d 是否正确
  BISHENG_VERSION=$(printf '%s' "${version_line}" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -1 || true)
  TGT_ENV=$(printf 'export PATH=%s/bin:$PATH; export LD_LIBRARY_PATH=%s/lib:${LD_LIBRARY_PATH:-};' \
    "${BISHENG_PREFIX}" "${BISHENG_PREFIX}")
  # ccache 包装（链模式注入）：CC/CXX 指向包装器，编译缓存命中加速重试
  if [[ -n "${AK_CCACHE_WRAP_DIR:-}" ]] && target_exec test -x "${AK_CCACHE_WRAP_DIR}/clang"; then
    BISHENG_CLANG="${AK_CCACHE_WRAP_DIR}/clang"
    BISHENG_CXX="${AK_CCACHE_WRAP_DIR}/clang++"
    TGT_ENV=$(printf 'export PATH=%s:%s/bin:$PATH; export LD_LIBRARY_PATH=%s/lib:${LD_LIBRARY_PATH:-};' \
      "${AK_CCACHE_WRAP_DIR}" "${BISHENG_PREFIX}" "${BISHENG_PREFIX}")
    log "ccache 编译缓存包装生效"
  fi
  log "逃逸防线: PATH 前置 ${BISHENG_PREFIX}/bin"

  if [[ "${DRY_RUN}" != true ]] && ! run_in_target "ls ${BISHENG_PREFIX}/lib/libomp.so >/dev/null 2>&1"; then
    log "⚠️  未找到 ${BISHENG_PREFIX}/lib/libomp.so，LTO 产物运行时将需要它，请确认安装完整"
  fi
}

# ── 编译器逃逸审计 ───────────────────────────────────────────

# 防线 0：编译前强制门禁——在真实构建 shell（应用 TGT_ENV 之后）中验证
# clang 实际解析到毕昇。生产环境脚本（CANN set_env.sh/profile.d/bashrc）未纳入
# 或中途重置 PATH/CC 时，在此拦截而非静默用系统编译器
assert_bisheng_active() {
  [[ "${DRY_RUN}" == true ]] && return 0
  local live_clang live_ver
  live_clang=$(run_in_target "${TGT_ENV} command -v clang 2>/dev/null" | head -1 || true)
  live_ver=$(run_in_target "${TGT_ENV} clang --version 2>/dev/null" | head -1 || true)
  if [[ -z "${live_ver}" ]] || ! printf '%s' "${live_ver}" | grep -qi bisheng; then
    fail_json "bisheng_env_escape" \
      "\"live_clang\":\"${live_clang}\"" \
      "\"live_version\":\"${live_ver}\"" \
      "\"expected\":\"${BISHENG_CLANG}\"" \
      "\"recovery_steps\":[\"检查 /etc/profile.d/bisheng.sh 是否存在且指向 ${BISHENG_PREFIX}\",\"环境依赖脚本(set_env.sh/bashrc)重置 PATH 时，确保毕昇在其后追加而非被覆盖\",\"或显式 --bisheng-prefix 走绝对路径注入\",\"用 --check-only 复核\"]"
  fi
  if [[ -n "${live_clang}" && "${live_clang}" != "${BISHENG_CLANG}" ]]; then
    fail_json "bisheng_env_escape" \
      "\"live_clang\":\"${live_clang}\"" \
      "\"expected\":\"${BISHENG_CLANG}\"" \
      "\"recovery_steps\":[\"PATH 中存在另一个更靠前的 clang（如 /usr/bin/clang 系统 LLVM）\",\"检查环境脚本追加顺序，或直接 --bisheng-prefix 绝对路径注入\"]"
  fi
  log "逃逸防线0通过: clang 实际解析 = ${live_clang}"
}

# 防线 2：逐文件 .comment 审计。聚合计数会掩盖单个 .so 逃逸（其他文件计数
# 补齐总数），必须每个构建产物都含 bisheng 标识；libgomp 等上游预捆绑
# 库由 GCC 构建属正常，仅告警
audit_compiler_marks() {
  local glob="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    return 0
  fi
  local out escaped="" f line id
  out=$(run_in_target "for f in ${glob}; do [ -f \"\$f\" ] || continue; readelf -h \"\$f\" >/dev/null 2>&1 || continue; m=\$(readelf -p .comment \"\$f\" 2>/dev/null | grep -ci bisheng || true); if [ \"\$m\" -eq 0 ]; then echo \"\$f\"; fi; done" || true)
  printf '%s' "${out}"
}

handle_escaped_artifacts() {
  local escaped="$1" strict="$2"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 逐文件毕昇标识审计（跳过）"
    return 0
  fi
  [[ -z "${escaped// /}" ]] && return 0
  local real="" libgomp="" f
  for f in ${escaped}; do
    # libgomp/libgfortran/libomp 是运行时组件（前两者为 GCC 运行时；libomp
    # 实测毕昇自带版亦为 GCC 构建——LLVM 运行时组件惯例），非编译器逃逸
    case "$(basename "${f}")" in
      libgomp*|libgfortran*|libomp*)
        libgomp+="$(basename "${f}") "
        ;;
      *)
        real+="${f} "
        ;;
    esac
  done
  if [[ -n "${libgomp// /}" ]]; then
    log "⚠️  上游预捆绑库(GCC 构建,属正常): ${libgomp}"
  fi
  if [[ -n "${real// /}" ]]; then
    local ids
    ids=$(run_in_target "for f in ${real}; do echo \"\$f => \$(readelf -p .comment \"\$f\" 2>/dev/null | grep -v -E 'String dump|^  \[' | head -1)\"; done" || true)
    log "编译器逃逸检出: ${real}"
    log "逃逸产物编译器身份: ${ids}"
    if [[ "${strict}" == true && "${AK_ALLOW_MIXED_COMPILER:-0}" != "1" ]]; then
      fail_json "compiler_escape_detected" \
        "\"escaped_artifacts\":\"${real}\"" \
        "\"recovery_steps\":[\"检查构建日志中这些产物由谁编译（通常为子构建 CC/CXX 未继承或 CMakeCache 残留）\",\"确认 PATH 前置毕昇 bin 生效\",\"rm -rf 源码 build 目录后重试\",\"确要继续（不推荐）: AK_ALLOW_MIXED_COMPILER=1\"]"
    fi
  fi
  return 0
}

# 防线 3：构建日志编译器指纹审计。cmake 会打印实际使用的编译器身份，
# "compiler identification is GNU" 即 gcc 逃逸实锤；Clang 版本与毕昇不符也提示
audit_build_log() {
  [[ -n "${LOG_FILE}" && -f "${LOG_FILE}" ]] || return 0
  if [[ "${DRY_RUN}" == true ]]; then
    return 0
  fi
  local gnu_hits clang_hits
  gnu_hits=$(grep -E 'compiler identification is GNU' "${LOG_FILE}" | sort -u | head -3 || true)
  if [[ -n "${gnu_hits}" ]]; then
    log "⚠️  构建日志检出 GNU 编译器指纹（gcc 逃逸实锤）: ${gnu_hits}"
    if [[ "${AK_ALLOW_MIXED_COMPILER:-0}" != "1" ]]; then
      fail_json "compiler_escape_in_log" \
        "\"evidence\":\"${gnu_hits}\"" \
        "\"recovery_steps\":[\"定位打印该行的子构建，检查其 CC/CXX 是否继承\",\"配合 --log-file 复现并检索上下文\",\"确要继续（不推荐）: AK_ALLOW_MIXED_COMPILER=1\"]"
    fi
  fi
  if [[ -n "${BISHENG_VERSION}" ]]; then
    clang_hits=$(grep -E "compiler identification is Clang" "${LOG_FILE}" | grep -v "${BISHENG_VERSION}" | sort -u | head -3 || true)
    [[ -n "${clang_hits}" ]] && log "⚠️  构建日志检出非毕昇版本 Clang 指纹: ${clang_hits}"
  fi
  return 0
}

# ── 前置检查 ─────────────────────────────────────────────────

detect_python_version() {
  if [[ -z "${PYTHON_VERSION}" ]]; then
    if [[ "${DRY_RUN}" == true ]]; then
      log "[dry-run] 将执行: ${TGT_ENV} ${PYTHON_EXEC} -c 'import sys; print(version)' 检测 Python 版本"
      PYTHON_VERSION="dry-run"
      log "Python 版本: ${PYTHON_VERSION} (${PYTHON_EXEC})"
      return 0
    fi
    PYTHON_VERSION=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])' 2>/dev/null" || true)
    if [[ -z "${PYTHON_VERSION}" ]]; then
      if [[ "${CHECK_ONLY}" == true ]]; then
        PYTHON_VERSION="unknown"
        log "⚠️  无法检测 Python 版本（${PYTHON_EXEC} 不可用？）"
        return 0
      fi
      fail "无法检测 Python 版本（${PYTHON_EXEC}），请使用 --python-version 或 --python-executable 指定"
    fi
  fi
  log "Python 版本: ${PYTHON_VERSION} (${PYTHON_EXEC})"
}

check_container() {
  if [[ -n "${CONTAINER}" ]]; then
    if [[ "${DRY_RUN}" == true ]]; then
      log "[dry-run] 将在容器 ${CONTAINER} 内执行（跳过存在性检查）"
      return 0
    fi
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
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 检查源码目录与子模块完整性: ${dir}"
    return 0
  fi
  log "检查源码目录: ${dir}"

  run_in_target "[ -d \"${dir}\" ]" || fail "源码目录不存在: ${dir}"

  log "检查子模块完整性..."
  local sub_status
  sub_status=$(run_in_target "cd ${dir} && git submodule status 2>/dev/null || echo 'NO_GIT'" || true)
  if [[ "${sub_status}" == *"NO_GIT"* ]]; then
    case "${component}" in
      pytorch|torch_npu)
        fail "源码目录不是 git 仓库（${dir}）。ZIP/源码包解压缺少 .git 与子模块，无法编译，请用 git clone 重新获取源码"
        ;;
      *)
        log "非 git 仓库，跳过子模块检查"
        ;;
    esac
  else
    # 完整性以"实际文件存在性"为准，不信任 git submodule status 文本：
    # 中断的 checkout 会留下 status 无 - 前缀但目录为空的假完整态（实测
    # pybind11/psimd 均如此），且全量 update 每次联网重拉造成编译反复等待
    local paths p missing="" dash_list still
    paths=$(run_in_target "cd ${dir} && git submodule status --recursive 2>/dev/null | awk '{print \$2}'" || true)
    dash_list=$(printf '%s\n' "${sub_status}" | awk '$1 ~ /^-/ {print $2}' 2>/dev/null || true)
    for p in ${paths} ${dash_list}; do
      [[ -z "${p}" ]] && continue
      run_in_target "cd ${dir} && [ -d '${p}' ] && [ -n \"\$(ls -A '${p}' 2>/dev/null)\" ]" >/dev/null 2>&1 || missing+="${p} "
    done
    # 顶层子模块 HEAD==gitlink 校验（零网络）：--depth 1 浅克隆在镜像缺
    # pinned SHA 时会把工作树静默留在默认分支 tip（实测 pybind11 停在旧
    # master），文件存在性检查发现不了，编译出错误版本
    local top_paths sha_missing
    top_paths=$(run_in_target "cd ${dir} && git submodule status 2>/dev/null | awk '{print \$2}'" || true)
    sha_missing=$(submodule_mismatched_paths "${dir}" "${top_paths}")
    if [[ -n "${sha_missing// /}" ]]; then
      log "检测到版本不符子模块（HEAD != 父仓 gitlink，疑似镜像缺 SHA 被检出分支 tip）: ${sha_missing}"
      missing+="${sha_missing} "
    fi
    if [[ -z "${missing// /}" ]]; then
      log "子模块文件存在性与版本一致性检查通过（零网络操作）"
    else
      log "检测到不完整/版本不符子模块: ${missing}"
      update_submodules "${dir}" "${missing}"
      still=""
      local recheck
      recheck=$(submodule_mismatched_paths "${dir}" "${missing}")
      still="${recheck}"
      for p in ${missing}; do
        run_in_target "cd ${dir} && [ -d '${p}' ] && [ -n \"\$(ls -A '${p}' 2>/dev/null)\" ]" >/dev/null 2>&1 || still+="${p} "
      done
      [[ -z "${still// /}" ]] || fail "子模块定向修复后仍不完整: ${still}（镜像/网络问题；可 --no-github-accelerate 或检查网络后重试）"
      log "子模块定向修复完成"
    fi
  fi

  case "${component}" in
    pytorch)
      run_in_target "[ -d \"${dir}/aten/src\" ] && [ -d \"${dir}/third_party\" ] && [ -f \"${dir}/requirements.txt\" ]" || fail "源码预检失败: 缺少关键目录或文件（aten/src、third_party、requirements.txt）"
      ;;
    torch_npu)
      run_in_target "[ -f \"${dir}/ci/build.sh\" ]" || fail "源码预检失败: 缺少 ci/build.sh"
      ;;
  esac
  log "源码完整性检查通过"
}

check_disk_space() {
  local required_gb="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 磁盘空间检查（需要 ${required_gb}GB）"
    return 0
  fi
  local target_path="${SOURCE_DIR:-/tmp}"

  # 修复点：源码目录尚不存在时 df 会失败，旧实现把空值算成 0GB 报"磁盘空间不足"
  if [[ -n "${SOURCE_DIR}" && ! -d "${SOURCE_DIR}" ]]; then
    target_path="$(dirname "${SOURCE_DIR}")"
    [[ -d "${target_path}" ]] || target_path="/tmp"
  fi

  local df_out available_kb available_gb
  if ! df_out=$(run_in_target "df -Pk ${target_path} 2>/dev/null" | tail -1); then
    log "⚠️  无法检查磁盘空间（df 不可用），跳过检查"
    return 0
  fi
  available_kb=$(printf '%s\n' "${df_out}" | awk '{print $4}')
  [[ "${available_kb}" =~ ^[0-9]+$ ]] || { log "⚠️  磁盘空间解析失败，跳过检查"; return 0; }
  available_gb=$((available_kb / 1024 / 1024))
  log "磁盘空间检查: ${target_path} 可用 ${available_gb}GB, 需要 ${required_gb}GB"
  if [[ "${available_gb}" -lt "${required_gb}" ]]; then
    fail "磁盘空间不足: 可用 ${available_gb}GB, 编译需要至少 ${required_gb}GB, 请清理磁盘后重试"
  fi
}

# ── 源码获取（全部在 target 环境内执行） ─────────────────────

# 修复点：旧实现用宿主机命令下载源码，--container 模式下源码落在宿主机、
# 编译却在容器内找目录，必然失败。现统一在 target 环境执行。
# 提速点（网络拉取源码是编译前最大的等待项，实测基线见 playbook）：
#   1. 子模块分层镜像收敛拉取：gitcode/ghfast 逐仓 SHA 级验证（缓存 24h）→
#      gitclone.com → github 直连，fetchJobs=8 并行（串行 35 个子模块约 30 分钟是主瓶颈）
#   2. pytorch 主仓三级克隆回退链（实测：gitee 18s → gitcode 39s → github 33s）
#      每次尝试硬超时（AK_GIT_TIMEOUT，默认 900s）防弱网慢速爬行拖到小时级
#   3. 子模块检出后强制 HEAD==gitlink 校验（防镜像缺 pinned SHA 时静默检出
#      分支 tip 的错误版本）
#   4. 源码缓存：首次拉取成功后打包到 SRC_CACHE_DIR，同版本重跑直接解包
#   5. 防悬挂：GIT_TERMINAL_PROMPT=0（私有子模块缺凭据立即失败而非挂死）
#      + http.lowSpeed 低速判死（弱网卡死连接触发上层重试而非无限等待）

# 缓存包路径；AK_SRC_CACHE=0 返回 1 表示缓存功能关闭
cache_tarball() {
  if [[ "${SRC_CACHE_DIR}" == "0" ]]; then
    return 1
  fi
  printf '%s/%s-%s.tar' "${SRC_CACHE_DIR}" "$1" "$2"
}

# 缓存命中 → 解包到目标目录返回 0；未命中/关闭/dry-run 返回 1（走网络拉取）。
# 解包出的残缺树由后续 check_source 文件存在性检查兜底定向补拉
cache_restore() {
  local component="$1" version="$2" target_dir="$3"
  if [[ "${DRY_RUN}" == true || "${SRC_CACHE_DIR}" == "0" ]]; then
    return 1
  fi
  local tarball
  tarball=$(cache_tarball "${component}" "${version}") || return 1
  run_in_target "[ -f '${tarball}' ]" || return 1
  log "命中源码缓存: ${tarball}（跳过网络拉取）"
  run_in_target "mkdir -p $(dirname "${target_dir}")" || return 1
  if ! run_in_target "tar -xf '${tarball}' -C $(dirname "${target_dir}")"; then
    log "⚠️  缓存解包失败，删除损坏缓存后走网络拉取"
    run_in_target "rm -f '${tarball}'" || true
    return 1
  fi
  run_in_target "[ -d '${target_dir}' ]" || return 1
  return 0
}

# 拉取成功后打包缓存（先写 .tmp 再原子改名，避免留下半截缓存被误命中；
# 打包失败仅告警，不影响编译主流程）
cache_store() {
  local component="$1" version="$2" target_dir="$3"
  if [[ "${DRY_RUN}" == true || "${SRC_CACHE_DIR}" == "0" ]]; then
    return 0
  fi
  local tarball
  tarball=$(cache_tarball "${component}" "${version}") || return 0
  log "打包源码缓存: ${tarball}（AK_SRC_CACHE=0 可关闭）"
  run_in_target "mkdir -p '${SRC_CACHE_DIR}'" || { log "⚠️  缓存目录创建失败，跳过缓存"; return 0; }
  if ! run_in_target "tar -cf '${tarball}.tmp' -C $(dirname "${target_dir}") $(basename "${target_dir}") && mv '${tarball}.tmp' '${tarball}'"; then
    log "⚠️  源码缓存打包失败（磁盘不足？），不影响编译流程"
    run_in_target "rm -f '${tarball}.tmp'" || true
  fi
  return 0
}

# ── 慢网确认门禁（测速 → 预估 → 用户确认 → 自动配参） ─────────
# 需求：网络异常缓慢时，skill 主动告知预估耗时并询问用户是否接受；
# 用户接受后无需手动算参，脚本按测得速度自动放宽超时预算。
# 状态机（AK_SLOW_NET）：
#   ask(默认):  预估超阈值 → TTY 下内联询问；非 TTY（Agent 编排）输出
#               slow_net_confirmation_required 结构化失败，由 Agent 转问用户，
#               用户接受后 AK_SLOW_NET=accept 重跑
#   accept:     跳过询问，按测速自动计算 GIT_ATTEMPT_TIMEOUT（total/速度×1.5+120s，
#               夹在 [900, 21600]）
#   skip:       跳过门禁，维持默认预算（资深用户自管参数）
# 阈值: AK_SLOW_NET_THRESHOLD（默认 600s）；缓存已命中则不触发门禁（零网络）。
# 数据量粗估为版本无关模型（pytorch≈1600MB 含子模块、torch_npu≈120MB），
# 实际随版本浮动，预估仅供参考。
# 测速: 对首选源做 depth-1 探针克隆，收满 30MiB 或 45s 早停，解析 git
# progress 的实时速率（KiB/s）——fast 网几秒结束，慢网跑满窗口。

AK_SLOW_NET="${AK_SLOW_NET:-ask}"
AK_SLOW_NET_THRESHOLD="${AK_SLOW_NET_THRESHOLD:-600}"

net_probe_speed() {
  local url="$1"
  local dir="/tmp/ak-netprobe.$$" plog="/tmp/ak-netprobe.$$.log"
  local received_mib=0 rate_kbs=0 last_seg=""
  rm -rf "${dir}" "${plog}"
  ( GIT_TERMINAL_PROMPT=0 timeout 45 git clone --progress --depth 1 "${url}" "${dir}" 2>&1 ) > "${plog}" &
  local pid=$!
  local i seg
  for i in $(seq 1 45); do
    sleep 1
    kill -0 "${pid}" 2>/dev/null || break
    last_seg=$(tr '\r' '\n' < "${plog}" 2>/dev/null | grep -oE "Receiving objects: *[0-9]+% \([0-9]+/[0-9]+\), +[0-9.]+ (MiB|KiB) \| +[0-9.]+ (MiB|KiB)/s" | tail -1)
    [[ -z "${last_seg}" ]] && continue
    received_mib=$(printf '%s' "${last_seg}" | sed -E 's/.*\), +([0-9.]+) (MiB|KiB) \|+.*/\1 \2/' | awk '{r=$1; if ($2=="KiB") r=r/1024; printf "%.1f", r}')
    if awk "BEGIN{exit !(${received_mib} >= 30)}"; then
      break
    fi
  done
  kill "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  last_seg=$(tr '\r' '\n' < "${plog}" 2>/dev/null | grep -oE "\| +[0-9.]+ (MiB|KiB)/s" | tail -1)
  if [[ -n "${last_seg}" ]]; then
    rate_kbs=$(printf '%s' "${last_seg}" | sed -E 's/\| +([0-9.]+) (MiB|KiB)\/s/\1 \2/' | awk '{r=$1; if ($2=="MiB") r=r*1024; printf "%d", r}')
  fi
  rm -rf "${dir}" "${plog}"
  printf '%s' "${rate_kbs}"
}

slow_net_gate() {
  local component="$1" version="$2"
  [[ "${AK_SLOW_NET}" == "skip" ]] && return 0
  [[ "${DRY_RUN}" == true ]] && { log "[dry-run] 慢网门禁跳过（AK_SLOW_NET=${AK_SLOW_NET}）"; return 0; }
  # 缓存已就绪 = 零网络，不触发门禁
  if cache_tarball "${component}" "${version}" >/dev/null 2>&1 && run_in_target "[ -f '$(cache_tarball "${component}" "${version}")' ]" >/dev/null 2>&1; then
    log "源码缓存已就绪，跳过慢网门禁"
    return 0
  fi
  local probe_url=""
  local est_mb=0
  case "${component}" in
    pytorch)   probe_url="https://gitee.com/mirrors/pytorch.git";     est_mb=1600 ;;
    torch_npu) probe_url="https://gitee.com/ascend/pytorch.git";      est_mb=120  ;;
    *) return 0 ;;
  esac
  log "网络测速: 对首选源 ${probe_url} 发起探针（30MiB 早停 / 45s 上限）..."
  local speed_kbs
  speed_kbs=$(net_probe_speed "${probe_url}")
  [[ "${speed_kbs}" =~ ^[0-9]+$ ]] || speed_kbs=0
  local est_seconds=-1 est_desc=""
  if (( speed_kbs > 0 )); then
    est_seconds=$(( est_mb * 1024 / speed_kbs ))
    est_desc="测得 ${speed_kbs} KB/s，预估总传输 ${est_mb}MB（粗估）≈ $(( est_seconds / 60 )) 分钟"
  else
    est_desc="首选源 45s 内无可测流量（不可达或极慢）"
  fi
  log "网络评估: ${est_desc}"
  if (( speed_kbs > 0 && est_seconds <= AK_SLOW_NET_THRESHOLD )); then
    return 0
  fi
  if [[ "${AK_SLOW_NET}" == "accept" ]]; then
    local auto_t=900
    if (( speed_kbs > 0 )); then
      auto_t=$(( est_seconds * 3 / 2 + 120 ))
    else
      auto_t=7200
    fi
    (( auto_t < 900 )) && auto_t=900
    (( auto_t > 21600 )) && auto_t=21600
    GIT_ATTEMPT_TIMEOUT="${auto_t}"
    log "AK_SLOW_NET=accept：超时预算自动放宽为 ${GIT_ATTEMPT_TIMEOUT}s（基于测速预估）"
    return 0
  fi
  # ask：TTY 内联询问；非 TTY 输出结构化确认需求（Agent 转问用户后 accept 重跑）
  if [[ -t 0 ]]; then
    local answer=""
    printf "⚠️  网络异常缓慢: %s\n是否接受该耗时并继续拉取? [y/N] " "${est_desc}" >&2
    read -r answer || answer=""
    case "${answer}" in
      y|Y|yes|Yes|YES)
        AK_SLOW_NET="accept"
        slow_net_gate "${component}" "${version}"
        return 0
        ;;
      *)
        fail_json "slow_net_rejected" \
          "\"estimate\":\"${est_desc}\"" \
          "\"recovery_steps\":[\"网络恢复后重跑\",\"或接受耗时: AK_SLOW_NET=accept 重跑（自动放宽超时预算）\",\"或快机拉取后拷贝 AK_SRC_CACHE 缓存 tar 离线解包\"]"
        ;;
    esac
  fi
  fail_json "slow_net_confirmation_required" \
    "\"estimate\":\"${est_desc}\"" \
    "\"threshold_seconds\":${AK_SLOW_NET_THRESHOLD}" \
    "\"recovery_steps\":[\"向用户确认是否接受预估耗时\",\"接受: AK_SLOW_NET=accept 重跑（脚本按测速自动放宽超时预算，无需手动算参）\",\"不接受: 网络恢复后重跑，或快机拉取后拷贝 AK_SRC_CACHE 缓存 tar 离线解包\"]"
}

download_source() {
  local component="$1"
  local version="$2"
  local target_dir="$3"

  if cache_restore "${component}" "${version}" "${target_dir}"; then
    log "源码获取完成（缓存命中，零网络操作）"
    return 0
  fi

  log "下载 ${component} ${version} 源码到 ${target_dir}..."
  run_in_target "mkdir -p $(dirname "${target_dir}")"
  local t0 t1
  t0=$(date +%s)

  case "${component}" in
    python)
      local tarball="Python-${version}.tgz"
      retry_in_target 3 "Python 源码下载(华为云镜像)" "wget -c -q --timeout=60 'https://mirrors.huaweicloud.com/python/${version}/${tarball}' -O /tmp/${tarball}" \
        || retry_in_target 3 "Python 源码下载(官方)" "wget -c -q --timeout=60 'https://www.python.org/ftp/python/${version}/${tarball}' -O /tmp/${tarball}"
      run_in_target "tar -tzf /tmp/${tarball} >/dev/null 2>&1 || { echo '下载的源码包损坏'; exit 1; }"
      run_in_target "tar -xf /tmp/${tarball} -C '$(dirname "${target_dir}")'"
      run_in_target "rm -rf '${target_dir}' && mv '$(dirname "${target_dir}")/Python-${version}' '${target_dir}'"
      ;;
    pytorch)
      # 三级回退链（耗时样本自 v2.5.1 --depth 1 实测：gitee 329MB/18s、gitcode 329MB/39s、
      # github 341MB/33s；机制版本无关，版本由环境探测决定，链与超时对所有版本生效）；
      # 每次尝试前清理残目录 + 硬超时（防弱网慢速爬行拖到小时级）
      # 三级耗尽 → 结构化 fail_json（含各源预算与恢复指引），不留半截树静默通过
      local clone_ok=false
      if retry_in_target 2 "pytorch gitee 克隆" "rm -rf ${target_dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git ${GIT_NET_OPTS} clone -q -b v${version} --depth 1 https://gitee.com/mirrors/pytorch.git ${target_dir} 2>/dev/null"; then
        clone_ok=true
      else
        log "gitee 镜像克隆失败，尝试 gitcode 加速..."
        if retry_in_target 2 "pytorch gitcode 加速克隆" "rm -rf ${target_dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git ${GIT_NET_OPTS} clone -q -b v${version} --depth 1 https://gitcode.com/gh_mirrors/py/pytorch.git ${target_dir} 2>/dev/null"; then
          clone_ok=true
        else
          log "gitcode 加速克隆失败，回退 github..."
          if retry_in_target 3 "pytorch github 克隆" "rm -rf ${target_dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git ${GIT_NET_OPTS} clone -q -b v${version} --depth 1 https://github.com/pytorch/pytorch.git ${target_dir}"; then
            clone_ok=true
          fi
        fi
      fi
      if [[ "${clone_ok}" != true ]]; then
        rm -rf "${target_dir}"
        fail_json "source_clone_failed" \
          "\"component\":\"${component}\",\"version\":\"${version}\"" \
          "\"budget\":\"3 源 7 次尝试 × ${GIT_ATTEMPT_TIMEOUT}s 硬超时（弱网爬行被墙钟截断，非无限等待）\"" \
          "\"recovery_steps\":[\"探测各源可达性: curl -sIL https://gitee.com/mirrors/pytorch.git 与 https://gitcode.com/gh_mirrors/py/pytorch.git\",\"弱网抖动大: 调大 AK_GIT_TIMEOUT（默认 900s）重跑\",\"网络受限内网: 手工克隆后用 --source-dir 指定，或配置可达私源镜像\",\"跑通一次后源码缓存(AK_SRC_CACHE)即隔离后续网络波动\"]"
      fi
      update_submodules "${target_dir}"
      ;;
    torch_npu)
      local branch="${version}"
      if [[ "${branch}" != v* ]]; then
        branch="v${branch}"
      fi
      retry_in_target 3 "torch_npu gitee 克隆" "rm -rf ${target_dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git ${GIT_NET_OPTS} clone -q -b ${branch} --depth 1 https://gitee.com/ascend/pytorch.git ${target_dir}" \
        || { rm -rf "${target_dir}"
             fail_json "source_clone_failed" \
               "\"component\":\"${component}\",\"version\":\"${version}\",\"source\":\"gitee/ascend/pytorch\"" \
               "\"budget\":\"3 次尝试 × ${GIT_ATTEMPT_TIMEOUT}s 硬超时\"" \
               "\"recovery_steps\":[\"探测源可达性: curl -sIL https://gitee.com/ascend/pytorch.git\",\"弱网抖动大: 调大 AK_GIT_TIMEOUT（默认 900s）重跑\",\"网络受限内网: 手工克隆后用 --source-dir 指定\",\"跑通一次后源码缓存(AK_SRC_CACHE)即隔离后续网络波动\"]"; }
      update_submodules "${target_dir}"
      ;;
  esac

  t1=$(date +%s)
  log "源码网络拉取耗时: $((t1 - t0))s"
  cache_store "${component}" "${version}" "${target_dir}"
  log "源码获取完成"
}

# ── 子模块镜像加速（机制版本无关；覆盖数字为 v2.5.1 实测样本，鲲鹏 aarch64 华为云）─────
# 35 个子模块 = 34 github + 1 gitlab(eigen)。实测覆盖：gitcode gh_mirrors 21/34、
# ghfast.top 通用代理补 11/34（并集 32/34），残余 onnx/psimd 走 github 直连；
# eigen 是唯一 gitlab 源，任何 github 加速器都不覆盖，直连（低速判死 + 重试兜底）。
# 铁律（实测教训）：
#   a. insteadOf 改写到镜像不存在的仓库（gitcode 403/ghfast 404）会让整个
#      submodule update 中止——改写层必须只含逐仓验证过的镜像；
#   b. --depth 1 浅克隆成功但 pinned SHA 补取失败时，工作树被静默留在默认
#      分支 tip（错误版本，文件存在性检查发现不了）——必须以 HEAD==gitlink 校验为准；
#   c. -c 的 insteadOf key 不能带引号（git 不解析引号，引号会泄漏进 key 导致改写失效）；
#   d. 镜像验证到 SHA 级，不能只验证仓库存在（gloo 实测：仓库在但 pinned SHA 缺失）。
# Git 内容寻址保证同一 SHA 内容跨源一致，主仓/子模块来源不同互不影响。

# 逐仓镜像验证脚本（写入源码树 .git 内，随仓库清理）：
#   输入 <cache秒> <map输出>，在源码树根执行；输出行格式 path|github相对路径|镜像URL
#   仅收录 pinned SHA 实际可浅取的镜像（并行 8 路，单项 20s 超时），map 缓存 24h
AK_PROBE_SCRIPT=$(cat <<'AKPROBE'
#!/usr/bin/env bash
set -uo pipefail
export GIT_TERMINAL_PROMPT=0
maxage="$1"; out="$2"
if [ -s "$out" ] && [ $(( $(date +%s) - $(stat -c %Y "$out" 2>/dev/null || echo 0) )) -lt "$maxage" ]; then
  exit 0
fi
probe_one() {
  p="$1" r="$2" m="$3" s="$4"
  d=$(mktemp -d "${TMPDIR:-/tmp}/ak-probe.XXXXXX") || return 1
  ( cd "$d" && git init -q . 2>/dev/null && timeout "${probe_t}" git fetch -q --depth 1 "$m" "$s" 2>/dev/null )
  rc=$?; rm -rf "$d"
  [ $rc -eq 0 ] && printf '%s|%s|%s\n' "$p" "$r" "$m"
}
export -f probe_one
# 探测超时随 AK_GIT_TIMEOUT 缩放（900s→20s）：慢网下固定 20s 会误杀本可
# 使用的镜像（探针需真实传输数 MB），导致镜像层被整体跳过退化为直连
export probe_t="${3:-20}"
cand=$(mktemp "${TMPDIR:-/tmp}/ak-cand.XXXXXX")
git config -f .gitmodules --get-regexp 'submodule\..*\.path' | while read -r k p; do
  sec="${k#submodule.}"; sec="${sec%.path}"
  u=$(git config -f .gitmodules --get "submodule.${sec}.url" 2>/dev/null) || continue
  case "$u" in
    https://github.com/*)
      r="${u#https://github.com/}"; r="${r%.git}"
      sha=$(git ls-tree HEAD "$p" 2>/dev/null | awk '{print $3}')
      [ -n "$sha" ] && printf '%s %s %s\n' "$p" "$r" "$sha"
      ;;
  esac
done > "$cand"
[ -s "$cand" ] || { rm -f "$cand"; exit 0; }
: > "$out.tmp"
# 层1: gitcode gh_mirrors（镜像路径含仓库名前 2 字符，无法通配，逐仓映射；
#   镜像 URL 不带 .git 后缀——.gitmodules 的 URL 多带 .git，insteadOf 按前缀
#   重写会保留残余 .git，若镜像 URL 也带 .git 会拼出 .git.git 403，实测踩坑）
awk '{n=$2; sub(".*/","",n); printf "%s %s %s %s\n", $1, $2, sprintf("https://gitcode.com/gh_mirrors/%s/%s", substr(n,1,2), n), $3}' "$cand" \
  | xargs -P 8 -n 4 bash -c 'probe_one "$@"' _ >> "$out.tmp" 2>/dev/null
# 层2: ghfast.top 通用代理（补 gitcode 未覆盖仓库）
awk 'NR==FNR{split($0,a,"|"); ok[a[1]]=1; next} !($1 in ok){printf "%s %s https://ghfast.top/https://github.com/%s %s\n", $1, $2, $2, $3}' "$out.tmp" "$cand" \
  | xargs -P 8 -n 4 bash -c 'probe_one "$@"' _ >> "$out.tmp" 2>/dev/null
rm -f "$cand"
# 空结果不落缓存（镜像全挂时下次仍需重探；否则空 map 会被当有效缓存 24h）
if [ -s "$out.tmp" ]; then
  mv "$out.tmp" "$out"
else
  rm -f "$out.tmp"
fi
AKPROBE
)

# 本地零网络校验：输出 HEAD != 父仓 gitlink 或工作树为空的子模块路径（空格分隔）
# 防御 --depth 1 的"SHA 缺失静默留在分支 tip"（实测 pybind11 停在旧 master tip）
submodule_mismatched_paths() {
  local dir="$1" paths="$2"
  if [[ -z "${paths// /}" ]]; then
    return 0
  fi
  local plist=""
  local p
  for p in ${paths}; do
    plist+="'${p}' "
  done
  run_in_target "cd ${dir} && for p in ${plist}; do pin=\$(git ls-tree HEAD \"\$p\" 2>/dev/null | awk '{print \$3}'); hd=\$(git -C \"\$p\" rev-parse HEAD 2>/dev/null); if [ \"\$pin\" != \"\$hd\" ] || ! [ -n \"\$(git -C \"\$p\" ls-files 2>/dev/null | head -1)\" ]; then printf '%s ' \"\$p\"; fi; done" 2>/dev/null || true
}

# PyTorch 子模块 URL 全部指向 github.com，是国内拉取慢的主因。
# 分层收敛拉取（每层只处理上一层校验后的残余路径，层间快速失败）：
#   层1: gitcode/ghfast 逐仓已验证镜像（SHA 级验证 + 24h 缓存）
#   层2: gitclone.com 通用代理（当前实测不可用，保留作恢复机会）
#   层3: github 直连（本机实测 10MB/s+，弱网环境依赖 fetchJobs 并行 + 低速判死）
# 全部层结束仍有残余 → 报错并给出人工指令。
# 可选 $2: 空格分隔的子模块路径列表，只定向更新这些（避免反复全量拉取）
update_submodules() {
  local dir="$1"
  local paths="${2:-}"

  # 仓库级提速/稳健配置（幂等）。子模块并行度 AK_SUBMODULE_JOBS 可调
  # （默认 8；=1 即串行，用于 A/B 度量或弱网单连接调试）
  run_in_target "cd ${dir} && git config submodule.fetchJobs ${AK_SUBMODULE_JOBS:-8}; git config http.lowSpeedLimit 1024; git config http.lowSpeedTime 60" || true

  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 子模块分层收敛拉取: 层1 gitcode/ghfast 逐仓SHA验证镜像 → 层2 gitclone.com → 层3 github 直连（fetchJobs=${AK_SUBMODULE_JOBS:-8} 并行 + 硬超时 ${GIT_ATTEMPT_TIMEOUT}s + HEAD==gitlink 校验）"
    run_in_target "cd ${dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git -c submodule.fetchJobs=${AK_SUBMODULE_JOBS:-8} submodule update --init --recursive --force --depth 1"
    return 0
  fi

  local accel_layers=("")
  if [[ "${GITHUB_ACCEL}" == true ]]; then
    # 生成逐仓已验证镜像映射（gitcode→ghfast，SHA 级，缓存 24h；
    # 探测单仓超时随 AK_GIT_TIMEOUT 缩放，下限 20s）
    local probe_t=$(( GIT_ATTEMPT_TIMEOUT / 45 ))
    (( probe_t < 20 )) && probe_t=20
    run_in_target "cat > ${dir}/.git/ak-probe.sh <<'AKPEOF'
${AK_PROBE_SCRIPT}
AKPEOF
chmod +x ${dir}/.git/ak-probe.sh && cd ${dir} && ./.git/ak-probe.sh 86400 ${dir}/.git/ak-mirror-map ${probe_t}" || true
    local map_lines=""
    map_lines=$(run_in_target "cat ${dir}/.git/ak-mirror-map 2>/dev/null" || true)
    local accel_cfgs="" line p r m cnt=0
    while IFS='|' read -r p r m; do
      [[ -z "${p}" || -z "${r}" || -z "${m}" ]] && continue
      # key 无引号（实测：带引号会泄漏进 key 导致改写失效）
      accel_cfgs+="-c url.${m}.insteadOf=https://github.com/${r} "
      cnt=$((cnt+1))
    done <<< "${map_lines}"
    if (( cnt > 0 )); then
      log "子模块镜像层1: ${cnt} 个仓库经 pinned-SHA 验证（gitcode/ghfast，缓存 24h）"
      accel_layers=("${accel_cfgs}" "-c url.https://gitclone.com/github.com/.insteadOf=https://github.com/ " "")
    else
      log "⚠️  逐仓镜像验证无可用结果，直接 github 直连"
      accel_layers=("" "")
    fi
  fi

  # 待拉取路径集合（未指定 = 全部顶层子模块）
  local remaining="${paths}"
  if [[ -z "${remaining// /}" ]]; then
    remaining=$(run_in_target "cd ${dir} && git config -f .gitmodules --get-regexp 'submodule\..*\.path' 2>/dev/null | awk '{print \$2}'" || true)
  fi

  # 有界轮次收敛（借鉴链接缺陷修复回路）：硬超时/网络抖动杀掉的 update 会留下
  # shallow.lock 残锁并使并行中的兄弟仓库半途而废，单轮无法收敛；
  # 每轮前清理 .git/modules 残锁（脚本串行执行，无并发 git，删锁安全），
  # 最多 3 轮，残余不减少即提前止损
  local layer cfgs pa p round prev_count cur_count
  prev_count=999999
  for round in 1 2 3; do
    cur_count=$(wc -w <<< "${remaining}")
    [[ -z "${remaining// /}" ]] && break
    (( cur_count >= prev_count )) && { log "⚠️  第 ${round} 轮残余未减少（${cur_count}），提前止损"; break; }
    prev_count=${cur_count}
    log "子模块收敛第 ${round}/3 轮，待拉取 ${cur_count} 个..."
    run_in_target "find ${dir}/.git/modules -name '*.lock' -type f -delete 2>/dev/null; true" || true
    for cfgs in "${accel_layers[@]}"; do
      [[ -z "${remaining// /}" ]] && break
      pa=""
      for p in ${remaining}; do
        pa+="'${p}' "
      done
      if [[ -n "${cfgs// /}" ]]; then
        log "子模块拉取（并行 fetchJobs=${AK_SUBMODULE_JOBS:-8}，路径数 $(wc -w <<< "${remaining}")）..."
        run_in_target "cd ${dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git ${cfgs} submodule update --init --recursive --force --depth 1 -- ${pa}" || true
      else
        log "子模块 github 直连拉取（并行 fetchJobs=${AK_SUBMODULE_JOBS:-8}，路径数 $(wc -w <<< "${remaining}")）..."
        run_in_target "cd ${dir} && GIT_TERMINAL_PROMPT=0 timeout -k 30 ${GIT_ATTEMPT_TIMEOUT} git submodule update --init --recursive --force --depth 1 -- ${pa}" || true
      fi
      remaining=$(submodule_mismatched_paths "${dir}" "${remaining}")
    done
  done

  if [[ -n "${remaining// /}" ]]; then
    fail "子模块按 pinned SHA 校验仍不完整: ${remaining}（镜像与直连均失败；检查网络，或对残余路径手动: cd ${dir} && git submodule update --init --recursive --force --depth 1 -- <路径>）"
  fi
  log "子模块全部通过 HEAD==gitlink 校验（版本正确性保证）"
}

detect_current_version() {
  local component="$1"
  local version=""
  local torch_ver npu_ver
  case "${component}" in
    python)
      version=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sys; print(\"%d.%d.%d\" % sys.version_info[:3])' 2>/dev/null" || true)
      [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "无法从 ${PYTHON_EXEC} 解析 Python 版本，请使用 --source-dir 指定源码路径"
      ;;
    pytorch)
      if [[ -n "${AK_PYTORCH_VERSION:-}" ]]; then
        version="${AK_PYTORCH_VERSION}"
        log "版本来源: AK_PYTORCH_VERSION 显式指定（优先于环境探测）: ${version}" >&2
      else
        version=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch; print(torch.__version__.split(\"+\")[0])' 2>/dev/null" || true)
        log "版本来源: 环境探测（${PYTHON_EXEC} import torch）: ${version:-<探测失败>}" >&2
      fi
      [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "无法解析 PyTorch 版本（可 export AK_PYTORCH_VERSION 显式指定；或 --source-dir 指定源码路径）"
      ;;
    torch_npu)
      # torch_npu.__version__ 形如 2.1.0.post4-6.0.rc1（<PyTorch>-<CANN>），
      # gitee 分支名为 v<PyTorch>-<CANN>，如 v2.1.0-6.0.rc1
      if [[ -n "${AK_TORCH_NPU_VERSION:-}" ]]; then
        version="${AK_TORCH_NPU_VERSION}"
        log "版本来源: AK_TORCH_NPU_VERSION 显式指定（优先于环境探测）: ${version}" >&2
      else
        version=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import re, torch_npu; v=torch_npu.__version__.split(\"+\")[0]; m=re.match(r\"(\\d+\\.\\d+\\.\\d+)(?:\\.post\\d+)?-(.+)\", v); print(m.group(1)+\"-\"+m.group(2) if m else \"\")' 2>/dev/null" || true)
        log "版本来源: 环境探测（${PYTHON_EXEC} import torch_npu.__version__）: ${version:-<探测失败>}" >&2
      fi
      [[ -n "${version}" ]] || fail "无法从 torch_npu.__version__ 推导 <PyTorch>-<CANN> 版本对（当前: $(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu; print(torch_npu.__version__)' 2>/dev/null" || echo unknown)），请 export AK_TORCH_NPU_VERSION=<PyTorch>-<CANN>（如 2.1.0-6.0.rc1）或使用 --source-dir 指定源码路径"
      ;;
  esac
  echo "${version}"
}

# ── 并行度 ───────────────────────────────────────────────────

calc_jobs() {
  if [[ -n "${MAX_JOBS_OPT}" ]]; then
    JOBS="${MAX_JOBS_OPT}"
  elif [[ "${DRY_RUN}" == true ]]; then
    JOBS=4
  else
    local nproc mem_kb mem_gb cap
    nproc=$(run_in_target "nproc 2>/dev/null" || echo 4)
    [[ "${nproc}" =~ ^[0-9]+$ ]] || nproc=4
    mem_kb=$(run_in_target "awk '/MemTotal/{print \$2}' /proc/meminfo 2>/dev/null" || echo 0)
    [[ "${mem_kb}" =~ ^[0-9]+$ ]] || mem_kb=0
    mem_gb=$((mem_kb / 1024 / 1024))
    cap=$((mem_gb / 2))
    (( cap < 4 )) && cap=4
    if (( nproc < cap )); then JOBS="${nproc}"; else JOBS="${cap}"; fi
  fi
  log "并行编译任务数: ${JOBS}"
}

# ── 依赖体检（快速探测，不安装） ─────────────────────────────

# 常用依赖速查表见经验库「常用依赖速查表」章节；本函数逐项探测并输出体检结果
check_dependencies() {
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 依赖体检（通用工具/各组件头文件与命令）"
    return 0
  fi

  local cc="${BISHENG_CLANG}"
  probe_cmd() { run_in_target "command -v ${1} >/dev/null 2>&1"; }
  probe_header() {
    local h="$1"
    run_in_target "printf '#include <${h}>' | ${cc} -E -x c - -o /dev/null 2>/dev/null" >/dev/null 2>&1
  }
  probe_py_include() {
    local inc
    inc=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig; print(sysconfig.get_paths()[\"include\"])' 2>/dev/null" || true)
    [[ -n "${inc}" ]] && run_in_target "[ -f \"${inc}/Python.h\" ]" >/dev/null 2>&1
  }
  probe_py_mod() {
    run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import ${1}' 2>/dev/null" >/dev/null 2>&1
  }

  local ok=() miss=() opt_miss=() t

  # 通用工具
  for t in git wget tar make readelf patchelf; do
    if probe_cmd "${t}"; then ok+=("${t}"); else miss+=("${t}"); fi
  done
  report_component "common" "${ok[*]}" "${miss[*]}" ""

  # python 组件（头文件以毕昇 clang 预探测）
  ok=(); miss=(); opt_miss=()
  for t in make perl; do probe_cmd "${t}" && ok+=("${t}") || miss+=("${t}"); done
  local h req_h="openssl/ssl.h readline/readline.h zlib.h bzlib.h lzma.h sqlite3.h ffi.h"
  local opt_h="uuid/uuid.h gdbm.h"
  for h in ${req_h}; do
    if probe_header "${h}"; then ok+=("hdr:$(basename ${h})"); else miss+=("hdr:$(basename ${h})"); fi
  done
  for h in ${opt_h}; do
    probe_header "${h}" || opt_miss+=("hdr:$(basename ${h})(可选,缺失则跳过对应标准模块)")
  done
  report_component "python" "${ok[*]}" "${miss[*]}" "${opt_miss[*]}"

  # pytorch 组件
  ok=(); miss=(); opt_miss=()
  for t in cmake ninja; do probe_cmd "${t}" && ok+=("${t}") || miss+=("${t}(pip 可补装)"); done
  if probe_py_include; then ok+=("Python.h"); else miss+=("Python.h(装 python3-devel 或用毕昇版 Python)"); fi
  if probe_py_mod "setuptools.command.bdist_wheel"; then ok+=("bdist_wheel"); else miss+=("bdist_wheel(>=70.1,<84)"); fi
  if probe_py_mod "wheel"; then ok+=("wheel"); else miss+=("wheel"); fi
  report_component "pytorch" "${ok[*]}" "${miss[*]}" ""

  # torch_npu 组件
  ok=(); miss=(); opt_miss=()
  if run_in_target "[ -d /usr/local/Ascend/ascend-toolkit/latest ] || [ -n \"\${ASCEND_HOME_PATH:-}\" ]" >/dev/null 2>&1; then
    ok+=("CANN")
  else
    opt_miss+=("CANN(编译 torch_npu 必需,当前环境未检出)")
  fi
  if probe_py_mod "torch"; then ok+=("torch"); else miss+=("torch(先编毕昇版 PyTorch)"); fi
  if probe_py_mod "setuptools.command.bdist_wheel" && probe_py_mod "wheel"; then ok+=("setuptools/wheel"); else miss+=("setuptools/wheel"); fi
  report_component "torch_npu" "${ok[*]}" "${miss[*]}" "${opt_miss[*]}"

  # 自动安装可修复项并复探（AK_AUTO_INSTALL_DEPS=0 关闭）
  if [[ "${CHECK_ONLY}" != true && "${AK_AUTO_INSTALL_DEPS:-1}" == "1" ]]; then
    auto_fix_dependencies "${cc}"
  fi
}

report_component() {
  local comp="$1" ok="$2" miss="$3" opt="$4"
  local line="依赖体检[${comp}]"
  [[ -n "${ok// /}" ]] && line+=" 可用: ${ok};"
  [[ -n "${miss// /}" ]] && line+=" 缺失: ${miss};"
  [[ -n "${opt// /}" ]] && line+=" 可选: ${opt}"
  log "${line%;}"
}

# CMakeCache linker flags 追加补丁（幂等，按 flag 特征子串去重）
cmake_cache_append_linker_flags() {
  local cache="$1" flag="$2"
  run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c '
import re, sys
cache, flag = sys.argv[1], sys.argv[2]
keys = [\"CMAKE_SHARED_LINKER_FLAGS\", \"CMAKE_MODULE_LINKER_FLAGS\"]
dedup = flag.split(\",\")[-1]
out, done = [], {}
for L in open(cache).read().splitlines(True):
    m = re.match(r\"^(CMAKE_(?:SHARED|MODULE)_LINKER_FLAGS):STRING=(.*)\", L)
    if m:
        done[m.group(1)] = 1
        if dedup not in L:
            out.append(m.group(1) + \":STRING=\" + m.group(2).rstrip() + \" \" + flag + \"\n\")
        else:
            out.append(L)
    else:
        out.append(L)
for k in keys:
    if k not in done:
        out.append(k + \":STRING=\" + flag + \"\n\")
open(cache, \"w\").write(\"\".join(out))
print(\"patched:\", cache)
' \"${cache}\" \"${flag}\""
}

# ── .rodata 段布局安全网 ─────────────────────────────────────

# BiSheng 5.2 (LLVM 19.1.7) 的 lld 默认 max-page-size=65536（段 Align 0x10000，
# 64KB 页内核兼容），rosegment/RELRO 重叠 bug 上游 LLVM 17 前已修复——正常
# 不需要 --no-rosegment。仅旧毕昇（LLVM<17）在 64KB 页 + 大 .rodata 时可能报
# "section .rodata load segment overlap"：构建失败且日志命中该错误时，
# 自动补丁 --no-rosegment 重链一次
retry_build_on_rosegment_overlap() {
  local build_fn_log_hint="${1:-}"
  if [[ -z "${LOG_FILE}" || ! -f "${LOG_FILE}" ]]; then
    return 1
  fi
  if ! grep -q 'load segment overlap' "${LOG_FILE}" 2>/dev/null; then
    return 1
  fi
  if [[ "${AK_ROSEGMENT_RETRIED:-0}" == "1" ]]; then
    return 1
  fi
  log "检出 lld '.rodata load segment overlap'（旧工具链 64KB 页兼容问题），补丁 --no-rosegment 重链..."
  local cache="${SOURCE_DIR}/build/CMakeCache.txt"
  if run_in_target "[ -f ${cache} ]" 2>/dev/null; then
    cmake_cache_append_linker_flags "${cache}" "-Wl,--no-rosegment"
  else
    AK_EXTRA_LDFLAGS="-Wl,--no-rosegment"
  fi
  AK_ROSEGMENT_RETRIED=1
  return 0
}

# ── 构建失败已知模式自动诊断 ─────────────────────────────────
# 经验库无法穷举编译问题（用户痛点：问题多、持续分析成本高）。本表把已踩过
# 的失败特征→根因/修复动作做成签名匹配，构建失败时自动附在 fail_json 输出，
# Agent 命中已知模式即可直接执行修复，无需从原始日志逐案重析。
# 新增经验：往 sigs/diags 数组各加一行（并与 references/ak-compiler-playbook.md
# 排障章节保持同步）
known_failure_diagnosis() {
  [[ -n "${LOG_FILE}" && -f "${LOG_FILE}" ]] || return 0
  local -a sigs=(
    'Python\.h: No such file'
    'No module named (setuptools|wheel)(\.command\.bdist_wheel)?'
    'casts away qualifiers'
    "use of undeclared identifier 'CLOCK_MONOTONIC_RAW'"
    'No space left on device'
    '[Kk]illed'
    'section \.rodata load segment overlap'
    'undefined symbol: .*new_qtensor'
    'error: unsupported option .*fprofile'
    'undefined reference to .*_gfortran'
    'fatal error: .*No such file or directory'
    'Compatibility with CMake < 3\.5 has been removed'
    'not found \(missing: ?libomp'
    'undefined symbol: .*(__kmpc_|omp_get_|omp_set_|omp_in_parallel)'
  )
  local -a diags=(
    '缺 Python 开发头文件: dnf install -y python3-devel，或确认 --python-executable 指向的毕昇 Python 及其 sysconfig 头文件'
    '构建工具缺失: python -m pip install "setuptools==75.8.0" "wheel==0.41.3"（须在 70.1~84 区间，>=84 静默产出残缺 whl）'
    'op-plugin 源码被 clang 严格 cast 检查拒绝: 按经验库「重编译环境」坑在报错处加 const_cast（可用 AK_PATCH_SCRIPT 分版补丁固化）'
    '缺 _GNU_SOURCE 或 <time.h>: 脚本已默认注入 -D_GNU_SOURCE；仍命中说明该文件缺显式 include（见 references/patches/torch_npu-clang19-missing-includes.sh）'
    '磁盘不足: df -h 复核（脚本按组件预检 3/12/8GB；ccache 目录也占空间，可 AK_CCACHE_DIR 迁移或 AK_CCACHE=0）'
    '疑似 OOM（链接期最常见）: --max-jobs 调低（如 nproc/4）；ThinLTO/lld 链接内存大，建议并行 ≤ 物理内存GB/16'
    'lld rosegment 段重叠（旧 LLVM 64KB 页）: 脚本应已自动 --no-rosegment 重试；仍命中请检查 lld 版本是否 <17'
    'ThinLTO 内部化 inline 符号: 脚本修复回路应已自动处理；仍命中见经验库「new_qtensor」节（internalization，非 COMDAT 折叠）'
    '编译器不支持该 PGO 旗标: 确认毕昇版本；或该组件 ci/build.sh 不支持旗标路径（脚本门禁2 应已拦截）'
    '静态库拉入 Fortran 符号: 脚本修复回路应已自动补 -lgfortran；仍命中见经验库「_gfortran」节'
    '源码/头文件缺失: 按日志具体路径核对（子模块未拉全最常见——重跑会走子模块 SHA 级校验补拉）'
    'CMake 4.x 移除 <3.5 兼容（旧版 pytorch/torch_npu vendored 三方声明 cmake_minimum_required 过低）: 脚本已注入 CMAKE_POLICY_VERSION_MINIMUM=3.5 规避；仍命中则 pip install "cmake<4" 并前置其 PATH'
    '毕昇 LTO 产物缺 libomp.so（whl 不随包分发编译器运行时）: 脚本修复回路已扩展到 ${BISHENG_PREFIX}/lib 自动补齐；仍命中则 source /etc/profile.d/bisheng.sh 后复核 LD_LIBRARY_PATH'
    'OpenMP 符号（__kmpc_*/omp_*）未解析——libomp.so 属毕昇运行时组件: 确认已 source /etc/profile.d/bisheng.sh（LD_LIBRARY_PATH 含毕昇 lib）；迁移场景把 ${BISHENG_PREFIX}/lib/libomp.so 一并带过去'
  )
  local out="" i
  for i in "${!sigs[@]}"; do
    if grep -qaE "${sigs[$i]}" "${LOG_FILE}" 2>/dev/null; then
      out+="- ${diags[$i]}\n"
    fi
  done
  printf '%b' "${out}"
}

# 构建失败统一出口：附已知模式命中项 + 日志尾部错误行（转义后入 JSON）
fail_build_with_diagnosis() {
  local comp="$1" diag tailerr
  diag=$(known_failure_diagnosis)
  tailerr=$(grep -aE 'error:|Error |FAILED|undefined reference|undefined symbol|No such file' "${LOG_FILE}" 2>/dev/null | tail -5 || true)
  if [[ -n "${diag}" ]]; then
    fail_json "${comp}_build_failed" \
      "\"matched_known_failures\":\"$(json_escape "${diag}")\"" \
      "\"log_tail_errors\":\"$(json_escape "${tailerr}")\"" \
      "\"recovery_steps\":[\"完整构建日志: ${LOG_FILE}\",\"按 matched_known_failures 对应动作修复后重跑（--reuse-build 增量续编）\",\"未覆盖的新问题: 把 log_tail_errors 交由 Agent 对照 references/ak-compiler-playbook.md 排障章节分析\"]"
  else
    fail_json "${comp}_build_failed" \
      "\"log_tail_errors\":\"$(json_escape "${tailerr}")\"" \
      "\"recovery_steps\":[\"完整构建日志: ${LOG_FILE}\",\"未命中已知失败特征（新问题）: 把 log_tail_errors 交由 Agent 对照 references/ak-compiler-playbook.md 排障章节分析\",\"修复后重跑（--reuse-build 增量续编）\"]"
  fi
}

# 通用分版补丁执行：AK_PATCH_SCRIPT 单补丁（兼容存量用法）；
# AK_PATCH_SCRIPTS 冒号分隔多补丁按序执行（torch_npu 2.4.0 需同时挂
# missing-includes + abi-env——历史单钩子无法满足）。
# 签名: <patch.sh> <源码树> <组件> <版本>；补丁脚本自身必须幂等。
# 分版补丁集见 references/patches/（按命中版本/症状挂载）
run_patch_scripts() {
  local list="${AK_PATCH_SCRIPTS:-}"
  [[ -n "${AK_PATCH_SCRIPT:-}" ]] && list="${list:+${list}:}${AK_PATCH_SCRIPT}"
  [[ -n "${list}" ]] || return 0
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 分版补丁（按序）: ${list}"
    return 0
  fi
  local p n=0 patches=()
  IFS=: read -r -a patches <<< "$list"
  for p in "${patches[@]}"; do
    [[ -z "${p}" ]] && continue
    n=$((n+1))
    log "执行分版补丁脚本[${n}]: ${p}（目标: ${SOURCE_DIR}）"
    if [[ -n "$CONTAINER" && -f "$p" ]]; then
      docker exec -i "$CONTAINER" bash -s -- "$SOURCE_DIR" "$COMPONENT" "${CURRENT_VERSION:-}" < "$p" || fail "分版补丁脚本执行失败: $p"
    else
      target_exec bash "$p" "$SOURCE_DIR" "$COMPONENT" "${CURRENT_VERSION:-}" || fail "分版补丁脚本执行失败: $p"
    fi
  done
  return 0
}

# ── 环境指纹快照与复现校验 ───────────────────────────────────
# 痛点：编译前后环境未隔离，优化前后数据不可复现。本组能力提供「可检测性」：
# 快照记录编译相关环境指纹（解释器/编译器/ABI/关键 so 哈希/pip freeze 指纹/
# CANN 版本/关键环境变量），--env-verify 对比当前环境与历史快照，差异键即
# 不可复现风险的来源（完整隔离方案见设计文档）。快照目录在宿主侧（与
# chain state 同侧）；探针值全部取自 target 环境（--container 时为容器内值）。
env_probe_pairs() {
  local torch_so npu_so
  torch_so=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch,os; print(os.path.join(os.path.dirname(torch.__file__),\"lib\",\"libtorch_cpu.so\"))' 2>/dev/null" || true)
  npu_so=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu,os; print(os.path.join(os.path.dirname(torch_npu.__file__),\"lib\",\"libtorch_npu.so\"))' 2>/dev/null" || true)
  printf 'snapshot_time\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'uname\t%s\n' "$(run_in_target 'uname -a' || true)"
  printf 'cpu_model\t%s\n' "$(run_in_target "lscpu 2>/dev/null | grep -E 'Model name|Model number' | tr '\n' ';'" || true)"
  printf 'python_path\t%s\n' "$(run_in_target "${TGT_ENV} command -v ${PYTHON_EXEC} 2>/dev/null" || true)"
  printf 'python_version\t%s\n' "$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sys; print(sys.version.split()[0])' 2>/dev/null" || true)"
  printf 'bisheng_clang_version\t%s\n' "$(run_in_target "${BISHENG_CLANG:-clang} --version 2>/dev/null | head -1" || true)"
  printf 'torch_version\t%s\n' "$(run_in_target "${TGT_ENV} TORCH_DEVICE_BACKEND_AUTOLOAD=0 ${PYTHON_EXEC} -c 'import torch; print(torch.__version__)' 2>/dev/null" || true)"
  printf 'torch_npu_version\t%s\n' "$(run_in_target "${TGT_ENV} TORCH_DEVICE_BACKEND_AUTOLOAD=0 ${PYTHON_EXEC} -c 'import torch_npu; print(torch_npu.__version__)' 2>/dev/null" || true)"
  printf 'torch_cxx11_abi\t%s\n' "$(run_in_target "${TGT_ENV} TORCH_DEVICE_BACKEND_AUTOLOAD=0 ${PYTHON_EXEC} -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))' 2>/dev/null" || true)"
  printf 'libtorch_cpu_sha256\t%s\n' "$(run_in_target "sha256sum '${torch_so}' 2>/dev/null | awk '{print \$1}'" || true)"
  printf 'libtorch_npu_sha256\t%s\n' "$(run_in_target "sha256sum '${npu_so}' 2>/dev/null | awk '{print \$1}'" || true)"
  printf 'cann_version\t%s\n' "$(run_in_target "ls /usr/local/Ascend/ascend-toolkit/ 2>/dev/null | grep -v latest | tr '\n' ';'" || true)"
  printf 'key_env_vars\t%s\n' "$(run_in_target "env | grep -aE '^(LLVM_PROFILE_FILE|OMP_PROC_BIND|OMP_NUM_THREADS|GLIBCXX_USE_CXX11_ABI|_GLIBCXX_USE_CXX11_ABI|ASCEND_HOME_PATH)=' | sort | tr '\n' ';'" || true)"
}

record_env_snapshot() {
  local dir="$1"
  mkdir -p "${dir}"
  local pip_freeze
  pip_freeze=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -m pip freeze 2>/dev/null | sort" || true)
  printf '%s\n' "${pip_freeze}" > "${dir}/pip_freeze.txt"
  env_probe_pairs > "${dir}/.pairs.$$"
  printf 'pip_freeze_sha256\t%s\n' "$(printf '%s' "${pip_freeze}" | sha256sum | awk '{print $1}')" >> "${dir}/.pairs.$$"
  python3 - "${dir}/.pairs.$$" "${dir}/env_snapshot.json" << 'PYEOF'
import json, sys
pairs = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.rstrip("\n")
    if "\t" in line:
        k, v = line.split("\t", 1)
        pairs[k] = v
json.dump(pairs, open(sys.argv[2], "w", encoding="utf-8"), indent=1, ensure_ascii=False)
PYEOF
  rm -f "${dir}/.pairs.$$"
  log "环境指纹快照完成: ${dir}/env_snapshot.json（依赖清单: ${dir}/pip_freeze.txt）"
}

verify_env_snapshot() {
  local dir="$1"
  [[ -f "${dir}/env_snapshot.json" ]] || fail "快照不存在: ${dir}/env_snapshot.json（先运行 --env-snapshot ${dir}）"
  local cur="/tmp/ak_env_verify_cur.$$"
  record_env_snapshot "${cur}" >/dev/null 2>&1
  python3 - "${dir}/env_snapshot.json" "${cur}/env_snapshot.json" << 'PYEOF'
import json, sys
old = json.load(open(sys.argv[1], encoding="utf-8"))
new = json.load(open(sys.argv[2], encoding="utf-8"))
# snapshot_time 为采集时刻元数据（两次快照必然不同），不参与复现性比对
META_KEYS = {"snapshot_time"}
keys = sorted((set(old) | set(new)) - META_KEYS)
mism = {k: {"snapshot": old.get(k, ""), "current": new.get(k, "")}
        for k in keys if old.get(k, "") != new.get(k, "")}
print(json.dumps({
    "env_reproducible": not mism,
    "mismatched_keys": list(mism),
    "details": mism,
}, indent=1, ensure_ascii=False))
sys.exit(1 if mism else 0)
PYEOF
  local rc=$?
  rm -rf "${cur}"
  return ${rc}
}



# 依赖自动安装（借鉴 Firefox mozboot + pytorch CI 重试模式）：
# 体检缺失项按映射表自动安装（dnf/pip 各自带重试），装后复探更新报告；
# 不可自动安装项（CANN/torch/非源码形态）保持人工提示
auto_fix_dependencies() {
  local cc="$1"
  [[ "${DRY_RUN}" == true ]] && return 0

  local auto_done=() auto_fail=() pkg

  probe_cmd() { run_in_target "command -v ${1} >/dev/null 2>&1"; }
  probe_header() {
    local h="$1"
    run_in_target "printf '#include <${h}>' | ${cc} -E -x c - -o /dev/null 2>/dev/null" >/dev/null 2>&1
  }
  probe_py_mod() {
    run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import ${1}' 2>/dev/null" >/dev/null 2>&1
  }

  # 命令类映射
  local cmd_map="git:git wget:wget tar:tar make:make readelf:binutils patchelf:patchelf perl:perl"
  for t in git wget tar make readelf patchelf perl; do
    probe_cmd "${t}" && continue
    pkg=$(printf '%s' "${cmd_map}" | tr ' ' '\n' | grep "^${t}:" | cut -d: -f2)
    [[ -n "${pkg}" ]] || { auto_fail+=("${t}(无映射包)"); continue; }
    if dnf_install_retry "${pkg}"; then
      probe_cmd "${t}" && auto_done+=("${t}") || auto_fail+=("${t}(装后仍缺)")
    else
      auto_fail+=("${t}(dnf 失败)")
    fi
  done

  # 头文件类映射（python 组件的 dnf 包；uuid/gdbm 可选项不做强求）
  local hdr_map="ssl.h:openssl-devel readline.h:readline-devel zlib.h:zlib-devel bzlib.h:bzip2-devel lzma.h:xz-devel sqlite3.h:sqlite-devel ffi.h:libffi-devel gdbm.h:gdbm-devel"
  for h in ssl.h readline.h zlib.h bzlib.h lzma.h sqlite3.h ffi.h gdbm.h; do
    probe_header "${h}" && continue
    pkg=$(printf '%s' "${hdr_map}" | tr ' ' '\n' | grep "^${h}:" | cut -d: -f2)
    [[ -n "${pkg}" ]] || continue
    if dnf_install_retry "${pkg}"; then
      probe_header "${h}" && auto_done+=("hdr:${h}") || auto_fail+=("hdr:${h}(装后仍缺)")
    else
      auto_fail+=("hdr:${h}(dnf 失败)")
    fi
  done

  # python 模块类（bdist_wheel/wheel 走钉版组合；cmake/ninja 走 pip）
  if ! probe_py_mod "setuptools.command.bdist_wheel" || ! probe_py_mod "wheel"; then
    if pip_install 'setuptools==75.8.0' 'wheel==0.41.3'; then
      probe_py_mod "setuptools.command.bdist_wheel" && probe_py_mod "wheel" \
        && auto_done+=("setuptools/wheel") || auto_fail+=("setuptools/wheel(装后仍缺)")
    else
      auto_fail+=("setuptools/wheel(pip 失败)")
    fi
  fi
  probe_py_mod "cmake" || { pip_install cmake && auto_done+=("cmake") || auto_fail+=("cmake(pip 失败)"); }
  probe_py_mod "ninja" || { pip_install ninja && auto_done+=("ninja") || auto_fail+=("ninja(pip 失败)"); }

  if [[ ${#auto_done[@]} -gt 0 ]]; then
    log "依赖自动安装成功: ${auto_done[*]}"
  fi
  if [[ ${#auto_fail[@]} -gt 0 ]]; then
    log "⚠️  依赖自动安装失败（需人工处理）: ${auto_fail[*]}"
  fi
  return 0
}

# ── PyTorch 编译前置 ─────────────────────────────────────────

# setuptools>=84 绕过自定义 build_py 导致 whl 静默残缺（torchnpu-wheel-packaging-playbook）；
# setuptools<70.1 不内置 bdist_wheel（PyTorch setup.py 直接 import 失败，实测 68.2.2 即缺）。
# 策略：优先钉实战版本组合 68.2.2，缺 bdist_wheel 则回退 70.1~84 之间的 75.8.0
pin_build_tools() {
  log "钉住 setuptools/wheel 构建版本（须 bdist_wheel 可用且 <84）..."
  local pin_ok=true
  pip_install 'setuptools==68.2.2' 'wheel==0.41.3' || pin_ok=false
  if [[ "${pin_ok}" == true && "${DRY_RUN}" != true ]]; then
    if ! run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import setuptools.command.bdist_wheel' 2>/dev/null"; then
      log "setuptools 68.2.2 缺 bdist_wheel（70.1 才内置），回退 setuptools==75.8.0..."
      pin_ok=true
      pip_install 'setuptools==75.8.0' 'wheel==0.41.3' || pin_ok=false
    fi
  fi
  [[ "${pin_ok}" == true ]] || fail_json "pip_install_build_tools_failed" \
    "\"pip_index\":\"${PIP_INDEX}\"" \
    "\"recovery_steps\":[\"检查网络与 PyPI 源（--pip-index 指定环境内私源，--pip-trusted-host 处理自签证书）\",\"离线环境: 联网机 pip download 后目标机 pip install --no-index --find-links\",\"手动执行: ${PYTHON_EXEC} -m pip install 'setuptools==75.8.0' 'wheel==0.41.3'\"]"
  if [[ "${DRY_RUN}" != true ]]; then
    local st_ver
    st_ver=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import setuptools; print(setuptools.__version__)' 2>/dev/null" || true)
    run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import setuptools.command.bdist_wheel' 2>/dev/null" \
      || fail_json "bdist_wheel_unavailable" "\"setuptools_version\":\"${st_ver}\"" \
         "\"recovery_steps\":[\"手动安装 70.1~84 之间的 setuptools，如 ${PYTHON_EXEC} -m pip install 'setuptools==75.8.0'\"]"
    run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import wheel' 2>/dev/null" \
      || fail_json "wheel_module_unavailable" "\"recovery_steps\":[\"${PYTHON_EXEC} -m pip install 'wheel==0.41.3'\"]"
    log "setuptools=${st_ver}（bdist_wheel/wheel 可用）"
  fi
}

install_pytorch_requirements() {
  local dir="$1"
  log "安装 PyTorch 编译依赖（requirements.txt，best-effort）..."
  PIP_CWD="${dir}" pip_install -r requirements.txt \
    || log "⚠️  requirements.txt 安装存在失败项，若编译报缺模块请按报错补装（PyPI 源: ${PIP_INDEX}）"
}

ensure_ninja_cmake() {
  run_in_target "command -v ninja >/dev/null 2>&1" || {
    log "ninja 未安装，尝试 pip 安装..."
    pip_install ninja || log "⚠️  ninja 安装失败，PyTorch 构建可能回退 make（较慢）"
  }
  run_in_target "command -v cmake >/dev/null 2>&1" || {
    log "cmake 未安装，尝试 pip 安装..."
    pip_install cmake || fail "cmake 不可用，无法编译 PyTorch"
  }
}

# clang 将 -Werror=cast-function-type 误报为错误（playbook「注意事项」）
patch_cast_function_type() {
  local dir="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 检查并移除 -Werror=cast-function-type"
    return 0
  fi
  local hits
  hits=$(run_in_target "cd ${dir} && grep -rl -- '-Werror=cast-function-type' CMakeLists.txt cmake/ 2>/dev/null" || true)
  if [[ -n "${hits}" ]]; then
    log "移除 -Werror=cast-function-type（clang 误报，备份为 *.bak_cft）..."
    run_in_target "cd ${dir} && grep -rl -- '-Werror=cast-function-type' CMakeLists.txt cmake/ 2>/dev/null | xargs -r sed -i.bak_cft 's/-Werror=cast-function-type//g'"
  fi
}

# ── 构建配置一致性/插桩验证 ──────────────────────────────────

# 产物插桩段检测：__llvm_prf_* 段存在 = 插桩版（PGO1），可采集 profile
count_llvm_prf_sections() {
  local so="$1"
  run_in_target "readelf -SW ${so} 2>/dev/null | grep -c '__llvm_prf'" || echo 0
}

# --reuse-build 配置漂移预检：CMakeCache 残留的 -fprofile-generate 与请求
# stage 不一致时，增量续编会静默产出错误形态产物（实测：请求 lto 却得到
# 插桩版，或反之），必须拒绝
precheck_reuse_config_drift() {
  local source_dir="$1"
  [[ "${REUSE_BUILD}" == true && "${DRY_RUN}" != true ]] || return 0
  local cache="${source_dir}/build/CMakeCache.txt"
  run_in_target "[ -f ${cache} ]" || return 0
  local has_pgo
  has_pgo=$(run_in_target "grep -c 'fprofile-generate' ${cache} 2>/dev/null" || echo 0)
  [[ "${has_pgo}" =~ ^[0-9]+$ ]] || has_pgo=0
  if [[ "${STAGE}" == "profile_gen" && "${has_pgo}" -eq 0 ]] || [[ "${STAGE}" != "profile_gen" && "${has_pgo}" -gt 0 ]]; then
    fail_json "reuse_build_config_drift" \
      "\"cache_has_profile_generate\":$([[ ${has_pgo} -gt 0 ]] && echo true || echo false)" \
      "\"requested_stage\":\"${STAGE}\"" \
      "\"recovery_steps\":[\"构建配置与请求 stage 不一致，本次需全量重建：去掉 --reuse-build 重跑（会 git clean）\",\"若确要保留插桩配置：--stage profile_gen\"]"
  fi
  log "reuse-build 配置一致性预检通过（cache ${has_pgo} 处 fprofile-generate vs stage=${STAGE}）"
}

# 产物形态门禁：profile_gen 必须产出插桩版（否则采集必然空转），
# compile/lto 必须产出非插桩版
check_artifact_instrumentation() {
  local libcpu="$1"
  [[ "${DRY_RUN}" == true ]] && return 0
  local n
  n=$(count_llvm_prf_sections "${libcpu}")
  [[ "${n}" =~ ^[0-9]+$ ]] || n=0
  if [[ "${STAGE}" == "profile_gen" ]]; then
    if [[ "${n}" -eq 0 ]]; then
      fail_json "artifact_not_instrumented" \
        "\"lib\":\"${libcpu}\"" \
        "\"recovery_steps\":[\"产物缺少 __llvm_prf 段，-fprofile-generate 未生效\",\"检查 CMAKE_CXX_FLAGS 是否含 -fprofile-generate\",\"去掉 --reuse-build 全量重建\"]"
    fi
    log "插桩验证通过（${n} 处 __llvm_prf 段，可采集 profile）"
  else
    if [[ "${n}" -gt 0 ]]; then
      fail_json "artifact_unexpectedly_instrumented" \
        "\"lib\":\"${libcpu}\"" \
        "\"recovery_steps\":[\"请求的是非插桩构建，但产物含 __llvm_prf 段（build 配置漂移）\",\"去掉 --reuse-build 全量重建\"]"
    fi
  fi
}

# CANN dlsym 导出面审计：
# CANN runtime 经 dlsym(RTLD_DEFAULT) 在 caller（PyTorch 进程全局作用域）的
# .dynsym 中按名查找运行时符号（如 check_aclnn_kernel_available/GetOpApiFuncAddr
# 系列）。这类符号没有任何链接期引用——import 阶段不会暴露问题，只有算子运行时
# 报错（npu_mhc_* 等）。被 ThinLTO 内部化的 inline 定义（linkonce_odr）会从
# .dynsym 消失，必须主动审计：CANN 库字符串提取候选名 ∩ torch 产物 local 符号。
audit_dlsym_export_surface() {
  [[ "${DRY_RUN}" == true ]] && { log "[dry-run] CANN dlsym 导出面审计"; return 0; }
  local cann_dir="${AK_CANN_DIR:-}"
  if [[ -z "${cann_dir}" ]]; then
    for d in /usr/local/Ascend/ascend-toolkit/latest/lib64 /usr/local/Ascend/ascend-toolkit/latest/*/lib64; do
      [[ -d "${d}" ]] && cann_dir="${d}" && break
    done
  fi
  [[ -z "${cann_dir}" ]] && { log "CANN 未检出，跳过 dlsym 导出面审计"; return 0; }
  log "CANN 库目录: ${cann_dir}"

  local torch_libs
  torch_libs=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch,os; print(os.path.dirname(torch.__file__)+\"/lib\")' 2>/dev/null" || true)
  [[ -n "${torch_libs}" ]] || return 0
  [[ -n "${NPU_DIR_AUDIT:-}" ]] || NPU_DIR_AUDIT=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu,os; print(os.path.dirname(torch_npu.__file__)+\"/lib\")' 2>/dev/null" || true)

  local cands
  cands=$(run_in_target "strings \$(ls ${cann_dir}/libopapi*.so ${cann_dir}/libacl*.so ${cann_dir}/libascendcl.so 2>/dev/null) 2>/dev/null | grep -E '^(_Z[A-Za-z0-9_]{8,}|(aclnn|check_|GetOpApi)[A-Za-z0-9_]{7,})\$' | sort -u | head -300" || true)
  [[ -z "${cands}" ]] && { log "未提取到 dlsym 候选名，跳过"; return 0; }
  log "dlsym 候选名: $(printf '%s\n' "${cands}" | wc -l) 个（提取自 CANN 库字符串）"

  local broken=() name dsym ssym
  # 一次提取动态/静态符号表（libtorch_cpu.so 约 2GB，300 候选 × 全量 nm 解析
  # 实测 30min 级；落盘后逐名本地 grep 秒级完成）
  local syms_dyn syms_sta
  syms_dyn=$(run_in_target "nm -D ${torch_libs}/libtorch_cpu.so ${NPU_DIR_AUDIT:+${NPU_DIR_AUDIT}/libtorch_npu.so} 2>/dev/null" || true)
  syms_sta=$(run_in_target "nm ${torch_libs}/libtorch_cpu.so ${NPU_DIR_AUDIT:+${NPU_DIR_AUDIT}/libtorch_npu.so} 2>/dev/null" || true)
  while IFS= read -r name; do
    name="${name//[[:space:]]/}"
    [[ -z "${name}" ]] && continue
    dsym=$(printf '%s\n' "${syms_dyn}" | grep -cE " ${name}\$" || true)
    [[ "${dsym}" =~ ^[0-9]+$ ]] || dsym=0
    [[ "${dsym}" -gt 0 ]] && continue
    ssym=$(printf '%s\n' "${syms_sta}" | grep -cE " [tT] ${name}\$" || true)
    [[ "${ssym}" =~ ^[0-9]+$ ]] || ssym=0
    [[ "${ssym}" -gt 0 ]] && broken+=("${name}")
  done <<< "${cands}"

  if [[ ${#broken[@]} -eq 0 ]]; then
    log "dlsym 导出面审计通过（CANN 候选名均可解析）"
    return 0
  fi
  log "⚠️  dlsym 目标被 ThinLTO 内部化: ${broken[*]}"

  if [[ -n "${PYTORCH_BUILD_SRC:-}" ]]; then
    if fix_internalized_symbols "${PYTORCH_BUILD_SRC}" "${broken[@]}"; then
      log "dlsym 导出面审计修复完成"
      return 0
    fi
  fi
  fail_json "dlsym_symbols_internalized" \
    "\"symbols\":\"${broken[*]}\"" \
    "\"mechanism\":\"CANN dlsym(RTLD_DEFAULT) 按名查找 caller 符号表，内化符号不在 .dynsym\"" \
    "\"recovery_steps\":[\"对每个符号: demangle 后在 PyTorch 源码树定位定义文件\",\"去 inline（头文件带函数体的改为 声明+单一TU定义；.cpp 内定义直接去 inline）\",\"增量重链 PyTorch 并重装（torch_npu 无需重编）\",\"无 PyTorch 源码时人工修复，符号清单见上方日志\"]"
}

# 通用内化符号修复：torch_npu import 报 undefined symbol 且该符号在
# libtorch_cpu 中为 local(t)——即被 ThinLTO 内部化（new_qtensor 是已知实例，
# 任意量化相关 inline 符号均可能命中）。修复 = demangle 定位定义源文件 →
# 去 inline（备份）→ 增量重链 PyTorch → 复核导出。
# torch_npu 无需重编（符号经 dynsym 动态解析）。
# 返回 0=全部修复并复核通过；1=无法自动修复（manual_symbols 已列出）
fix_internalized_symbols() {
  local source_dir="$1"; shift
  local syms=("$@")
  if [[ "${DRY_RUN}" == true || ${#syms[@]} -eq 0 ]]; then
    return 1
  fi
  local cxxfilt="c++filt"
  run_in_target "command -v llvm-cxxfilt >/dev/null 2>&1" && cxxfilt="llvm-cxxfilt"

  local fixed=0 manual=""
  local sym demangled fname cands cand ln fixed_file=""
  for sym in "${syms[@]}"; do
    demangled=$(run_in_target "echo '${sym}' | ${cxxfilt} 2>/dev/null" | head -1 || true)
    fname=$(printf '%s' "${demangled}" | sed 's/(.*//; s/.*:://; s/~.*//' )
    [[ -n "${fname}" ]] || { manual+="${sym}(无法demangle) "; continue; }
    log "定位 ${fname} 的定义源文件..."
    cands=$(run_in_target "cd ${source_dir} && grep -rln \"${fname}\" aten/src caffe2 torch 2>/dev/null | grep -E '\\.cpp$' | head -10" || true)
    local target_file="" target_ln=""
    for cand in ${cands}; do
      ln=$(run_in_target "cd ${source_dir} && grep -nE '^inline .*${fname}\\(|^static inline .*${fname}\\(' ${cand} 2>/dev/null | head -1 | cut -d: -f1" || true)
      if [[ -n "${ln}" ]]; then
        target_file="${cand}"; target_ln="${ln}"
        break
      fi
    done
    if [[ -z "${target_file}" ]]; then
      manual+="${sym}(定义非 inline 形态或未定位: ${demangled}) "
      continue
    fi
    log "  修复: ${target_file}:${target_ln} 去 inline（备份 .bak_inlined）"
    if [[ "${fixed_file}" != "${target_file}" ]]; then
      run_in_target "cd ${source_dir} && cp -n ${target_file} ${target_file}.bak_inlined 2>/dev/null; sed -i '${target_ln}s/^static inline //; ${target_ln}s/^inline //' ${target_file}"
      fixed_file="${target_file}"
    fi
    fixed=$((fixed+1))
  done

  if [[ ${fixed} -eq 0 ]]; then
    log "⚠️  无可自动修复的内化符号: ${manual}"
    return 1
  fi

  log "增量重链 PyTorch（${fixed} 个符号去 inline）..."
  run_in_target "${TGT_ENV} export CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} MAX_JOBS=${JOBS} CMAKE_POLICY_VERSION_MINIMUM=3.5 \
GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI} _GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI} \
USE_XNNPACK=0 USE_NNPACK=0 USE_PYTORCH_QNNPACK=0 USE_TENSORPIPE=${AK_USE_TENSORPIPE:-1} USE_KINETO=0 BUILD_CUSTOM_PROTOBUF=OFF && \
cd ${source_dir} && ${PYTHON_EXEC} setup.py bdist_wheel" || {
    log "⚠️  重链失败"
    return 1
  }
  local whl
  whl=$(latest_whl "${source_dir}/dist" "torch-*.whl")
  [[ -n "${whl}" ]] || { log "⚠️  未找到重链 whl"; return 1; }
  gate_whl_bisheng "${whl}" "torch/_C*.so"
  install_whl "${whl}"
  repair_torch_runtime_libs "${source_dir}"

  # 复核：全部符号必须已导出
  local libcpu sym remain=0
  libcpu=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch,os; print(os.path.dirname(torch.__file__)+\"/lib/libtorch_cpu.so\")' 2>/dev/null" || true)
  for sym in "${syms[@]}"; do
    local c
    c=$(run_in_target "nm -D ${libcpu} 2>/dev/null | grep -c ' ${sym}$'" || echo 0)
    [[ "${c}" =~ ^[0-9]+$ ]] || c=0
    if [[ "${c}" -eq 0 ]]; then
      log "⚠️  ${sym} 重链后仍未导出"
      remain=$((remain+1))
    fi
  done
  [[ ${remain} -eq 0 ]] && { log "内化符号修复完成（${fixed} 个已导出，torch_npu 无需重编）"; return 0; }
  log "⚠️  剩余未导出: ${remain} 个; ${manual}"
  return 1
}

# torchair 构建保障：torch_npu 的 setup.py 仅在 third_party/torchair/torchair
# 子模块非空时追加 -DBUILD_TORCHAIR=on（setup.py:344-351），子模块为空时
# **静默跳过** torchair 构建（无任何报错），wheel 照常产出 → import torchair
# 失败/inductor NPU 扩展缺失。构建前必须确保子模块在位。
verify_torchair_build() {
  local source_dir="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] torchair 子模块检查与补拉"
    return 0
  fi
  local ta="${source_dir}/third_party/torchair/torchair"
  if run_in_target "[ -d ${ta} ] && [ -n \"\$(ls -A ${ta} 2>/dev/null)\" ]"; then
    log "torchair 子模块在位（${ta}）"
    return 0
  fi
  log "⚠️  third_party/torchair/torchair 为空：setup.py 将静默跳过 torchair 构建（-DBUILD_TORCHAIR=on 不会生效）"
  log "自动补拉 torchair 子模块..."
  update_submodules "${source_dir}" "third_party/torchair/torchair"
  if run_in_target "[ -d ${ta} ] && [ -n \"\$(ls -A ${ta} 2>/dev/null)\" ]"; then
    log "torchair 子模块补拉完成"
    return 0
  fi
  fail_json "torchair_submodule_missing" \
    "\"path\":\"${ta}\"" \
    "\"recovery_steps\":[\"torchair 子模块补拉失败（网络/镜像问题）\",\"手动: cd ${source_dir} && git submodule update --init --recursive --force -- third_party/torchair/torchair\",\"镜像加速可用 --no-github-accelerate=false 与 update_submodules 回退逻辑\"]"
}

# 符号链接完整性（问题18：迁移拷贝破坏链接 → 产物不可用）：
#   1. 绝对路径链接 = 迁移敌对（容器 B 无该路径即断链）——目标在树内的自动改写为相对；
#      目标在树外的报错（须先把目标文件捆绑进树，参照 libprotobuf 捆绑模式）
#   2. 断链（readlink 目标不存在）= 已损坏，报错
#   3. 相对链接安全，仅记录进迁移清单
verify_symlink_integrity() {
  local base="$1"
  [[ "${DRY_RUN}" == true ]] && { log "[dry-run] 符号链接完整性: ${base}"; return 0; }
  local links
  links=$(run_in_target "find ${base} -type l" || true)
  [[ -z "${links}" ]] && { log "符号链接完整性: 无符号链接"; return 0; }

  local hazards="" dangling=0 abs_fixed=0 outside=0 l t
  while IFS= read -r l; do
    [[ -z "${l}" ]] && continue
    t=$(run_in_target "readlink ${l}" || true)
    if [[ "${t:0:1}" == "/" ]]; then
      case "${t}" in
        "${base}"|"${base}"/*)
          local ld
          ld=$(dirname "${l}")
          run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c \"import os; print(os.path.relpath('${t}', '${ld}'))\"" > /tmp/ak_relpath_$$
          local rel
          rel=$(cat /tmp/ak_relpath_$$); rm -f /tmp/ak_relpath_$$
          run_in_target "ln -sfn '${rel}' '${l}'"
          log "  绝对链接改写为相对（迁移安全）: ${l} -> ${rel}"
          abs_fixed=$((abs_fixed+1))
          ;;
        *)
          hazards+="树外绝对链接: ${l} -> ${t}; "
          outside=$((outside+1))
          ;;
      esac
    fi
    if ! run_in_target "[ -e ${l} ]"; then
      log "  ⚠️  断链: ${l} -> ${t}"
      dangling=$((dangling+1))
    fi
  done <<< "${links}"
  log "符号链接完整性: 改写绝对链接 ${abs_fixed} 个，断链 ${dangling} 个，树外链接 ${outside} 个"

  if [[ ${dangling} -gt 0 || -n "${hazards}" ]]; then
    fail_json "symlink_migration_hazard" \
      "\"base\":\"${base}\"" \
      "\"hazards\":\"${hazards}\"" \
      "\"dangling_count\":${dangling}" \
      "\"recovery_steps\":[\"迁移必须使用保留链接的方式: cp -a / tar(默认保留) / rsync -a；禁止 cp -r 跟随拷贝与 zip 打包\",\"树外绝对链接: 先将目标文件捆绑进树内（cp -aL + 相对链接，参照 libprotobuf 捆绑模式）再迁移\",\"断链: 确认源文件归属，缺失则从构建目录补齐\",\"修复后重跑本门禁复核\"]"
  fi
  return 0
}

# 产物可迁移性审计（问题17：A 容器编译 → B 容器使用）：
# 1) 解析核心 .so 的 NEEDED，分类：系统 / 毕昇运行时(libomp) / 外部(libgfortran)
# 2) 非系统依赖捆绑进 torch/lib（RPATH $ORIGIN 覆盖，自包含）——含 libgomp 类
#    GCC wheel 残留检测（历史 segfault 教训：误用 GCC wheel 自带 openblas）
# 3) 生成迁移清单 manifest（md5 + NEEDED 分类 + GLIBCXX 版本需求 + 容器 B 校验命令）
# 4) 模拟容器 B 校验：LD_LIBRARY_PATH 仅 torch/lib 的 ldd -r 全解析
audit_portability() {
  local torch_dir="$1"
  [[ "${DRY_RUN}" == true ]] && { log "[dry-run] 可迁移性审计与依赖捆绑"; return 0; }
  local libcpu="${torch_dir}/lib/libtorch_cpu.so"
  [[ -f $(run_in_target "ls ${libcpu}" 2>/dev/null) ]] || return 0

  local needed bundled="" name src md5
  needed=$(run_in_target "readelf -dW ${libcpu} 2>/dev/null | grep NEEDED | awk '{print \$5}' | tr -d '[]'" || true)
  log "libtorch_cpu NEEDED: $(printf '%s ' ${needed})"

  # 非系统 NEEDED 捆绑进 torch/lib（已存在则跳过）：
  #   libomp 必须取毕昇前缀的（系统 llvm libomp 版本错配有风险）；
  #   libgfortran 等静态 openblas 引入的运行时库 cp -aL 解引用符号链接；
  #   libc10/libtorch*/libshm 为 torch 自身组件（已在 torch/lib，非外部依赖）
  for name in ${needed}; do
    case "${name}" in
      libc.so.*|libm.so.*|libstdc++.so.*|libgcc_s.so.*|ld-linux*|libpthread*|libdl*|librt*) continue ;;
      libc10*|libtorch*|libshm*|libprotobuf*|libc10_nv*|libshm*) continue ;;
      libomp.so*)
        if ! run_in_target "[ -e ${torch_dir}/lib/${name} ]"; then
          src=$(run_in_target "ls ${BISHENG_PREFIX}/lib/libomp.so 2>/dev/null" || true)
          if [[ -n "${src}" ]]; then
            # 先清残留断链（历史 cp -a 按链接拷贝的产物；pip 重装不清 RECORD 外
            # 文件会残留）——cp -aL 目标为断链时按其引用路径写入必然失败
            run_in_target "rm -f ${torch_dir}/lib/${name}; cp -aL ${src} ${torch_dir}/lib/${name}" && bundled+="${name}(毕昇运行时) "
            log "捆绑 ${name} ← ${src}（毕昇运行时，禁用系统 llvm libomp 替代）"
          else
            log "⚠️  毕昇前缀无 libomp.so——目标容器需安装毕昇编译器运行时（见 playbook 运行时环境配置）"
          fi
        else
          bundled+="${name}(已随包) "
        fi
        ;;
      libgfortran.so*|libopenblas*|libgomp.so*)
        if ! run_in_target "[ -e ${torch_dir}/lib/${name} ]"; then
          src=$(run_in_target "ldd ${libcpu} 2>/dev/null | grep \"${name} =>\" | awk '{print \$3}' | head -1" || true)
          if [[ -n "${src}" ]]; then
            run_in_target "cp -aL ${src} ${torch_dir}/lib/${name}" && bundled+="${name} "
            log "捆绑 ${name} ← ${src}（目标容器零外部依赖）"
          fi
        else
          bundled+="${name}(已随包) "
        fi
        ;;
      *) log "⚠️  未分类 NEEDED: ${name}——请确认目标容器存在该库或补充捆绑规则" ;;
    esac
  done

  # GCC wheel 残留检测（历史 segfault：误用 GCC wheel 自带 openblas/omp）
  local stray
  stray=$(run_in_target "ls ${torch_dir}/lib/ | grep -E 'libopenblas|libgomp' | grep -v '^\${bundled}'" || true)
  [[ -n "${stray}" ]] && log "⚠️  torch/lib 存在 openblas/gomp 副本: ${stray}——须核对来源为本次捆绑，而非 GCC wheel 残留（版本错配会 segfault）"

  # 迁移清单（manifest）：md5 + NEEDED + GLIBCXX 需求 + 容器 B 校验命令
  local manifest="${torch_dir}/../ak_portability_manifest.json"
  local glibcxx
  glibcxx=$(run_in_target "strings ${libcpu} | grep -oE 'GLIBCXX_[0-9.]+' | sort -Vu | tail -1" || true)
  run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c '
import hashlib, json, os, sys
torch_lib = sys.argv[1] + \"/lib\"
needed = sys.argv[2].split()
glibcxx = sys.argv[3]
def md5(p):
    h = hashlib.md5()
    with open(p, \"rb\") as f:
        for c in iter(lambda: f.read(1 << 20), b\"\"):
            h.update(c)
    return h.hexdigest()
libs = {}
for f in sorted(os.listdir(torch_lib)):
    if f.endswith((\".so\", \".so.0\", \".so.5\")) or \".so.\" in f:
        libs[f] = md5(os.path.join(torch_lib, f))
sym = {}
for root, dirs, files in os.walk(sys.argv[1]):
    for f in files:
        p = os.path.join(root, f)
        if os.path.islink(p):
            sym[os.path.relpath(p, sys.argv[1])] = os.readlink(p)
m = {
  \"torch_lib_md5\": libs,
  \"symlinks\": sym,
  \"libtorch_cpu_needed\": needed,
  \"bundled_runtime\": [k for k in libs if k.startswith((\"libomp\", \"libgfortran\", \"libopenblas\", \"libgomp\"))],
  \"glibcxx_required\": glibcxx,
  \"container_b_checks\": [
    \"md5sum 逐一核对 torch_lib_md5（防 GCC wheel 残留/版本漂移）\",
    \"ldd -r libtorch_cpu.so 无 undefined symbol\",
    \"strings libtorch_cpu.so | grep GLIBCXX_ | sort -Vu | tail -1 <= 容器 B libstdc++ 支持版本（同镜像应一致）\",
    \"python3 -c \\\"import torch, torch_npu\\\" 无报错\",
  ],
}
open(sys.argv[4], \"w\").write(json.dumps(m, indent=2, ensure_ascii=False))
print(\"manifest:\", sys.argv[4])
' \"${torch_dir}\" \"${needed}\" \"${glibcxx}\" \"${manifest}\"" || true
  log "可迁移性审计完成（捆绑: ${bundled:-无}；清单: ${manifest}）"

  # 符号链接完整性（问题18：迁移拷贝破坏链接）
  verify_symlink_integrity "${torch_dir}"
}

# 构建后链接缺陷检测与自修复（单次重链合并修复）：
#   缺陷A new_qtensor 折叠：torch 2.5 中该函数为 inline 定义（linkonce_odr），
#     lld ThinLTO 会把仅被 DSO 内部引用的 inline 弱符号内部化（.local 克隆），
#     实测 -Wl,-u / --export-dynamic / --export-dynamic-symbol 均无法阻止
#     （lld 19.1.7）。唯一可靠修复：把定义改为外部链接（去 inline），重建对象
#     后增量重链。nm 复核动态导出 >=1。
#   缺陷B 未解析运行时符号：torch cmake 强制 BLA_STATIC，静态 libopenblas.a 的
#     Fortran LAPACK 成员被拉入后引用 _gfortran_*，但 DT_NEEDED 缺 libgfortran。
#     修复：SHARED/MODULE linker flags 追加 -Wl,--no-as-needed -lgfortran
#     （严禁波及 CMAKE_STATIC_LINKER_FLAGS/EXE——会污染 llvm-ar 命令）。
# 两个检测在首次产物上完成，修复合并为一次 touch CMakeLists.txt + 增量重链。
repair_link_defects() {
  local source_dir="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 链接缺陷检测（new_qtensor 内部化 / 未解析符号）与自修复"
    return 0
  fi
  local torch_dir libcpu
  torch_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch\"))' 2>/dev/null" || true)
  [[ -n "${torch_dir}" ]] || return 0
  libcpu="${torch_dir}/lib/libtorch_cpu.so"

  # 有界轮次循环：首轮检测可能被其他缺失掩盖（如 protobuf 缺失时 ldd -r
  # 不报 gfortran），修复-重链后新缺陷才显形 → 逐轮收敛，最多 3 轮
  local round newqt_fixed=false gfortran_fixed=false
  for round in 1 2 3; do
    local fixes=""
    local cnt
    cnt=$(run_in_target "nm -D ${libcpu} 2>/dev/null | grep -c new_qtensor" || echo 0)
    [[ "${cnt}" =~ ^[0-9]+$ ]] || cnt=0
    local need_newqt=false need_gfortran=false
    if [[ "${cnt}" -eq 0 ]]; then
      if [[ "${newqt_fixed}" == true ]]; then
        fail_json "newqtensor_still_folded" \
          "\"recovery_steps\":[\"重链后符号仍未导出，确认 Quantizer.cpp 的 inline 已移除且对象重新编译\"]"
      fi
      log "⚠️  new_qtensor 已被 lld ThinLTO 内部化（torch_npu import 将报 undefined symbol）"
      need_newqt=true
    else
      log "new_qtensor 符号复核通过（未折叠，导出 ${cnt} 处）"
    fi

    local und
    # 解析环境与运行时契约一致：TGT_ENV（LD_LIBRARY_PATH 含毕昇 lib，profile.d
    # 同源）+ torch/lib 前置。裸 `LD_LIBRARY_PATH=torch/lib ldd -r` 会把 libomp
    # 提供的 OpenMP 符号（__kmpc_*/omp_*）误报为未解析（实测踩坑）
    und=$(run_in_target "${TGT_ENV} LD_LIBRARY_PATH=${torch_dir}/lib:\${LD_LIBRARY_PATH:-} ldd -r ${libcpu} 2>/dev/null" | grep 'undefined symbol' | sed 's/.*undefined symbol: \([^ ]*\).*/\1/' | sort -u || true)
    if [[ -z "${und}" ]]; then
      log "运行时符号解析检查: 全部可解析"
    else
      log "⚠️  检出未解析运行时符号: $(printf '%s ' ${und})"
      if printf '%s' "${und}" | grep -q '^_gfortran'; then
        if [[ "${gfortran_fixed}" == true ]]; then
          fail_json "runtime_symbols_unresolved" "\"undefined\":\"${und}\"" 
        fi
        need_gfortran=true
      fi
    fi

    if [[ "${need_newqt}" == false && "${need_gfortran}" == false && -z "${und}" ]]; then
      log "链接缺陷收敛（第 ${round} 轮）：产物健康"
      return 0
    fi

    if [[ "${need_newqt}" == true ]]; then
      local qsrc="${source_dir}/aten/src/ATen/quantized/Quantizer.cpp"
      run_in_target "[ -f ${qsrc} ]" || fail_json "newqtensor_patch_no_source" \
        "\"recovery_steps\":[\"${qsrc} 不存在（torch 版本差异），请定位 new_qtensor 的 inline 定义文件后手工去 inline\"]"
      log "修复A: 去除 Quantizer.cpp 中 new_qtensor 的 inline 定义（备份 .bak_inline）..."
      run_in_target "cp -n ${qsrc} ${qsrc}.bak_inline 2>/dev/null; grep -q '^inline Tensor new_qtensor(' ${qsrc} && sed -i 's/^inline Tensor new_qtensor(/Tensor new_qtensor(/' ${qsrc}"
      fixes="new_qtensor"
    fi
    if [[ "${need_gfortran}" == true ]]; then
      log "修复B: SHARED/MODULE linker flags 追加 -Wl,--no-as-needed -lgfortran..."
      cmake_cache_append_linker_flags "${source_dir}/build/CMakeCache.txt" "-Wl,--no-as-needed -lgfortran"
      run_in_target "touch ${source_dir}/CMakeLists.txt"
      fixes="${fixes:+${fixes}+}gfortran"
    fi
    if [[ -z "${fixes}" && -n "${und}" ]]; then
      fail_json "runtime_symbols_unresolved" "\"undefined\":\"${und}\"" \
        "\"recovery_steps\":[\"非 _gfortran 类未解析符号需按符号归属逐个定位（缺库则补装，缺 NEEDED 则检查链接顺序）\"]"
    fi

    log "增量重链（第 ${round} 轮，应用修复: ${fixes}）..."
    run_in_target "touch ${source_dir}/CMakeLists.txt"
    local tp_flag="${AK_USE_TENSORPIPE:-1}"
    run_in_target "${TGT_ENV} export CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} MAX_JOBS=${JOBS} CMAKE_POLICY_VERSION_MINIMUM=3.5 \
GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI:-1} _GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI:-1} \
USE_XNNPACK=0 USE_NNPACK=0 USE_PYTORCH_QNNPACK=0 USE_TENSORPIPE=${tp_flag} USE_KINETO=0 BUILD_CUSTOM_PROTOBUF=OFF && \
cd ${source_dir} && ${PYTHON_EXEC} setup.py bdist_wheel"

    local whl2
    whl2=$(latest_whl "${source_dir}/dist" "torch-*.whl")
    [[ -n "${whl2}" ]] || fail_json "whl_not_found" "\"source_dir\":\"${source_dir}\""
    log "重链完成: ${whl2}"
    gate_whl_bisheng "${whl2}" "torch/_C*.so"
    install_whl "${whl2}"
    repair_torch_runtime_libs "${source_dir}"
    [[ "${need_newqt}" == true ]] && newqt_fixed=true
    [[ "${need_gfortran}" == true ]] && gfortran_fixed=true
  done
  fail_json "link_defects_not_converged" \
    "\"recovery_steps\":[\"3 轮未收敛，按日志逐项人工定位（每轮的未解析符号清单见上）\"]"
}

# 64KB 页内核加载性判定（与宿主机页大小无关，纯布局判定）：
#   aarch64 鲲鹏内核普遍 64KB 页。链接期布局由 lld max-page-size（aarch64 默认
#   0x10000）决定，与宿主机 4K/64K 无关——因此本判定在任何宿主机上都能确定性地
#   回答"64KB 页内核能否加载"。三项检查：LOAD 段对齐 >= 0x10000；
#   offset ≡ vaddr (mod p_align)；段区间无重叠。
#   历史问题 "--no-rosegment"（lld 旧版 RELRO+rodata 段重叠，LLVM<17）会在
#   重叠检查中暴露，若命中配合 rosegment 重试逻辑即可，无需预设该旗标。
verify_elf_page_compat() {
  local so="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] ELF 64KB 页布局判定: ${so}"
    return 0
  fi
  run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c \"import re, subprocess, sys
so = sys.argv[1]
out = subprocess.run(['readelf', '-lW', so], capture_output=True, text=True).stdout
loads = []
for L in out.splitlines():
    m = re.match(r'\\s*LOAD\\s+(0x[0-9a-f]+)\\s+(0x[0-9a-f]+)\\s+\\S+\\s+(0x[0-9a-f]+)\\s+(0x[0-9a-f]+)\\s+(R ?E? ?W?)\\s+(0x[0-9a-f]+)\\s*\$', L)
    if m:
        loads.append((int(m.group(2),16), int(m.group(1),16), int(m.group(4),16), int(m.group(6),16), m.group(5)))
bad = []
for va, off, memsz, align, flg in loads:
    if align < 0x10000: bad.append(f'align {hex(align)} < 64KB @VA {hex(va)}')
    if (va - off) % align != 0: bad.append(f'congruence fail @VA {hex(va)}')
sl = sorted(loads)
for a, b in zip(sl, sl[1:]):
    if a[0] + a[2] > b[0]: bad.append(f'overlap {hex(a[0]+a[2])} > {hex(b[0])}')
if bad:
    print('FAIL: ' + '; '.join(bad)); sys.exit(1)
print(f'64KB 页布局判定通过（{len(loads)} 个 LOAD 段）')
\" \"${so}\"" || fail_json "elf_page_compat_failed" \
    "\"artifact\":\"${so}\"" \
    "\"recovery_steps\":[\"检查产物段布局: readelf -lW ${so}\",\"若 align < 64KB，确认链接器未覆盖 max-page-size\",\"若为旧版 lld 段重叠，脚本会先触发 --no-rosegment 重试\"]"
}

# ── whl 产物毕昇标识门禁 ─────────────────────────────────────

# 安装前从 whl 解出 .so 复核 .comment 段，非毕昇产物直接失败，
# 避免 GCC 编译物被 pip 装进环境造成"看似成功"的假象
gate_whl_bisheng() {
  local whl="$1"
  local so_glob="$2"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] whl 毕昇标识门禁: ${so_glob}"
    return 0
  fi
  local tmpd="/tmp/ak_whl_gate_$$"
  run_in_target "rm -rf ${tmpd} && mkdir -p ${tmpd} && ${PYTHON_EXEC} -c \"import zipfile; zipfile.ZipFile('${whl}').extractall('${tmpd}')\""
  local marks
  marks=$(count_bisheng_marks "${tmpd}/${so_glob}")
  run_in_target "rm -rf ${tmpd}"
  if [[ "${marks}" -eq 0 ]]; then
    fail_json "not_bisheng_built" \
      "\"artifact\":\"${whl}\"" \
      "\"recovery_steps\":[\"确认编译阶段 CC/CXX 指向毕昇 clang（脚本已自动解析并注入）\",\"检查构建日志中实际使用的编译器路径\",\"清理 ${SOURCE_DIR}/build 后重新执行本命令\"]"
  fi
  log "whl 毕昇标识复核通过（${marks} 处 bisheng 标记）"
}

install_whl() {
  local whl="$1"
  log "安装 ${whl} ..."
  run_in_target "${TGT_ENV} ${PYTHON_EXEC} -m pip install -q ${whl} --force-reinstall --no-deps"
}

# ── Profile 检查 ─────────────────────────────────────────────

check_profile() {
  local profile_dir="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] profile 有效性检查（真实运行时此处校验 profraw 数量/插桩段/杂散文件）"
    return 0
  fi
  log "检查 profile 目录: ${profile_dir}"

  # 分诊诊断：插桩复核 + 杂散 profraw 搜索（profile 写到别处/CWD 的常见情形）
  local diag_inst=0 diag_stray="" lib_hint=""
  case "${COMPONENT}" in
    pytorch)   lib_hint=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch,os; print(os.path.dirname(torch.__file__)+\"/lib/libtorch_cpu.so\")' 2>/dev/null" || true) ;;
    torch_npu) lib_hint=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu,os; print(os.path.dirname(torch_npu.__file__)+\"/lib/libtorch_npu.so\")' 2>/dev/null" || true) ;;
  esac
  if [[ "${DRY_RUN}" != true && -n "${lib_hint}" ]]; then
    diag_inst=$(count_llvm_prf_sections "${lib_hint}")
    [[ "${diag_inst}" =~ ^[0-9]+$ ]] || diag_inst=0
  fi
  diag_stray=$(run_in_target "ls ./*.profraw 2>/dev/null | head -3; find /tmp /root /home -maxdepth 2 -name '*.profraw' 2>/dev/null | grep -v \"^${profile_dir}\" | head -5" 2>/dev/null || true)
  diag_stray=$(printf '%s' "${diag_stray}" | tr '\n' ';' | cut -c1-300)

  local profraw_files
  profraw_files=$(run_in_target "ls -la ${profile_dir}/*.profraw 2>/dev/null || echo 'NONE'" || true)

  if [[ "${profraw_files}" == *"NONE"* ]]; then
    cat <<EOF
{"component":"${COMPONENT}","optimize":"${OPTIMIZE}","stage":"${STAGE}","status":"failed","fail_reason":"no_profraw","diagnostics":{"profile_dir":"${profile_dir}","profraw_count":0,"instrumented_sections":${diag_inst},"stray_profraw":"${diag_stray}","expected_env":"LLVM_PROFILE_FILE=${profile_dir}/default_%m_%p.profraw","possible_causes":["workload 未运行或秒退（缺 CANN env/模型路径错/依赖缺失）","运行的是 PGO2/非插桩二进制（instrumented_sections=0 即是）","LLVM_PROFILE_FILE 未设置或被多进程框架重建丢弃（vLLM v1 需 sitecustomize.py 注入）","profile 写到了运行目录等其他位置（见 stray_profraw）"],"recovery_steps":["前台直接运行 workload 观察报错，排除秒退","确认运行的是插桩版：instrumented_sections > 0","export OMP_PROC_BIND=false; export LLVM_PROFILE_FILE=${profile_dir}/default_%m_%p.profraw 后重跑 workload","多进程框架（vLLM）用 sitecustomize.py 注入并一次性 SIGINT 全进程，见 playbook","若 stray_profraw 非空：移入 ${profile_dir} 后重跑本命令","确认后重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize lto_pgo --stage profile_use"]},"retryable":true}
EOF
    return 1
  fi

  local profraw_count
  profraw_count=$(run_in_target "ls ${profile_dir}/*.profraw 2>/dev/null | wc -l" || echo 0)

  local empty_count
  empty_count=$(run_in_target "find ${profile_dir} -name '*.profraw' -size 0 2>/dev/null | wc -l" || echo 0)

  if [[ "${profraw_count}" =~ ^[0-9]+$ && "${empty_count}" =~ ^[0-9]+$ && "${profraw_count}" -eq "${empty_count}" ]]; then
    cat <<EOF
{"component":"${COMPONENT}","optimize":"${OPTIMIZE}","stage":"${STAGE}","status":"failed","fail_reason":"empty_profraw","diagnostics":{"profile_dir":"${profile_dir}","profraw_count":${profraw_count},"empty_count":${empty_count},"instrumented_sections":${diag_inst},"possible_causes":["workload 运行时间过短，profile 计数未写入","进程异常退出（SIGTERM/SIGKILL 不触发 atexit 刷盘）","运行的是 PGO2/非插桩二进制（不产 profraw 计数）"],"recovery_steps":["延长 workload 覆盖（prefill+decode 等主要路径）","对全部 workload 进程一次性 SIGINT 优雅退出（多进程列表见 playbook）","确认 instrumented_sections > 0","重新运行模型后重跑本命令"]},"retryable":true}
EOF
    return 1
  fi

  log "Profile 文件检查通过（${profraw_count} 个 .profraw）"

  # ── profile 形态执行门禁（manifest 驱动）──────────────────────
  # 实测校准(384 核鲲鹏 950): 集中度/覆盖数均无法判别窄负载 profile
  # （pure-matmul 与均衡混合的 top-10 质量占比 0.92 vs 0.89、覆盖函数数
  # 22501 vs 23061——import 期初始化流量污染计数）。可判别的是**计数幅度**:
  # 未真实执行的形态其核心函数仅有 1~10 的注册流量，真实执行则积累数百+。
  # 因此门禁改为: 标准采集脚本写入 workload_manifest.json（声明执行了哪些
  # 形态 + 各自的 marker 与最小计数），此处逐形态校验热函数计数；无 manifest
  # 时仅输出建议性集中度。注意: 本门禁拦截"采集未跑成/形态未执行"，
  # 不判"负载是否匹配生产"——后者由多负载基准验收负责。
  # 稳定性设计: 门禁 python/合并失败一律跳过（fail-open，不影响构建走通）；
  # marker 随版本漂移误判时 AK_PGO_SKIP_THRESHOLD_CHECK=1 逃生舱（与旧
  # 阈值门禁共用）。
  local pd_tool
  pd_tool=$(profdata_tool)
  local conc_out conc_share="n/a"
  local manifest="${profile_dir}/workload_manifest.json"
  local pd_tmp="${profile_dir}/.gate_$$.profdata"
  if run_in_target "${pd_tool} merge -output='${pd_tmp}' ${profile_dir}/*.profraw" >/dev/null 2>&1; then
    conc_out=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c \"import re, subprocess
out = subprocess.run(['${pd_tool}', 'show', '${pd_tmp}', '--all-functions', '--counts'], capture_output=True, text=True).stdout
masses = []
for m in re.findall(r'Block counts: \[([0-9, ]*)\]', out):
    nums = [int(x) for x in m.split(',') if x.strip()]
    masses.append(sum(nums))
masses = sorted(masses, reverse=True)
total = sum(masses)
share = sum(masses[:10]) / total if total > 0 else 1.0
print(f'{share:.4f}')
\"" || true)
    conc_share=$(printf '%s' "${conc_out}" | tail -1)
    [[ "${conc_share}" =~ ^[0-9.]+$ ]] || conc_share="n/a"
    log "Profile 集中度（建议值，不作为门禁）: top-10 质量占比 ${conc_share}"

    if run_in_target "[ -f '${manifest}' ]" && [[ "${AK_PGO_SKIP_THRESHOLD_CHECK:-0}" != "1" ]]; then
      local gate_out="/tmp/ak_gate_out.$$"
      run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c \"import json, re, subprocess
mf = json.load(open('${manifest}'))
out = subprocess.run(['${pd_tool}', 'show', '${pd_tmp}', '--all-functions', '--counts'], capture_output=True, text=True).stdout
entries = []
for m in re.finditer(r'([^\\\\s:][^\\\\n]*?):\\\\n\\\\s*Hash: 0x[0-9a-f]+\\\\n\\\\s*Counters: [0-9]+\\\\n\\\\s*Block counts: \\\\[([0-9, ]*)\\\\]', out):
    nums = [int(x) for x in m.group(2).split(',') if x.strip()]
    if nums:
        entries.append((m.group(1), nums[0]))
bad_shapes = []
for shape, spec in mf.get('shapes', {}).items():
    marker = spec.get('marker', '')
    min_entry = int(spec.get('min_entry', 100))
    hot = sum(1 for n, e in entries if marker and re.search(marker, n) and e >= min_entry)
    print('shape %s: hot=%d min_entry=%d' % (shape, hot, min_entry))
    if hot == 0:
        bad_shapes.append(shape)
if bad_shapes:
    print('GATE_FAIL:' + ','.join(bad_shapes))
\"" >"${gate_out}" 2>/dev/null || true
      sed 's/^/  [gate] /' "${gate_out}" >&2 || true
      if grep -q "GATE_FAIL" "${gate_out}"; then
        local failed_shapes
        failed_shapes=$(grep "GATE_FAIL" "${gate_out}" | cut -d: -f2)
        rm -f "${gate_out}" "${pd_tmp}"
        fail_json "profile_shape_not_executed" \
          "\"failed_shapes\":\"${failed_shapes}\"" \
          "\"manifest\":\"${manifest}\"" \
          "\"recovery_steps\":[\"采集负载未有效执行形态: ${failed_shapes}（其核心函数计数仅 import 注册流量量级）\",\"检查采集脚本是否早退/权重配错/运行了错误二进制\",\"用标准脚本重采: scripts/ak_profile_workload.py --weights <按目标形态配比>\",\"marker 随版本漂移误判时强过门禁: AK_PGO_SKIP_THRESHOLD_CHECK=1\",\"确认后重新执行 --stage profile_use\"]"
      fi
      rm -f "${gate_out}" "${pd_tmp}"
    else
      run_in_target "rm -f '${pd_tmp}'" || true
      log "（未发现 workload_manifest.json——非标准脚本采集，形态执行校验跳过；建议用 scripts/ak_profile_workload.py 采集可自动校验）"
    fi
  else
    log "⚠️  profdata 合并失败，跳过 profile 形态门禁（PGO2 阶段 merge_profile 仍会硬校验）"
  fi
  return 0
}

# CSS PGO (BRBE) functions from master

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
  local profgen
  if [[ "$DRY_RUN" == true ]]; then LLVM_PROFGEN=llvm-profgen; return 0; fi
  if target_exec test -x "${BISHENG_PREFIX}/bin/llvm-profgen"; then
    profgen="${BISHENG_PREFIX}/bin/llvm-profgen"
  else
    profgen=$(run_in_target 'command -v llvm-profgen' || true)
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
  if ! target_exec test -s "$profile_file"; then
    cat <<EOF
{"status":"failed","fail_reason":"css_profile_not_found","diagnostics":{"profile":"${profile_file}","possible_causes":["gen 阶段产物未安装/未运行（collect 采样的是旧二进制则 profile 无效）","collect 阶段未执行，或 llvm-profgen 产出为空","gen 构建缺完整 -g（共享库 -gline-tables-only 不够，DWARF 缺失导致空 profile）","--css-profile 路径配错"],"recovery_steps":["执行采集: ak_compile_optimize.sh --component ${COMPONENT} --optimize css_pgo --stage collect --pid <pid> --binary <gen产物>","确认 gen 构建含 -fpseudo-probe-for-profiling -g","重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize css_pgo --stage use --css-profile <file>"]},"retryable":true}
EOF
    return 1
  fi
  local size=0 func_count=0
  if [[ "${DRY_RUN}" != true ]]; then
    size=$(target_exec bash -c 'wc -c < "$1"' bash "$profile_file")
    func_count=$(target_exec grep -cE '^[^[:space:]!][^:]*:[0-9]+:[0-9]+' -- "$profile_file" || true)
    func_count="${func_count:-0}"
  fi
  if [[ "${DRY_RUN}" != true && "${func_count}" -eq 0 ]]; then
    log "⚠️  profile 无函数归属（${size} 字节），疑似空 profile（常见根因: gen 构建缺完整 -g / DWARF，llvm-profgen 枚举不到函数）"
    return 1
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
  target_exec mkdir -p -- "$PROFILE_DIR"
  local real
  real=$(target_exec bash -c '
    probe=$(mktemp "$1/brbe_probe.XXXXXX") || exit 1
    (while :; do :; done) & wp=$!
    cleanup_probe() { kill "$wp" 2>/dev/null; wait "$wp" 2>/dev/null; rm -f -- "$probe"; }
    trap cleanup_probe EXIT
    sleep 0.3
    perf record -g --call-graph fp -e cycles:u -j any,u -o "$probe" -p "$wp" -- sleep 3 || exit 1
    perf script -F brstack -i "$probe" | grep -cE "0x[1-9a-f][0-9a-f]+/0x[1-9a-f][0-9a-f]+"
  ' bash "$PROFILE_DIR") || real=0
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
  if [[ -n "$PID" && -n "$COLLECT_CMD" ]]; then fail "--pid 和 --cmd 只能选择一个"; fi
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

  if [[ -n "${PID}" && "${DRY_RUN}" != true ]] && ! target_exec test -d "/proc/$PID"; then
    fail "目标进程不存在: ${PID}（须先用 gen 产物启动真实负载）"
  fi
  if ! target_exec test -f "$BINARY_PATH"; then
    fail "采样二进制不存在: ${BINARY_PATH}（须为 gen 阶段产物，含完整 DWARF，否则 profile 为空）"
  fi
  if [[ "${DRY_RUN}" != true ]]; then
    # llvm-profgen 依据 perf mmap 事件里的文件名匹配二进制；若 --binary 是软链
    # （如 python3 -> python3.10），basename 与 mmap 名不一致会静默产出 0 样本空 profile。
    local resolved_binary
    resolved_binary="$(target_exec readlink -f -- "$BINARY_PATH")"
    # 存在性判断同样在目标上下文执行（--container 时宿主机看不到容器内路径）
    if [[ -n "${resolved_binary}" && "${resolved_binary}" != "${BINARY_PATH}" ]] && target_exec test -f "$resolved_binary"; then
      log "采样二进制为软链，解析为真实路径: ${resolved_binary}"
      BINARY_PATH="${resolved_binary}"
    fi
  fi
  target_exec mkdir -p -- "$PROFILE_DIR"
  target_exec rm -f -- "$perf_data" "$perf_script"

  check_llvm_profgen || return 1
  check_brbe_support || return 1

  log "perf record（BRBE 分支采样，帧指针调用栈）..."
  local record_rc=0
  if [[ -n "${COLLECT_CMD}" ]]; then
    if ! target_exec perf record -g --call-graph fp -e cycles:u -j any,u -o "$perf_data" -- bash -c "$COLLECT_CMD"; then
      record_rc=1
    fi
  else
    if ! target_exec perf record -g --call-graph fp -e cycles:u -j any,u -o "$perf_data" -p "$PID" -- sleep "$COLLECT_DURATION"; then
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
  if ! target_exec bash -c 'perf script -F ip,brstack --show-mmap-event -i "$1" > "$2"' bash "$perf_data" "$perf_script"; then
    fail "perf script 导出失败"
  fi

  # 前置校验：perf mmap 事件中必须出现该二进制名，否则 llvm-profgen 会静默产出空 profile
  # （grep 与文件生成同上下文执行：perf.script 写在目标上下文，--container 时宿主机路径不存在，
  #  宿主机 grep 会恒定误报 binary_mmap_mismatch）
  # grep 退出码分流：1 = 名不匹配（语义结果）；2/其他 = 文件不存在或目标执行失败（另一类原因，
  #  单独 fail_reason，避免同一诊断覆盖两类根因）
  if [[ "${DRY_RUN}" != true ]]; then
    local bin_base
    bin_base="$(basename "${BINARY_PATH}")"
    local mmap_rc=0
    # 按字面匹配 mmap 文件名，保留空格和标点。
    target_exec grep -aqF -- "$bin_base" "$perf_script" || mmap_rc=$?
    if [[ "${mmap_rc}" -ne 0 ]]; then
      if [[ "${mmap_rc}" -eq 1 ]]; then
        cat <<EOF
{"status":"failed","fail_reason":"binary_mmap_mismatch","diagnostics":{"binary":"${BINARY_PATH}","perfscript":"${perf_script}","hint":"perf script 的 mmap 事件中未找到 ${bin_base}，llvm-profgen 无法把样本映射到二进制；常见原因是 --binary 与进程实际加载的文件名不一致（软链/重命名/tmp 副本）"},"retryable":true}
EOF
      else
        cat <<EOF
{"status":"failed","fail_reason":"perfscript_missing","diagnostics":{"perfscript":"${perf_script}","hint":"perf script 文件在目标上下文不存在或不可读（grep rc=${mmap_rc}）：与 mmap 名不匹配是两类根因；检查 --profile-dir 在目标上下文可写、perf script 导出是否实际落盘"},"retryable":true}
EOF
      fi
      return 1
    fi
  fi

  local page_size_arg=()
  if [[ -n "${PAGE_SIZE}" ]]; then
    page_size_arg=("-page-size=$PAGE_SIZE")
  fi

  log "llvm-profgen 生成 sample profile..."
  if ! target_exec "$LLVM_PROFGEN" "--binary=$BINARY_PATH" "--perfscript=$perf_script" "--output=$profile_file" --format=text ${page_size_arg[@]+"${page_size_arg[@]}"}; then
    cat <<EOF
{"status":"failed","fail_reason":"profgen_failed","diagnostics":{"binary":"${BINARY_PATH}","perfscript":"${perf_script}","hints":["共享库必须完整 -g（-gline-tables-only 不够）","部分 llvm-profgen 版本无 -page-size 选项，此时不传 --page-size","Invalid instructions 告警可忽略，整体失败需检查 perfscript 格式"],"retryable":true}
EOF
    return 1
  fi

  # 空 profile 判定按函数归属（顶层 "name:total:entry" 行数），不按文件大小：
  # 小二进制/小组件的有效 profile 可远小于 10KB，按大小误判会拒绝有效产物。
  local size=0 func_count=0
  if [[ "${DRY_RUN}" != true ]]; then
    size=$(target_exec bash -c 'wc -c < "$1"' bash "$profile_file")
    func_count=$(target_exec grep -cE '^[^[:space:]!][^:]*:[0-9]+:[0-9]+' -- "$profile_file" || true)
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


profdata_tool() {
  if run_in_target "[ -x ${BISHENG_PREFIX}/bin/llvm-profdata ]" 2>/dev/null; then
    printf '%s' "${BISHENG_PREFIX}/bin/llvm-profdata"
  else
    printf '%s' "llvm-profdata"
  fi
}

merge_profile() {
  local profile_dir="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] profraw 合并为 default.profdata"
    return 0
  fi
  local pd_tool
  pd_tool=$(profdata_tool)

  # 验证留痕：工具路径与版本必须与插桩 clang 同源，否则可能 invalid profile
  local pd_ver
  pd_ver=$(run_in_target "${TGT_ENV} ${pd_tool} --version 2>/dev/null | head -1" || true)
  log "profdata 工具: ${pd_tool}"
  log "profdata 工具版本: ${pd_ver}"
  if [[ "${pd_tool}" != "${BISHENG_PREFIX}"* && "${DRY_RUN}" != true ]]; then
    log "⚠️  使用非毕昇自带的 llvm-profdata，与插桩 clang 版本可能不匹配；若报 invalid profile，请将毕昇 bin 加入 PATH 后重跑"
  fi

  # 快路径：整目录合并；失败（单个损坏 profraw 即可毁掉整批）转逐文件容错合并
  local merge_err="" tol_out=""
  if ! merge_err=$(run_in_target "${TGT_ENV} ${pd_tool} merge ${profile_dir} -o ${profile_dir}/default.profdata" 2>&1); then
    log "⚠️  目录整体合并失败: $(printf '%s' "${merge_err}" | head -2 | tr '\n' ';')"
    log "切换逐文件容错合并（跳过损坏的 .profraw）..."
    if ! tol_out=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c '
import glob, os, subprocess, sys
pd, d = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.join(d, \"*.profraw\")))
ok, bad = [], []
for f in files:
    r = subprocess.run([pd, \"show\", f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (ok if r.returncode == 0 else bad).append(f)
if not ok:
    print(\"NONE_VALID\"); sys.exit(1)
tmp = [os.path.join(d, \".ak_merge_a.profdata\"), os.path.join(d, \".ak_merge_b.profdata\")]
acc = None
for i in range(0, len(ok), 50):
    batch = ok[i:i+50]
    dst = tmp[i // 50 % 2]
    args = [pd, \"merge\"] + batch + ([acc] if acc else []) + [\"-o\", dst]
    r = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        print(\"BATCH_FAIL: \" + r.stderr.decode(errors=\"replace\")[:200]); sys.exit(1)
    acc = dst
os.replace(acc, os.path.join(d, \"default.profdata\"))
print(f\"tolerant_merge_done ok={len(ok)} skipped={len(bad)}\")
for f in bad:
    print(\"SKIPPED_CORRUPT: \" + f)
' \"${pd_tool}\" \"${profile_dir}\"" 2>&1); then
      case "${tol_out}" in
        *NONE_VALID*) fail_json "profdata_none_valid" \
          "\"profile_dir\":\"${profile_dir}\"" \
          "\"recovery_steps\":[\"全部 .profraw 均损坏（常见：进程被 SIGKILL 未刷盘）\",\"重新采集：插桩版运行 workload 并一次性 SIGINT 全进程\"]" ;;
        *BATCH_FAIL:*) fail_json "profdata_merge_batch_failed" \
          "\"detail\":\"$(printf '%s' "${tol_out}" | grep -o 'BATCH_FAIL: .*' | head -1)\"" ;;
        *) fail_json "profdata_merge_tolerant_failed" "\"detail\":\"$(printf '%s' "${tol_out}" | tail -2 | tr '\n' ';')\"" ;;
      esac
    fi
    printf '%s\n' "${tol_out}" | grep 'tolerant_merge_done' | while IFS= read -r l; do log "容错合并完成: ${l#tolerant_merge_done }"; done
    printf '%s\n' "${tol_out}" | grep 'SKIPPED_CORRUPT' | while IFS= read -r l; do log "  跳过损坏: ${l#SKIPPED_CORRUPT: }"; done
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    return 0
  fi

  local profdata_size
  profdata_size=$(run_in_target "stat -c %s ${profile_dir}/default.profdata 2>/dev/null" || echo 0)
  [[ "${profdata_size}" =~ ^[0-9]+$ ]] || profdata_size=0
  if [[ "${profdata_size}" -eq 0 ]]; then
    fail_json "profdata_merge_failed" \
      "\"profile_dir\":\"${profile_dir}\"" \
      "\"tool\":\"${pd_tool}\"" \
      "\"recovery_steps\":[\"检查 .profraw 文件是否有效\",\"手动执行: ${pd_tool} merge ${profile_dir} -o ${profile_dir}/default.profdata\",\"重新执行: ak_compile_optimize.sh --component ${COMPONENT} --optimize lto_pgo --stage profile_use\"]"
  fi

  # playbook「PGO Profile 生成工作流」有效性阈值：
  # profile >50MB 且函数数 >100000，不满足禁止 -fprofile-use（否则负收益）
  if [[ "${AK_PGO_SKIP_THRESHOLD_CHECK:-0}" != "1" ]]; then
    local min_bytes=$((50 * 1024 * 1024))
    if [[ "${profdata_size}" -lt "${min_bytes}" ]]; then
      fail_json "profile_coverage_insufficient" \
        "\"profdata_size\":${profdata_size}" \
        "\"threshold\":\"size>=52428800\"" \
        "\"recovery_steps\":[\"用目标负载重新采集（更长时间/更多请求覆盖 prefill+decode 等主要路径）\",\"确认采集用的是插桩版（PGO1）二进制，PGO2 二进制不产 profraw\",\"确需跳过: AK_PGO_SKIP_THRESHOLD_CHECK=1\"]"
    fi
    local func_lines
    # ⚠️ 必须带 --all-functions：缺省只输出摘要行（实测 70MB 真实 profile 仅得 4 行，
    # 导致真实 profile 被阈值门误拒——问题11 遗留 bug，实验中发现并修正）
    func_lines=$(run_in_target "${TGT_ENV} ${pd_tool} show --all-functions --counts ${profile_dir}/default.profdata 2>/dev/null | wc -l" || echo 0)
    [[ "${func_lines}" =~ ^[0-9]+$ ]] || func_lines=0
    if [[ "${func_lines}" -lt 100000 ]]; then
      fail_json "profile_coverage_insufficient" \
        "\"profdata_functions\":${func_lines}" \
        "\"threshold\":\"functions>=100000\"" \
        "\"recovery_steps\":[\"用目标负载重新采集\",\"确需跳过: AK_PGO_SKIP_THRESHOLD_CHECK=1\"]"
    fi
    log "Profile 有效性验证通过: size=${profdata_size} bytes, functions>=${func_lines}, tool=${pd_tool} (${pd_ver})"
  else
    log "⚠️  已跳过 PGO 有效性阈值检查（AK_PGO_SKIP_THRESHOLD_CHECK=1）: size=${profdata_size}"
  fi
  return 0
}

# 预期耗时参考（384 核/374 并行实测基线，按并行度线性缩放；仅供预期管理，
# 编译耗时属预期行为，任何阶段不得因耗时跳过或擅自降级优化）
log_eta_reference() {
  local component="$1" stage="$2"
  local base_min
  case "${component}:${OPTIMIZE}:${stage}" in
    python:*:*)            base_min=13 ;;
    pytorch:lto:*)         base_min=15 ;;
    pytorch:lto_pgo:profile_gen) base_min=16 ;;
    pytorch:lto_pgo:profile_use) base_min=20 ;;
    torch_npu:lto:*)       base_min=15 ;;
    torch_npu:lto_pgo:*)   base_min=20 ;;
    *)                     base_min=20 ;;
  esac
  local est=$(( base_min * 374 / JOBS ))
  (( est < base_min )) && est=${base_min}
  log "预期耗时参考: ~${est} 分钟（${JOBS} 并行，基线 ${base_min} 分钟@374 并行；大内存机器更高）。"
  log "⚠️  编译耗时属预期行为：禁止因耗时跳过编译阶段、降低优化等级或擅自变更构建参数；"
  log "    建议后台运行 + 定期轮询 --log-file（ninja 进度 [n/m] 即实时进度）；中断后可用 --reuse-build 增量续编"
}

# ThinLTO 后端缓存：显著加速修复性重链/增量重链（首次全量链接无收益）
thinlto_cache_flags() {
  if [[ "${THINLTO_CACHE}" == "1" && -n "${SOURCE_DIR}" ]]; then
    printf -- '-Wl,--thinlto-cache-dir=%s/.thinlto-cache ' "${SOURCE_DIR}"
  fi
}

# ── 编译函数 ─────────────────────────────────────────────────

compile_python() {
  local source_dir="$1"
  local install_dir="$2"

  local pgo_flags=""
  case "${OPTIMIZE}" in
    css_pgo)
      local probe_flags="-fpseudo-probe-for-profiling $(mcpu_flag)"
      local cc_flags="-O3 -fno-omit-frame-pointer $probe_flags"
      local ld_flags="-flto -fuse-ld=lld $probe_flags"
      if [[ "$STAGE" == gen ]]; then
        cc_flags+=" -g"
      else
        local profile_flag="-fprofile-sample-use=$(css_profile_path)"
        cc_flags+=" $profile_flag"
        ld_flags+=" $profile_flag"
      fi
      pgo_flags="--with-lto CFLAGS=$(printf '%q' "$cc_flags") LDFLAGS=$(printf '%q' "$ld_flags")"
      ;;
    lto)     pgo_flags="--with-lto" ;;
    lto_pgo) pgo_flags="--with-lto --enable-optimizations" ;;
  esac

  log "编译 Python (${OPTIMIZE})..."
  log_eta_reference python "${OPTIMIZE}"

  local clean_cmd="git clean -dfx 2>/dev/null || true; "
  if [[ "${REUSE_BUILD}" == true ]]; then
    log "⚠️  --reuse-build: 跳过清理，增量续编（请确认构建配置未变更）"
    clean_cmd=""
  fi

  assert_bisheng_active
  audit_build_log
  run_in_target "${TGT_ENV} export CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} && \
cd ${source_dir} && ${clean_cmd}\
./configure --prefix=${install_dir} --enable-shared ${pgo_flags} && \
make -j${JOBS} && make install"

  # --enable-shared 不自动设置 rpath，隔离前缀 python 会加载系统 libpython（playbook「rpath 坑」）
  if [[ "${DRY_RUN}" != true ]]; then
    if run_in_target "command -v patchelf >/dev/null 2>&1"; then
      local pybin
      pybin=$(run_in_target "ls ${install_dir}/bin/python3.* 2>/dev/null | grep -v -E '\\.orig|\\.config' | head -1" || true)
      if [[ -n "${pybin}" ]]; then
        log "修复 rpath: ${pybin}"
        run_in_target "cp ${pybin} ${pybin}.orig && patchelf --set-rpath '\$ORIGIN/../lib' ${pybin}"
      fi
    else
      log "⚠️  缺少 patchelf，无法自动修复 rpath；使用该 Python 前需设置 LD_LIBRARY_PATH=${install_dir}/lib（dnf install patchelf 后重跑可自动修复）"
    fi

    # 逃逸防线2/3：逐文件审计 + 构建日志指纹
    audit_build_log
    handle_escaped_artifacts "$(audit_compiler_marks "${install_dir}/bin/python3.* ${install_dir}/lib/libpython3.*.so*")" true
    log "Python 编译产物逐文件毕昇复核通过"
  fi

  if [[ "$OPTIMIZE" == css_pgo && "$STAGE" == gen && "$DRY_RUN" != true ]]; then
    target_exec bash -c '
      for lib in "$1"/lib/libpython*.so*; do
        [[ -f "$lib" ]] && readelf -SW "$lib" | grep -E "[.]pseudo_probe([[:space:]]|$)" >/dev/null && exit 0
      done
      exit 1
    ' bash "$install_dir" || fail "CSS gen 产物缺少 .pseudo_probe 段"
  fi
  log "Python 编译安装完成: ${install_dir}"
}

compile_pytorch() {
  local source_dir="$1"
  local stage="$2"
  local profile_dir="$3"

  # 前置依赖门：PyTorch 必须用毕昇版 Python 构建（playbook「编译顺序」）
  if [[ "${DRY_RUN}" != true && "${AK_ALLOW_NON_BISENG_PYTHON:-0}" != "1" ]]; then
    local py_abs py_marks
    py_abs=$(run_in_target "${TGT_ENV} command -v ${PYTHON_EXEC} 2>/dev/null" || true)
    [[ -n "${py_abs}" ]] || py_abs="${PYTHON_EXEC}"
    py_marks=$(count_bisheng_marks "${py_abs}")
    if [[ "${py_marks}" -eq 0 ]]; then
      fail_json "python_not_bisheng" \
        "\"python\":\"${py_abs}\"" \
        "\"recovery_steps\":[\"先用 --component python 编译安装毕昇版 Python\",\"编译 PyTorch 时传 --python-executable <毕昇版Python路径>\",\"确要继续（不推荐）: AK_ALLOW_NON_BISENG_PYTHON=1\"]"
    fi
    log "构建 Python 毕昇标识复核通过"
  fi

  ensure_ninja_cmake
  pin_build_tools
  install_pytorch_requirements "${source_dir}"
  patch_cast_function_type "${source_dir}"
  precheck_reuse_config_drift "${source_dir}"

  local extra_flags=""
  case "${stage}" in
    compile)     extra_flags="-flto=thin -fuse-ld=lld" ;;
    profile_gen) extra_flags="-flto=thin -fuse-ld=lld -fprofile-generate=${profile_dir}" ;;
    profile_use) extra_flags="-flto=thin -fuse-ld=lld -fprofile-use=${profile_dir}/default.profdata" ;;
    gen)
      # css_pgo gen: 伪探针 + 完整 -g（共享库 -gline-tables-only 不够，llvm-profgen 需完整 DWARF）
      extra_flags="-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g $(mcpu_flag)"
      ;;
    use)
      # css_pgo use: 去 -g；必须保留 -fpseudo-probe-for-profiling（SampleProfileProbePass 绑定，否则 profile 被忽略）
      local _css_profile_file
      _css_profile_file="$(css_profile_path)"
      extra_flags="-flto=thin -fuse-ld=lld -fno-omit-frame-pointer -fpseudo-probe-for-profiling -fprofile-sample-use=${_css_profile_file} $(mcpu_flag)"
      ;;
  esac
  # 架构选项注入（可选）: AK_ARCH_FLAGS="-mcpu=native" / "-march=armv9-a+sve2+bf16" 等。
  # 语义: 同时进入编译与链接（ThinLTO 后端按此定 target）。产物绑定构建机 ISA——
  # 跨型号分发需按 kunpeng 微架构文档复核特性矩阵（0xd01 无 SVE，0xd03/0xd06 有）
  if [[ -n "${AK_ARCH_FLAGS:-}" ]]; then
    extra_flags="${extra_flags} ${AK_ARCH_FLAGS}"
    log "架构选项注入: ${AK_ARCH_FLAGS}（产物绑定构建机 ISA，跨型号分发需复核）"
  fi
  # 逃生舱：AK_PROFILE_GEN_NO_LTO=1 时 profile_gen 跳过 LTO（仅插桩）。
  # 用途：特定版本 bitcode 触发链接期 lld 崩溃（实测 pytorch v2.6.0 +
  # 毕昇 5.2.0 ld.lld InstCombine 段错误，--lto-O0 不可绕）时保住验证链路；
  # 插桩采集不依赖 LTO，profile_use 阶段可换工具链恢复完整 LTO。
  if [[ "${stage}" == "profile_gen" && "${AK_PROFILE_GEN_NO_LTO:-0}" == "1" ]]; then
    extra_flags="-fuse-ld=lld -fprofile-generate=${profile_dir}"
    log "⚠️  AK_PROFILE_GEN_NO_LTO=1：profile_gen 跳过 LTO（仅插桩；链接器崩溃逃生舱）"
  fi

  log "编译 PyTorch (${stage})..."
  log_eta_reference pytorch "${stage}"

  # 供 torch_npu 阶段的内化符号修复回路定位 PyTorch 源码（同会话内已知）
  PYTORCH_BUILD_SRC="${SOURCE_DIR}"

  local clean_cmd="git clean -dfx && "
  if [[ "${REUSE_BUILD}" == true ]]; then
    log "⚠️  --reuse-build: 跳过清理，增量续编（请确认构建配置未变更；修复性重链依赖既有 build 目录）"
    clean_cmd=""
  fi

  assert_bisheng_active
  # USE_TENSORPIPE：上游默认开启，rpc 依赖它。clang19 对 libnop variant.h 的
  # template 关键字新诊断（missing-template-arg-list-after-template-kw）会把
  # TP 编译打断——实测 BiSheng 5.2 (clang 19.1.7) 用 -Wno-error 压制后 TP 完整
  # 编译通过且 rpc available=True；环境变量 AK_USE_TENSORPIPE=0 可强制关闭
  local tp_flag="${AK_USE_TENSORPIPE:-1}"
  # 全链 ABI 基线：一致性为不变量，值本身可为 0/1；默认 1（官方生态默认），
  # AK_GLIBCXX_USE_CXX11_ABI 可覆盖（用于适配整体 ABI=0 的既有生产栈）
  CHAIN_ABI="${AK_GLIBCXX_USE_CXX11_ABI:-1}"
  log "全链 ABI 基线: ${CHAIN_ABI}（PyTorch/torch_npu/vLLM-Ascend 须一致，值不强制）"
  local tp_suppress=""
  if [[ "${tp_flag}" == "1" ]]; then
    tp_suppress="-Wno-error=missing-template-arg-list-after-template-kw"
  fi

  run_in_target "${TGT_ENV} export CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} MAX_JOBS=${JOBS} CMAKE_POLICY_VERSION_MINIMUM=3.5 \
GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI} _GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI} \
USE_XNNPACK=0 USE_NNPACK=0 USE_PYTORCH_QNNPACK=0 USE_TENSORPIPE=${tp_flag} USE_KINETO=0 BUILD_CUSTOM_PROTOBUF=OFF \
CMAKE_C_FLAGS='${extra_flags}' CMAKE_CXX_FLAGS='${extra_flags} ${tp_suppress}' \
LDFLAGS='-O3 -fuse-ld=lld ${AK_ARCH_FLAGS:-} $(thinlto_cache_flags)${AK_EXTRA_LDFLAGS:-}' && \
cd ${source_dir} && ${clean_cmd}${PYTHON_EXEC} setup.py bdist_wheel" || PYTORCH_BUILD_FAILED=true
  if [[ "${PYTORCH_BUILD_FAILED:-false}" == true ]]; then
    if retry_build_on_rosegment_overlap; then
      log "使用 --no-rosegment 重试构建..."
      run_in_target "${TGT_ENV} export CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} MAX_JOBS=${JOBS} CMAKE_POLICY_VERSION_MINIMUM=3.5 \
GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI} _GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI} \
USE_XNNPACK=0 USE_NNPACK=0 USE_PYTORCH_QNNPACK=0 USE_TENSORPIPE=${tp_flag} USE_KINETO=0 BUILD_CUSTOM_PROTOBUF=OFF \
CMAKE_C_FLAGS='${extra_flags}' CMAKE_CXX_FLAGS='${extra_flags} ${tp_suppress}' \
LDFLAGS='-O3 -fuse-ld=lld ${AK_ARCH_FLAGS:-} $(thinlto_cache_flags)${AK_EXTRA_LDFLAGS:-}' && \
cd ${source_dir} && ${PYTHON_EXEC} setup.py bdist_wheel" || PYTORCH_BUILD_FAILED=true
    fi
    [[ "${PYTORCH_BUILD_FAILED:-false}" == true ]] && fail_build_with_diagnosis "pytorch"
  fi
  unset PYTORCH_BUILD_FAILED

  local whl="<dry-run-whl>"
  if [[ "${DRY_RUN}" != true ]]; then
    whl=$(latest_whl "${source_dir}/dist" "torch-*.whl")
  fi
  [[ -n "${whl}" ]] || fail_json "whl_not_found" \
    "\"source_dir\":\"${source_dir}\"" \
    "\"recovery_steps\":[\"查看构建日志定位编译错误（常见: 缺 Python.h→装 python3-devel；缺 bdist_wheel→脚本已钉 setuptools；cast-function-type→脚本已屏蔽）\",\"修复后重新执行本命令\"]"
  log "编译成功: ${whl}"

  gate_whl_bisheng "${whl}" "torch/_C*.so"
  install_whl "${whl}"
  repair_torch_runtime_libs "${source_dir}"

  # 链接缺陷检测与自修复（new_qtensor 内部化 + gfortran 未解析符号）
  repair_link_defects "${source_dir}"

  # CANN dlsym 导出面审计（CANN 在位时：候选名 ∩ local 符号 = 内化即修）
  audit_dlsym_export_surface

  # 产物形态门禁（PGO1 必须插桩 / LTO 必须非插桩）
  local torch_dir_post
  torch_dir_post=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch\"))' 2>/dev/null" || true)
  [[ -n "${torch_dir_post}" ]] && check_artifact_instrumentation "${torch_dir_post}/lib/libtorch_cpu.so"

  # 可迁移性审计（问题17：依赖捆绑 + GCC wheel 残留检测 + 迁移清单）
  [[ -n "${torch_dir_post}" ]] && audit_portability "${torch_dir_post}"

  # 逃逸防线2/3：逐文件审计 + 构建日志指纹（仅审计本脚本产出的 so）
  local torch_dir
  torch_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch\"))' 2>/dev/null" || true)
  [[ -n "${torch_dir}" ]] && handle_escaped_artifacts "$(audit_compiler_marks "${torch_dir}/lib/*.so")" true
  log "PyTorch 安装完成"
}

# BUILD_CUSTOM_PROTOBUF=OFF 且系统 protobuf soname 不匹配时（如系统 25.x vs 需要
# 3.13），cmake 会编译自带 protobuf 到 build/lib 与 torch/lib64，但 whl 安装后
# torch/lib 的 RPATH 覆盖不到 → import 报 libprotobuf.so.3.13.0.0 not found。
# 安装后扫描 "not found" 依赖并从构建目录补齐到 torch/lib
repair_torch_runtime_libs() {
  local source_dir="$1"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 扫描并修复 torch 运行库缺失依赖"
    return 0
  fi
  local torch_dir missing libname src f
  torch_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch\"))' 2>/dev/null" || true)
  [[ -n "${torch_dir}" ]] || return 0

  # 扫描须前置 TGT_ENV（PATH+LD_LIBRARY_PATH 含毕昇 lib）：编译器运行时
  # （libomp.so 等）按运行时契约由 ${BISHENG_PREFIX}/lib 解析（profile.d），
  # 裸 ldd 会把它们误报为缺失（实测 libomp 误报即此根因）
  missing=$(run_in_target "${TGT_ENV} for f in ${torch_dir}/lib/*.so ${torch_dir}/lib/*.so.*; do ldd \"\$f\" 2>/dev/null; done | grep 'not found' | awk '{print \$1}' | sort -u" || true)
  [[ -z "${missing}" ]] && { log "运行库依赖检查: 全部可解析"; return 0; }

  log "发现缺失运行库: $(printf '%s ' ${missing})"
  for libname in ${missing}; do
    # 搜索序：源码树（构建期产物，ABI 精确匹配）→ 毕昇前缀 lib（编译器
    # 运行时组件如 libomp.so——LTO 产物依赖它，whl 不随包分发，安装
    # warning 已提示"运行时将需要它"，此处自动补齐）
    src=$(run_in_target "ls ${source_dir}/build/lib/${libname} ${source_dir}/torch/lib64/${libname} ${source_dir}/torch/lib/${libname} ${BISHENG_PREFIX}/lib/${libname} 2>/dev/null | head -1" || true)
    if [[ -n "${src}" ]]; then
      log "补齐 ${libname} ← ${src}"
      # cp -aL 解引用符号链接（实测踩坑: 毕昇 libomp.so 是相对符号链接，
      # cp -a 按链接拷贝会产出断链——pip 重装只清 RECORD 内文件，断链会
      # 残留并毒化后续可迁移性审计的符号链接完整性门禁）
      run_in_target "rm -f '${torch_dir}/lib/${libname}'; cp -aL ${src} ${torch_dir}/lib/" || return 1
    else
      fail_json "runtime_lib_unresolvable" \
        "\"missing_lib\":\"${libname}\"" \
        "\"recovery_steps\":[\"确认系统是否缺少对应运行库（如 CANN 环境需先 source set_env.sh）\",\"或安装系统同名库后重试\"]"
    fi
  done
  missing=$(run_in_target "${TGT_ENV} for f in ${torch_dir}/lib/*.so; do ldd \"\$f\" 2>/dev/null; done | grep 'not found' | awk '{print \$1}' | sort -u" || true)
  [[ -z "${missing}" ]] || fail_json "runtime_lib_unresolvable" "\"missing_libs\":\"${missing}\""
  log "运行库依赖修复完成"
}

compile_torch_npu() {
  local source_dir="$1"
  local stage="$2"
  local profile_dir="$3"

  # ABI 前置门：PyTorch 必须是毕昇编译版（playbook「编译顺序」前置验证清单）
  if [[ "${DRY_RUN}" != true ]]; then
    local torch_dir
    torch_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch\"))' 2>/dev/null" || true)
    if [[ -n "${torch_dir}" ]]; then
      local bisheng_check
      bisheng_check=$(count_bisheng_marks "${torch_dir}/_C*.so")
      if [[ "${bisheng_check}" -eq 0 ]]; then
        if [[ "${AK_ALLOW_NON_BISENG_PYTHON:-0}" == "1" ]]; then
          log "⚠️  当前 PyTorch 不是毕昇编译版（已按 AK_ALLOW_NON_BISENG_PYTHON=1 继续），ABI 可能不兼容导致 torch_npu 运行时崩溃"
        else
          fail_json "pytorch_not_bisheng" \
            "\"torch_dir\":\"${torch_dir}\"" \
            "\"recovery_steps\":[\"先用毕昇编译器重编译 PyTorch: ak_compile_optimize.sh --component pytorch ...\",\"编译 torch_npu 前必须装好毕昇版 PyTorch\",\"确要继续（不推荐）: AK_ALLOW_NON_BISENG_PYTHON=1\"]"
        fi
      else
        log "PyTorch 毕昇编译版确认通过（${bisheng_check} 处标记）"
      fi
      # ABI 全链一致性：读取 PyTorch 实际编译 ABI 作为全链基线。
      # 不变量是「全链一致」而非具体值——生产栈可能整体运行旧 ABI(0)，
      # 此时 torch_npu 应按同值(0)构建；强制 =1 反而制造新的错配。
      # 值来源：torch._C._GLIBCXX_USE_CXX11_ABI（Python bool，int 归一化）
      local torch_abi
      # TORCH_DEVICE_BACKEND_AUTOLOAD=0：torch_npu 装入后 import torch 会自动
      # 加载 NPU 后端，无驱动环境（无 stub 路径）时连 torch 都 import 失败；
      # 读 PyTorch 自身 ABI 无需后端，显式关闭自动加载
      torch_abi=$(run_in_target "${TGT_ENV} TORCH_DEVICE_BACKEND_AUTOLOAD=0 ${PYTHON_EXEC} -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))' 2>/dev/null" || true)
      case "${torch_abi}" in
        1) log "PyTorch ABI=1（官方生态默认），torch_npu 将按同值构建" ;;
        0) log "⚠️  PyTorch ABI=0（旧 ABI）：torch_npu 将按同值(0)构建保持全链一致；如需切回 ABI=1 须先重编 PyTorch" ;;
        *) fail_json "torch_abi_unknown" \
             "\"python\":\"${PYTHON_EXEC}\"" \
             "\"recovery_steps\":[\"无法读取 PyTorch 编译 ABI（torch._C._GLIBCXX_USE_CXX11_ABI），确认 PyTorch 可 import 且版本支持\"]" ;;
      esac
      CHAIN_ABI_NPU="${torch_abi}"
    else
      log "⚠️  无法定位 torch 包目录，跳过 ABI 前置检查"
    fi
  fi
  CHAIN_ABI_NPU="${CHAIN_ABI_NPU:-${AK_GLIBCXX_USE_CXX11_ABI:-1}}"

  local pgo_flag="" css_cxxflags=""
  case "${stage}" in
    compile)     pgo_flag="--enable_lto" ;;
    profile_gen) pgo_flag="--enable_lto --enable_pgo=1" ;;
    profile_use) pgo_flag="--enable_lto --enable_pgo=2" ;;
    gen)
      # css_pgo gen: 伪探针 + 完整 DWARF（经 CXXFLAGS 注入，不走 build.sh PGO 旗标）
      css_cxxflags="-O3 -fno-omit-frame-pointer -fpseudo-probe-for-profiling -g $(mcpu_flag)"
      pgo_flag="--enable_lto"
      ;;
    use)
      # css_pgo use: 保留伪探针，注入 sample profile
      css_cxxflags="-O3 -fno-omit-frame-pointer -fpseudo-probe-for-profiling -fprofile-sample-use=$(css_profile_path) $(mcpu_flag)"
      pgo_flag="--enable_lto"
      ;;
  esac

  # 前置硬门禁1: CANN（torch_npu 编译必需，缺失时 build.sh 内部才会模糊失败）
  if ! run_in_target "[ -d /usr/local/Ascend/ascend-toolkit/latest ] || [ -n \"\${ASCEND_HOME_PATH:-}\" ]" >/dev/null 2>&1; then
    fail_json "cann_not_found" \
      "\"component\":\"torch_npu\"" \
      "\"recovery_steps\":[\"安装 CANN toolkit（同 torch_npu 版本对匹配的 CANN 版本）并 source set_env.sh\",\"安装后确认 /usr/local/Ascend/ascend-toolkit/latest 存在或 export ASCEND_HOME_PATH=<路径>\",\"x86_64/aarch64 安装包见 hiascend.com 开发者专区\",\"或使用 --source-dir 指定已在 CANN 环境准备好的源码路径\"]"
  fi

  # 前置硬门禁2: ci/build.sh 参数能力探测（实测 v2.1.0-6.0.rc1 的 build.sh 仅支持
  # --disable_torchair/--disable_rpc，不识别 --enable_lto/--enable_pgo，直接传参
  # 报 "Unsupported parameters" 中途失败；支持该参数的版本才走 build.sh 旗标路径）
  if [[ -n "${pgo_flag}" ]] && ! run_in_target "grep -q -- '--enable_lto' ${source_dir}/ci/build.sh 2>/dev/null"; then
    fail_json "torch_npu_buildsh_no_pgo_flag" \
      "\"source_dir\":\"${source_dir}\"" \
      "\"recovery_steps\":[\"该版本 ci/build.sh 不支持 --enable_lto/--enable_pgo 旗标（实测 v2.1.0-6.0.rc1 仅支持 --disable_torchair/--disable_rpc）\",\"升级 torch_npu 到支持 PGO 旗标的版本，或按 playbook 手动经 CFLAGS/CXXFLAGS/LDFLAGS 注入 -fprofile-instr-generate（未自动化，需实测）\",\"PyTorch 侧 PGO1 不受影响（经 CMake 直接注入）\"]"
  fi

  log "编译 torch_npu (${stage})..."
  log_eta_reference torch_npu "${stage}"

  local clean_cmd="git clean -dfx && "
  if [[ "${REUSE_BUILD}" == true ]]; then
    log "⚠️  --reuse-build: 跳过清理，增量续编（请确认构建配置未变更）"
    clean_cmd=""
  fi

  assert_bisheng_active

  # torchair 保障：子模块为空会被 setup.py 静默跳过（BUILD_TORCHAIR 不生效）
  verify_torchair_build "${source_dir}"

  # 预创建包目录：全新构建时 build_py 先于 BdistWheelBuild 需要
  # build/packages/* 已存在（torchnpu-wheel-packaging-playbook「增量 vs 全新构建」）
  # ci/build.sh 内部用 `python${PY_VERSION}` 从 PATH 取解释器（不认 --python-executable），
  # 必须把毕昇 python 前置 PATH，否则系统 python3.11 的 setuptools 环境会污染构建
  local py_bindir
  py_bindir=$(dirname "${PYTHON_EXEC}")
  run_in_target "${TGT_ENV} export PATH=${py_bindir}:\$PATH CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} MAX_JOBS=${JOBS} CMAKE_POLICY_VERSION_MINIMUM=3.5 \
GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI_NPU} _GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI_NPU} \
LDFLAGS='-O3 -fuse-ld=lld ${AK_ARCH_FLAGS:-} ${AK_EXTRA_LDFLAGS:-}' CXXFLAGS='${css_cxxflags:+${css_cxxflags} }-D_GNU_SOURCE -D_GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI_NPU} -Wno-missing-template-arg-list-after-template-kw ${AK_EXTRA_CXXFLAGS:-}' && \
cd ${source_dir} && ${clean_cmd}\
mkdir -p build/packages/torch_npu build/packages/torchnpugen && \
bash ci/build.sh --python=${PYTHON_VERSION} ${pgo_flag}" || NPU_BUILD_FAILED=true
  if [[ "${NPU_BUILD_FAILED:-false}" == true ]]; then
    if retry_build_on_rosegment_overlap; then
      log "使用 --no-rosegment 重试构建..."
      run_in_target "${TGT_ENV} export PATH=${py_bindir}:\$PATH CC=${BISHENG_CLANG} CXX=${BISHENG_CXX} MAX_JOBS=${JOBS} CMAKE_POLICY_VERSION_MINIMUM=3.5 \
GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI_NPU} _GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI_NPU} \
LDFLAGS='-O3 -fuse-ld=lld ${AK_ARCH_FLAGS:-} ${AK_EXTRA_LDFLAGS:-}' CXXFLAGS='${css_cxxflags:+${css_cxxflags} }-D_GNU_SOURCE -D_GLIBCXX_USE_CXX11_ABI=${CHAIN_ABI_NPU} -Wno-missing-template-arg-list-after-template-kw ${AK_EXTRA_CXXFLAGS:-}' && \
cd ${source_dir} && mkdir -p build/packages/torch_npu build/packages/torchnpugen && \
bash ci/build.sh --python=${PYTHON_VERSION} ${pgo_flag}" || NPU_BUILD_FAILED=true
    fi
    [[ "${NPU_BUILD_FAILED:-false}" == true ]] && fail_build_with_diagnosis "torch_npu"
  fi
  unset NPU_BUILD_FAILED

  local whl="<dry-run-whl>"
  if [[ "${DRY_RUN}" != true ]]; then
    whl=$(latest_whl "${source_dir}/dist" "torch_npu-*.whl")
  fi
  [[ -n "${whl}" ]] || fail_json "whl_not_found" \
    "\"source_dir\":\"${source_dir}\"" \
    "\"recovery_steps\":[\"检查构建日志: 无 [n/m] Building 记录说明 build_clib/build_ext 被跳过（见 torchnpu-wheel-packaging-playbook）\",\"确认 setuptools 在 70.1~84 区间（如 75.8.0）且 wheel==0.41.3（setuptools>=84 静默产出残缺 whl；68.2.2 实测缺 bdist_wheel 不可用）\",\"修复后重新执行本命令\"]"
  log "编译成功: ${whl}"

  gate_whl_bisheng "${whl}" "torch_npu/lib/*.so"
  install_whl "${whl}"

  # 逃逸防线2/3：逐文件审计 + 构建日志指纹
  local npu_dir
  npu_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch_npu\"))' 2>/dev/null" || true)
  [[ -n "${npu_dir}" ]] && handle_escaped_artifacts "$(audit_compiler_marks "${npu_dir}/lib/*.so")" true
  [[ -n "${npu_dir}" ]] && check_artifact_instrumentation "${npu_dir}/lib/libtorch_npu.so"

  # ABI 全链一致性后置校验：标记比对（PyTorch 定义侧 vs torch_npu 引用侧）。
  # 不变量是「一致」而非具体值——四组合实测矩阵（mock 编译）：
  #   cpu(1)+npu(1)→PASS, cpu(0)+npu(0)→PASS, 两个交叉→DETECTED
  # 标记提取失败时回退按 CHAIN_ABI_NPU 计数校验（V=1: Ss=0; V=0: NSt7__cxx11=0）
  if [[ -n "${npu_dir}" && "${DRY_RUN}" != true ]]; then
    local cpu_tok npu_tok npu_old_abi npu_new_abi
    cpu_tok=$(run_in_target "nm -D ${torch_dir}/lib/libtorch_cpu.so 2>/dev/null | grep '_ZN5torch7LibraryC1' | head -1 | grep -oE 'NSt7__cxx1112basic_string|Ss' | head -1" || true)
    npu_tok=$(run_in_target "nm -D ${npu_dir}/lib/libtorch_npu.so 2>/dev/null | grep ' U _ZN5torch7LibraryC1' | head -1 | grep -oE 'NSt7__cxx1112basic_string|Ss' | head -1" || true)
    if [[ -n "${cpu_tok}" && -n "${npu_tok}" ]]; then
      if [[ "${cpu_tok}" != "${npu_tok}" ]]; then
        if [[ "${AK_ALLOW_ABI_MISMATCH:-0}" == "1" ]]; then
          log "⚠️  ABI 标记不一致: PyTorch=${cpu_tok} vs torch_npu=${npu_tok}（已按 AK_ALLOW_ABI_MISMATCH=1 继续），运行时 import 将报 undefined symbol"
        else
          fail_json "torch_npu_abi_mismatch" \
            "\"pytorch_marker\":\"${cpu_tok}\"" \
            "\"torch_npu_marker\":\"${npu_tok}\"" \
            "\"recovery_steps\":[\"两侧 ABI 标记不一致（NSt7__cxx1112basic_string=新 ABI / Ss=旧 ABI）\",\"使 torch_npu 与 PyTorch 同值重建（脚本已自动按基线导出双变量）\",\"PyTorch ABI 由其构建时 GLIBCXX_USE_CXX11_ABI 决定，切换需先重编 PyTorch\",\"确要继续（不推荐）: AK_ALLOW_ABI_MISMATCH=1\"]"
        fi
      else
        log "torch_npu ABI 一致性校验通过（标记比对: ${cpu_tok} == ${npu_tok}）"
      fi
    else
      npu_old_abi=$(run_in_target "nm -D ${npu_dir}/lib/libtorch_npu.so 2>/dev/null | grep ' U _ZN5torch7LibraryC1.*Ss' | wc -l" || echo 0)
      npu_new_abi=$(run_in_target "nm -D ${npu_dir}/lib/libtorch_npu.so 2>/dev/null | grep -c NSt7__cxx1112basic_string" || echo 0)
      [[ "${npu_old_abi}" =~ ^[0-9]+$ ]] || npu_old_abi=0
      [[ "${npu_new_abi}" =~ ^[0-9]+$ ]] || npu_new_abi=0
      local abi_bad=false
      [[ "${CHAIN_ABI_NPU}" == "1" && "${npu_old_abi}" -gt 0 ]] && abi_bad=true
      [[ "${CHAIN_ABI_NPU}" == "0" && "${npu_new_abi}" -gt 0 ]] && abi_bad=true
      if [[ "${abi_bad}" == true ]]; then
        if [[ "${AK_ALLOW_ABI_MISMATCH:-0}" != "1" ]]; then
          fail_json "torch_npu_abi_mismatch" \
            "\"baseline_abi\":\"${CHAIN_ABI_NPU}\"" \
            "\"old_abi_symbols\":${npu_old_abi}" \
            "\"new_abi_symbols\":${npu_new_abi}" \
            "\"recovery_steps\":[\"torch_npu 产物与基线 ABI(${CHAIN_ABI_NPU}) 不符\",\"确认双变量导出: GLIBCXX_USE_CXX11_ABI（PyTorch 读）与 _GLIBCXX_USE_CXX11_ABI（torch_npu 读）\",\"去掉 --reuse-build 全量重建\",\"确要继续（不推荐）: AK_ALLOW_ABI_MISMATCH=1\"]"
        fi
      else
        log "torch_npu ABI 一致性校验通过（回退计数法: 基线=${CHAIN_ABI_NPU}）"
      fi
    fi
    # vLLM-Ascend 提示：三链之一，脚本不覆盖其重编译
    if run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import vllm' 2>/dev/null"; then
      log "⚠️  检测到 vLLM：vLLM-Ascend 同属 ABI 链（须与全链基线 ${CHAIN_ABI_NPU} 一致），请按 playbook「ABI 双栈共存」复核其 so"
    fi
  fi

  # CANN dlsym 导出面审计（torch_npu 安装后含 npu 产物）
  audit_dlsym_export_surface

  # torchair 验证：inductor NPU 扩展必须随 wheel 产出。
  # 注意 import 顺序：torchair 包由 torch_npu 挂载 sys.path，必须全链导入
  # （import torch, torch_npu, torchair），裸 import torchair 必挂（实测）。
  # 无 NPU 驱动的编译机上 import 会缺 libascend_hal.so（驱动层组件，toolkit
  # 只带 stub 桩库）——首次失败时把 toolkit stub 目录【追加到 LD_LIBRARY_PATH
  # 末尾】重试：真库优先解析，仅驱动库兜底到桩（stub 置前会遮蔽真库导致
  # 符号缺失，实测踩坑）。stub 通过 = 加载链验证，NPU 调用仍需真实驱动。
  local torchair_import_cmd="import torch, torch_npu, torchair"
  # CANN 环境前置：torch_npu 链接 libascendcl/libhccl 等，ldconfig 缓存不含
  # CANN 库（实测 0 处），必须 source set_env.sh 展开 LD_LIBRARY_PATH
  local cann_env=""
  if run_in_target "[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]" >/dev/null 2>&1; then
    cann_env="source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null;"
  fi
  if ! run_in_target "${TGT_ENV} ${cann_env} ${PYTHON_EXEC} -c '${torchair_import_cmd}'" 2>/dev/null; then
    local stub_dir
    stub_dir=$(run_in_target "ls -d /usr/local/Ascend/ascend-toolkit/latest/runtime/lib64/stub/linux/aarch64 2>/dev/null" | head -1)
    if [[ -n "${stub_dir}" ]] && run_in_target "${TGT_ENV} ${cann_env} LD_LIBRARY_PATH=\"\${LD_LIBRARY_PATH:+\$LD_LIBRARY_PATH:}${stub_dir}\" ${PYTHON_EXEC} -c '${torchair_import_cmd}'" 2>/dev/null; then
      log "torchair import 验证通过（无驱动环境经 stub 库完成加载链验证；NPU 调用需真实驱动）"
    else
      fail_json "torchair_import_failed" \
        "\"recovery_steps\":[\"检查源码树 third_party/torchair/torchair 是否非空（空则 setup.py 静默跳过 BUILD_TORCHAIR，脚本已在构建前自动补拉）\",\"确认 ci/build.sh 未带 --disable_torchair、DISABLE_INSTALL_TORCHAIR 未被设置\",\"检查构建日志中 BUILD_TORCHAIR 与 torchair 编译段\",\"torchair C++ 扩展构建依赖 ninja/编译器同源，确认 MAX_JOBS 与 CC/CXX 生效\",\"无驱动环境: 确认 toolkit runtime stub 目录存在（runtime/lib64/stub/linux/aarch64）\",\"修复后重跑（--reuse-build 可增量续编 torch_npu）\"]"
    fi
  else
    log "torchair import 验证通过（inductor NPU 扩展在位）"
  fi
  log "torch_npu 安装完成"
}

# ── 验证 ─────────────────────────────────────────────────────

verify_component() {
  local component="$1"
  log "验证 ${component}..."

  case "${component}" in
    python)
      local pybin
      pybin=$(run_in_target "ls ${INSTALL_DIR}/bin/python3.* 2>/dev/null | grep -v -E '\\.orig|\\.config' | head -1" || true)
      [[ -n "${pybin}" ]] || pybin="${INSTALL_DIR}/bin/python3"
      run_in_target "${pybin} --version" || fail "Python 版本验证失败"
      handle_escaped_artifacts "$(audit_compiler_marks "${INSTALL_DIR}/bin/python3.* ${INSTALL_DIR}/lib/libpython3.*.so*")" true
      run_in_target "${pybin} -c 'import ssl, hashlib, decimal, sqlite3, ctypes, lzma, bz2, zlib'" \
        || fail "Python 标准模块 smoke test 失败（检查编译依赖是否齐全）"
      log "逐文件毕昇复核通过 + 标准模块 smoke test 通过"
      ;;
    pytorch)
      run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch; print(torch.__version__)'" || fail "PyTorch import 失败"
      local torch_dir
      torch_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch\"))' 2>/dev/null" || true)
      if [[ -n "${torch_dir}" ]]; then
        handle_escaped_artifacts "$(audit_compiler_marks "${torch_dir}/lib/*.so")" true
      fi
      run_in_target "${TGT_ENV} ldd \$(command -v ${PYTHON_EXEC}) 2>/dev/null | grep -i omp" \
        || log "⚠️  未检测到 libomp.so 链接（运行时需 LD_LIBRARY_PATH 指向毕昇 lib）"
      run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch; a=torch.randn(4,4); print((a@a).shape)'" \
        || fail "PyTorch matmul smoke test 失败"
      log "逐文件毕昇复核通过 + matmul smoke test 通过"
      # TCPStore libuv 后端验证（问题16）：默认 use_libuv=True 依赖编译期
      # TORCH_USE_LIBUV 宏，而该宏经 USE_TENSORPIPE=1 链路启用（Dependencies.cmake:1158）；
      # TP=0 的非标构建会连带失去 libuv 后端 → TCPStore 崩溃
      if ! run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'from torch.distributed import TCPStore; s=TCPStore(\"127.0.0.1\",0,is_master=True,wait_for_workers=False,use_libuv=True); s.set(\"ak\",\"v\"); assert s.get(\"ak\")==b\"v\"'" 2>&1; then
        fail "TCPStore(use_libuv=True) 验证失败——构建缺 libuv 后端。根因链: TORCH_USE_LIBUV 依赖 USE_TENSORPIPE=1 链路(Dependencies.cmake:1158)，TP=0 会连带砍掉；须恢复 USE_TENSORPIPE=1（配合 -Wno-error=missing-template-arg-list-after-template-kw，见 playbook §注意事项）后重建 PyTorch"
      fi
      log "TCPStore use_libuv=True smoke test 通过（libuv 后端在位）"
      ;;
    torch_npu)
      local npu_imp_err
      if ! npu_imp_err=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu; print(torch_npu.__version__)'" 2>&1); then
        # undefined symbol 分诊：若缺失符号在 libtorch_cpu 中为 local(t)，
        # 即被 ThinLTO 内部化 → 走通用修复回路（PyTorch 源码可用时自动修复）
        local und_syms=() internalized=() s
        und_syms=($(printf '%s' "${npu_imp_err}" | grep -oE 'undefined symbol: [^ ,]+' | sed 's/undefined symbol: //' | sort -u || true))
        if [[ ${#und_syms[@]} -gt 0 && -n "${PYTORCH_BUILD_SRC:-}" && "${DRY_RUN}" != true ]]; then
          local cpu_lib
          cpu_lib=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch,os; print(os.path.dirname(torch.__file__)+\"/lib/libtorch_cpu.so\")' 2>/dev/null" || true)
          for s in "${und_syms[@]}"; do
            local t
            t=$(run_in_target "nm ${cpu_lib} 2>/dev/null | grep -E \" [tT] ${s}\$\" | head -1 | awk '{print \$2}'" || true)
            [[ "${t}" == "t" ]] && internalized+=("${s}")
          done
          if [[ ${#internalized[@]} -gt 0 ]]; then
            log "检出被内化的 torch 符号: ${internalized[*]} → 启动通用修复回路"
            if fix_internalized_symbols "${PYTORCH_BUILD_SRC}" "${internalized[@]}"; then
              npu_imp_err=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu; print(torch_npu.__version__)'" 2>&1) || true
            fi
          fi
        fi
        if printf '%s' "${npu_imp_err}" | grep -q 'undefined symbol'; then
          if printf '%s' "${npu_imp_err}" | grep -q 'Ss'; then
            fail "torch_npu import 失败（undefined symbol 含 Ss=旧 ABI 符号，疑似 ABI 错配；按 playbook「ABI 双栈共存」核查 GLIBCXX_USE_CXX11_ABI 双变量）: ${npu_imp_err}"
          fi
          fail "torch_npu import 失败（${#und_syms[@]} 个未定义符号；若为 torch 内化符号且 PyTorch 源码可用会自动修复，否则按符号名定位定义源文件去 inline 后重链 PyTorch——torch_npu 无需重编）: ${npu_imp_err}"
        fi
        fail "torch_npu import 失败: ${npu_imp_err}"
      fi
      local npu_dir
      npu_dir=$(run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import sysconfig,os; print(os.path.join(sysconfig.get_paths()[\"purelib\"], \"torch_npu\"))' 2>/dev/null" || true)
      if [[ -n "${npu_dir}" ]]; then
        handle_escaped_artifacts "$(audit_compiler_marks "${npu_dir}/lib/*.so")" true
      fi
      run_in_target "${TGT_ENV} ${PYTHON_EXEC} -c 'import torch_npu; print(\"npu:\", torch_npu.npu.is_available())'" \
        || log "⚠️  torch_npu.is_available() 失败（编译机可能无 NPU 设备，import 通过即可）"
      log "逐文件毕昇复核通过"
      ;;
  esac
  log "验证完成"
}

# ── Chain 编排模式（方向1 健壮性 / 方向2 耗时） ───────────────

# ccache 包装：重试/重建场景命中编译缓存（首次构建无收益，失败重试收益极大）
# ccache 包装器创建（chain 与单组件模式共用）。历史仅 chain 模式启用 ccache，
# 且 ccache 安装探测/包装脚本写入都在宿主机执行——--container 模式下装错侧、
# 包装器写在宿主而 CC 指向它（target 不可见）。本 helper 把安装探测与写入
# 统一走 run_in_target（容器内完成）。PGO 相关 sloppiness 必设：
# time_macros 让 __DATE__/__TIME__ 不参与缓存键（否则缓存恒 miss），
# profile 文件由 ccache 纳入哈希依赖，profile 变更自动失效
setup_ccache_wrappers() {
  local prefix="$1" wrapdir="$2"
  [[ "${AK_CCACHE}" == "1" ]] || { log "ccache 已禁用（AK_CCACHE=0）"; return 1; }
  [[ "${DRY_RUN}" == true ]] && return 1
  if ! run_in_target "command -v ccache >/dev/null 2>&1"; then
    run_in_target "dnf install -y -q ccache >/dev/null 2>&1" || true
  fi
  if ! run_in_target "command -v ccache >/dev/null 2>&1"; then
    log "⚠️  ccache 不可用且安装失败——失败重试将全量重编"
    return 1
  fi
  export CCACHE_SLOPPINESS="${CCACHE_SLOPPINESS:-pch_defines,time_macros}"
  export CCACHE_NOHASHDIR="${CCACHE_NOHASHDIR:-true}"
  # 编译器校验按内容而非 mtime：ninja 侧 PyTorch cmake 会自动把 PATH 中的 ccache
  # 设为 CMAKE_CXX_COMPILER_LAUNCHER（外层），其"编译器"实为本 wrapper 脚本——
  # 历史缺陷：wrapper 每次运行被重写 → mtime 变化 → 跨次运行缓存全失效
  # （实测 E1 全量重编命中率 0.7%，380s≈冷启动 359s）。content 校验 + 幂等写入
  # （内容不变不动 mtime）双保险
  export CCACHE_COMPILERCHECK="${CCACHE_COMPILERCHECK:-content}"
  # 幂等写入：先写 .tmp 再 cmp，内容相同则保留原文件（保 mtime 稳定）
  run_in_target "mkdir -p '${wrapdir}' && for w in clang clang++; do \
printf '#!/usr/bin/env bash\nexec ccache \"${prefix}/bin/'\$w'\" \"\$@\"\n' > '${wrapdir}/\$w.tmp' || exit 1; \
if [ ! -f '${wrapdir}/\$w' ] || ! cmp -s '${wrapdir}/\$w' '${wrapdir}/\$w.tmp'; then mv '${wrapdir}/\$w.tmp' '${wrapdir}/\$w'; chmod +x '${wrapdir}/\$w'; else rm -f '${wrapdir}/\$w.tmp'; fi; done"
  return 0
}

# 单组件模式 ccache 预备：先探测毕昇前缀，造包装器并导出 AK_CCACHE_WRAP_DIR；
# 随后 resolve_bisheng 的 ccache 分支会自动把 CC/CXX 切到包装器。缓存目录
# 跨运行持久（/tmp 重启后失效，可 AK_CCACHE_DIR 指到持久卷）
prepare_ccache_wrappers() {
  [[ "${AK_CCACHE}" == "1" ]] || { log "ccache 已禁用（AK_CCACHE=0）"; return 1; }
  [[ "${DRY_RUN}" == true ]] && return 1
  local prefix="${BISHENG_PREFIX_OPT}"
  if [[ -z "${prefix}" ]]; then
    local bpclang
    bpclang=$(run_in_target "source /etc/profile.d/bisheng.sh 2>/dev/null; command -v clang" | head -1 || true)
    [[ -n "${bpclang}" ]] && prefix="$(dirname "$(dirname "${bpclang}")")"
  fi
  [[ -n "${prefix}" ]] || { log "ccache: 未探测到毕昇前缀，跳过（resolve_bisheng 仍会全量探测）"; return 1; }
  export CCACHE_DIR="${AK_CCACHE_DIR:-/tmp/ak-ccache}"
  if ! setup_ccache_wrappers "${prefix}" "${CCACHE_DIR}/ccwrap"; then
    return 1
  fi
  export AK_CCACHE_WRAP_DIR="${CCACHE_DIR}/ccwrap"
  log "ccache 已启用（单组件模式）: ${CCACHE_DIR}（失败重试/阶段重跑命中缓存可大幅提速）"
  return 0
}

setup_chain_ccache() {
  export CCACHE_DIR="${AK_CCACHE_DIR:-${CHAIN_DIR}/ccache}"
  if ! setup_ccache_wrappers "${CHAIN_BISHENG_PREFIX}" "${CHAIN_DIR}/ccwrap"; then
    return 1
  fi
  export AK_CCACHE_WRAP_DIR="${CHAIN_DIR}/ccwrap"
  log "ccache 已启用: ${CCACHE_DIR}（编译缓存命中可大幅加速失败重试）"
}

chain_budget_log() {
  local now used
  now=$(date +%s)
  used=$(( (now - CHAIN_START) / 60 ))
  log "预算护栏: 已用 ${used}/${BUDGET_MINUTES} 分钟, 若超 4h 请评估降级选项（见 playbook 耗时纪律）"
}

run_chain_stage() {
  local desc="$1"; shift
  local now used
  now=$(date +%s)
  used=$(( (now - CHAIN_START) / 60 ))
  if (( used + 30 > BUDGET_MINUTES )); then
    log "⚠️  预算预警: 已用 ${used}/${BUDGET_MINUTES} 分钟，即将进入 ${desc}（预计 15-25 分钟）"
  fi
  log "═══ Chain 阶段: ${desc} ═══"
  # dry-run 必须透传子命令（历史缺陷：--chain --dry-run 会真实编译——
  # run_chain_stage 直接 bash 子调用且 DRY_RUN 未导出未追加）
  local child_args=()
  [[ "${DRY_RUN}" == true ]] && child_args+=(--dry-run)
  [[ -z "$CONTAINER" ]] || child_args+=(--container "$CONTAINER")
  bash "${CHAIN_SELF}" "$@" ${child_args[@]+"${child_args[@]}"} --log-file "${CHAIN_DIR}/stage_$(echo "${desc}" | tr ' /' '__').log"
  local rc=$?
  now=$(date +%s)
  used=$(( (now - CHAIN_START) / 60 ))
  if (( used > BUDGET_MINUTES )); then
    log "⚠️  预算超支: ${used}/${BUDGET_MINUTES} 分钟（可接受上限 4h=${BUDGET_MINUTES}x2，超支不中止仅告警）"
  fi
  return ${rc}
}

chain_state() {
  # $1=write $2=key $3=value | $1=read $2=key -> stdout value
  local sf="${CHAIN_DIR}/state.json"
  mkdir -p "${CHAIN_DIR}"
  if [[ "$1" == "write" ]]; then
    python3 - "$sf" "$2" "$3" << 'PYST'
import json, sys, os, tempfile
sf, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
st = json.load(open(sf)) if os.path.exists(sf) else {}
st[k] = v
with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(sf), delete=False) as tmp:
    json.dump(st, tmp, indent=1)
os.replace(tmp.name, sf)
PYST
  else
    python3 - "$sf" "$2" << 'PYST'
import json, sys, os
sf, k = sys.argv[1], sys.argv[2]
st = json.load(open(sf)) if os.path.exists(sf) else {}
sys.stdout.write(str(st.get(k, "")))
PYST
  fi
}

# dry-run 下不落盘 chain 状态（避免污染 --chain-resume 的断点状态：
# dry-run 阶段并未真实执行，标记 done 会让后续 resume 错误跳过该阶段）
chain_state_write() {
  [[ "${DRY_RUN}" == true ]] && return 0
  chain_state write "$1" "$2"
}

# 链解释器解析（替代历史硬编码 python3.11）：以 python 阶段实际安装到
# ${CHAIN_DIR}/python/bin 的解释器版本为准（版本不预设，系统 python3.9 时
# 硬编码 3.11 直接断链）。pytorch/torch_npu 阶段运行时惰性解析（此时
# python_build 已完成）。dry-run 用占位路径；python 不在链且无历史安装 →
# fail 并给出补救指引
resolve_chain_python() {
  if [[ -n "${CHAIN_PYTHON_EXE:-}" ]]; then
    return 0
  fi
  if [[ "${DRY_RUN}" == true ]]; then
    CHAIN_PYTHON_EXE="${CHAIN_DIR}/python/bin/python3"
    log "[dry-run] 链解释器占位: ${CHAIN_PYTHON_EXE}"
    return 0
  fi
  local pyver
  pyver=$(run_in_target "ls ${CHAIN_DIR}/python/bin/ 2>/dev/null | grep -E '^python3\.[0-9]+\$' | sort -V | tail -1" || true)
  if [[ -n "${pyver}" ]]; then
    CHAIN_PYTHON_EXE="${CHAIN_DIR}/python/bin/${pyver}"
    log "链解释器: ${CHAIN_PYTHON_EXE}"
    return 0
  fi
  fail "链目录未见已安装 Python: ${CHAIN_DIR}/python/bin/python3.<版本>（python 不在 --chain 且无历史安装；请把 python 加入 --chain，或先单独执行 python 阶段）"
}

chain_profile_usable() {
  [[ -n "$1" ]] || return 1
  target_exec bash -c '
    pd="$2/bin/llvm-profdata"
    [[ -x "$pd" ]] || pd=llvm-profdata
    for raw in "$1"/*.profraw; do
      [[ -s "$raw" ]] && "$pd" show "$raw" >/dev/null 2>&1 && exit 0
    done
    exit 1
  ' bash "$1" "$CHAIN_BISHENG_PREFIX"
}

collect_chain_profile() {
  local root="$1" attempt
  [[ -n "$CHAIN_WORKLOAD" ]] || { log "重新采集需要 --chain-workload"; return 1; }
  if [[ "$DRY_RUN" == true ]]; then
    PROFILE_DIR="$root/ak-chain.dry-run"
    log "[dry-run] 在独占目录 $PROFILE_DIR 采集: $CHAIN_WORKLOAD"
    return 0
  fi
  target_exec mkdir -p -- "$root" || return 1
  root=$(target_exec readlink -f -- "$root") || return 1
  attempt=$(target_exec mktemp -d "$root/ak-chain.XXXXXXXX") || return 1
  log "本次 profile 目录: $attempt"
  target_exec bash -c '
    export PATH="$1/python/bin:$PATH" OMP_PROC_BIND=false LLVM_PROFILE_FILE="$2/default_%m_%p.profraw"
    cd /tmp && exec bash -c "$3"
  ' bash "$CHAIN_DIR" "$attempt" "$CHAIN_WORKLOAD" || log "负载退出非零，检查已落盘的 profile"
  chain_profile_usable "$attempt" || { log "本次采集未生成可读取的 profraw: $attempt"; return 1; }
  PROFILE_DIR="$attempt"
  chain_state_write profile_run_dir "$PROFILE_DIR"
}

run_chain() {
  CHAIN_SELF=$(readlink -f "${BASH_SOURCE[0]}")
  mkdir -p "${CHAIN_DIR}"
  CHAIN_START=$(date +%s)

  if [[ "${CHAIN_RESUME}" == true ]]; then
    local done_py
    done_py=$(chain_state read chain_done)
    log "断点续跑: 已完成阶段 = ${done_py:-无}"
  fi

  # 毕昇前缀探测（ccache 包装需要）。
  # dry-run 用占位前缀（不依赖现场已装编译器，编排计划可预览）；
  # 探测统一走 run_in_target（历史用宿主 bash -c source profile.d——
  # --container 时读的是宿主机而非容器内的 profile.d）
  if [[ -n "${BISHENG_PREFIX_OPT}" ]]; then
    CHAIN_BISHENG_PREFIX="${BISHENG_PREFIX_OPT}"
  elif [[ "${DRY_RUN}" == true ]]; then
    CHAIN_BISHENG_PREFIX="/opt/bisheng"
    log "[dry-run] chain 毕昇前缀占位: ${CHAIN_BISHENG_PREFIX}"
  else
    local bpclang
    bpclang=$(run_in_target "source /etc/profile.d/bisheng.sh 2>/dev/null; command -v clang" | head -1 || true)
    [[ -n "${bpclang}" ]] || fail "chain 模式需要毕昇编译器（source profile.d 或 --bisheng-prefix）"
    run_in_target "${bpclang} --version 2>/dev/null" | grep -qi bisheng || fail "chain 模式 PATH 中的 clang 非毕昇"
    CHAIN_BISHENG_PREFIX="$(dirname "$(dirname "${bpclang}")")"
  fi
  log "chain 毕昇前缀: ${CHAIN_BISHENG_PREFIX}"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 跳过 ccache 设置（不执行 dnf/写包装脚本）"
  else
    setup_chain_ccache || log "继续（无 ccache，重试成本升高）"
  fi

  local IFS=','; read -ra comps <<< "${CHAIN}"; unset IFS
  local want_python=false want_pytorch=false want_npu=false
  for comp in "${comps[@]}"; do
    case "${comp}" in
      python) want_python=true ;;
      pytorch) want_pytorch=true ;;
      torch_npu) want_npu=true ;;
      *) fail "未知链组件: ${comp}" ;;
    esac
  done
  local need_workload=false
  { [[ "${want_pytorch}" == true ]] || [[ "${want_npu}" == true ]]; } && need_workload=true
  if [[ "${need_workload}" == true && -z "${CHAIN_WORKLOAD}" && "${CHAIN_RESUME}" != true ]]; then
    fail "chain 含 pytorch/torch_npu 时必须提供 --chain-workload <采集负载命令>"
  fi

  # 阶段序列（playbook 编译顺序）：python → pgo1 全链 → 统一采集 → pgo2 全链
  # （pytorch pgo2 不得早于 torch_npu pgo1：NPU 负载采集需 torch_npu 在位）
  local stages=()
  [[ "${want_python}" == true ]] && stages+=("python_build")
  [[ "${want_pytorch}" == true ]] && stages+=("pytorch_pgo1")
  [[ "${want_npu}" == true ]] && stages+=("torch_npu_pgo1")
  [[ "${need_workload}" == true ]] && stages+=("collect")
  [[ "${want_pytorch}" == true ]] && stages+=("pytorch_pgo2")
  [[ "${want_npu}" == true ]] && stages+=("torch_npu_pgo2")

  local profile_root="$PROFILE_DIR" saved_profile
  if [[ "$need_workload" == true && "$CHAIN_RESUME" != true ]]; then
    for st in "${stages[@]}"; do chain_state_write "${st}_done" pending; done
  fi
  for st in "${stages[@]}"; do
    if [[ "${CHAIN_RESUME}" == true ]]; then
      local st_done
      st_done=$(chain_state read "${st}_done")
      if [[ "$st" == collect && "$st_done" == done ]]; then
        saved_profile=$(chain_state read profile_run_dir)
        if chain_profile_usable "$saved_profile"; then
          PROFILE_DIR="$saved_profile"
        else
          st_done=failed
        fi
      fi
      [[ "${st_done}" == "done" ]] && { log "跳过 ${st}（已完成）"; continue; }
      [[ "${st_done}" == "failed" ]] && log "重试上次失败的 ${st}（--chain-resume）"
    fi
    case "${st}" in
      python_build)
        run_chain_stage "python_lto_pgo" \
          --component python --optimize lto_pgo --stage compile \
          --install-dir "${CHAIN_DIR}/python" --bisheng-prefix "${CHAIN_BISHENG_PREFIX}" --verify \
          || { chain_state_write "${st}_done" failed; fail "python 阶段失败（--chain-resume 可续跑）"; }
        ;;
      pytorch_pgo1|pytorch_pgo2)
        local pst="${st##*_}"
        local stage_arg="profile_gen"; [[ "${pst}" == "pgo2" ]] && stage_arg="profile_use"
        resolve_chain_python
        run_chain_stage "pytorch_${pst}" \
          --component pytorch --optimize lto_pgo --stage "${stage_arg}" \
          --python-executable "${CHAIN_PYTHON_EXE}" \
          --profile-dir "${PROFILE_DIR}" \
          --bisheng-prefix "${CHAIN_BISHENG_PREFIX}" --verify \
          || { chain_state_write "${st}_done" failed; fail "pytorch ${pst} 失败"; }
        ;;
      torch_npu_pgo1|torch_npu_pgo2)
        local nst="${st##*_}"
        # stage 与参数校验契约对齐（历史缺陷：传 --stage compile 被
        # "--optimize lto_pgo 仅支持 profile_gen|profile_use" 拒绝，
        # chain torch_npu 阶段必失败；nflag 在本层展开属死代码——
        # --enable_pgo=1/2 由 compile_torch_npu 按 stage 内部映射）
        local stage_arg="profile_gen"; [[ "${nst}" == "pgo2" ]] && stage_arg="profile_use"
        resolve_chain_python
        run_chain_stage "torch_npu_${nst}" \
          --component torch_npu --optimize lto_pgo --stage "${stage_arg}" \
          --python-executable "${CHAIN_PYTHON_EXE}" \
          --profile-dir "${PROFILE_DIR}" \
          --bisheng-prefix "${CHAIN_BISHENG_PREFIX}" --verify \
          || { chain_state_write "${st}_done" failed; fail "torch_npu ${nst} 失败"; }
        ;;
      collect)
        log "═══ Chain 阶段: profile 统一采集（真实负载，覆盖全组件） ═══"
        chain_state_write collect_done pending
        chain_state_write pytorch_pgo2_done pending
        chain_state_write torch_npu_pgo2_done pending
        collect_chain_profile "$profile_root" || {
          chain_state_write collect_done failed
          fail "采集失败；--chain-resume 将重试采集"
        }
        ;;
    esac
    chain_state_write "${st}_done" done
    chain_budget_log
  done
  local now=$(date +%s)
  log "═══ Chain 完成: 总耗时 $(( (now - CHAIN_START) / 60 )) 分钟（预算 ${BUDGET_MINUTES}）═══"
  log "迁移指引: 安装树与 ak_portability_manifest.json 已生成；B 容器校验见 references/ak-compiler-playbook.md「符号链接导致迁移后产物不可用」节（逐符号链接核对 + ldd -r + import）"
}

# ── 主流程 ───────────────────────────────────────────────────

# 可测性：source 本文件时仅加载全局变量与函数定义，不执行主流程（供单测
# harness 使用；参数解析与日志 exec 已由文件顶部的条件块跳过）
if [[ "${AK_TEST_SOURCE_ONLY:-0}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

# Chain 编排模式：单命令串行全链（含 ccache/预算/断点续跑），不进入单组件流程
if [[ -n "${CHAIN}" ]]; then
  run_chain
  exit $?
fi

START_TIME=$(date +%s)

# 一键安装模式：不需要 component/optimize/stage
if [[ -n "${INSTALL_BISHENG}" ]]; then
  log "=== A+K 编译优化：安装毕昇编译器 ==="
  build_tgt_env
  if [[ "${DRY_RUN}" != true && -n "${CONTAINER}" ]] && ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    fail "容器 ${CONTAINER} 不存在"
  fi
  install_bisheng "${INSTALL_BISHENG}"
  output_json "success" "\"bisheng_prefix\":\"${INSTALL_BISHENG}\""
  exit 0
fi

log "=== A+K 编译优化 (${COMPONENT:-env-check}) ==="

# 毕昇环境
build_tgt_env

# 环境指纹快照/复现校验：不硬性要求毕昇编译器在位——编译前基线快照
# 恰恰要在装编译器之前可用；检测不到时 bisheng_clang_version 记为空
if [[ -n "${ENV_SNAPSHOT_DIR}" || -n "${ENV_VERIFY_DIR}" ]]; then
  if [[ "${DRY_RUN}" != true ]]; then
    # 输出重定向：resolve 失败时不在 stdout 落第二个 failed JSON（避免与
    # 快照/校验结果 JSON 混淆），诊断走 stderr 与 --check-only
    resolve_bisheng >/dev/null 2>&1 || log "⚠️ 未检测到毕昇编译器，快照 bisheng_clang_version 将为空（诊断: --check-only）"
  fi
  if [[ -n "${ENV_SNAPSHOT_DIR}" ]]; then
    if [[ "${DRY_RUN}" == true ]]; then
      log "[dry-run] 环境指纹快照 → ${ENV_SNAPSHOT_DIR}/env_snapshot.json"
      exit 0
    fi
    record_env_snapshot "${ENV_SNAPSHOT_DIR}"
    output_json "success" "\"env_snapshot\":\"${ENV_SNAPSHOT_DIR}/env_snapshot.json\""
    exit 0
  fi
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] 环境复现校验 vs ${ENV_VERIFY_DIR}"
    exit 0
  fi
  verify_env_snapshot "${ENV_VERIFY_DIR}"
  exit $?
fi

if [[ "${DRY_RUN}" == true ]]; then
  BISHENG_CLANG="clang"
  BISHENG_CXX="clang++"
  BISHENG_PREFIX="${BISHENG_PREFIX_OPT:-/opt/bisheng}"
  log "dry-run 模式，跳过毕昇编译器检查（命令中按 clang 占位）"
else
  # 单组件模式 ccache 预备（历史仅 chain 模式启用）：resolve_bisheng 的
  # ccache 分支会自动把 CC/CXX 切到包装器；失败 best-effort 继续
  if [[ "${CHECK_ONLY}" != true ]]; then prepare_ccache_wrappers || true; fi
  resolve_bisheng || exit 1
fi

detect_python_version
check_container

# 环境准备阶段：PyPI 源探测选源 + 常用依赖体检（--check-only 同样执行）
select_pip_index
check_dependencies

if [[ "${CHECK_ONLY}" == true ]]; then
  log "=== 环境检查完成 ==="
  output_json "success" \
    "\"bisheng\":\"${BISHENG_CLANG}\"" \
    "\"bisheng_prefix\":\"${BISHENG_PREFIX}\"" \
    "\"python_version\":\"${PYTHON_VERSION}\"" \
    "\"pip_index\":\"${PIP_INDEX}\""
  exit 0
fi

# 参数校验
[[ -n "${COMPONENT}" ]] || { usage >&2; fail "--component is required"; }
[[ -n "${OPTIMIZE}" ]] || { usage >&2; fail "--optimize is required"; }
[[ -n "${STAGE}" ]] || { usage >&2; fail "--stage is required"; }

case "${COMPONENT}" in
  python|pytorch|torch_npu) ;;
  *) fail "未知组件: ${COMPONENT}（可选 python|pytorch|torch_npu）" ;;
esac

# stage 与 optimize 组合校验（旧实现 lto+profile_gen 会被静默当 compile 处理）
case "${OPTIMIZE}" in
  lto)
    [[ "${STAGE}" == "compile" ]] || fail "--optimize lto 仅支持 --stage compile"
    # torch_npu 纯 LTO 门禁：经验库实测负收益 -22.4%（无 PGO 引导的跨模块内联在
    # device launch 路径为负优化）。默认拦截；对照实验豁免: AK_ALLOW_TORCH_NPU_LTO_ONLY=1
    if [[ "${COMPONENT}" == "torch_npu" && "${AK_ALLOW_TORCH_NPU_LTO_ONLY:-0}" != "1" ]]; then
      fail_json "torch_npu_lto_only_rejected" \
        "\"component\":\"torch_npu\"" \
        "\"recovery_steps\":[\"torch_npu 纯 ThinLTO（无 PGO）实测负收益 -22.4%（经验库实测，qwen2.5-1.5b 案例 125.71→86.18 tok/s）\",\"改用 --optimize lto_pgo（--stage profile_gen → 采集 → profile_use）\",\"确需 LTO-only 做对照实验: AK_ALLOW_TORCH_NPU_LTO_ONLY=1 强过本门禁\"]"
    fi
    ;;
  lto_pgo)
    if [[ "${COMPONENT}" == "python" ]]; then
      # Python PGO 内置（--enable-optimizations 跑自带 benchmark），无需 profile_gen/use
      [[ "${STAGE}" == "compile" ]] || fail "python 组件 PGO 内置，仅支持 --stage compile"
    else
      [[ "${STAGE}" == "profile_gen" || "${STAGE}" == "profile_use" ]] || fail "--optimize lto_pgo 仅支持 --stage profile_gen | profile_use"
    fi
    ;;
  css_pgo)
    [[ "${STAGE}" == "gen" || "${STAGE}" == "collect" || "${STAGE}" == "use" ]] || fail "--optimize css_pgo 仅支持 --stage gen | collect | use"
    if [[ "${STAGE}" == "collect" ]]; then
      collect_css_profile
      exit $?
    fi
    if [[ "${STAGE}" == "use" ]]; then
      check_css_profile || exit 1
      CSS_PROFILE="$(css_profile_path)"
      if [[ ! "$CSS_PROFILE" =~ ^/[a-zA-Z0-9_./+-]+$ ]]; then
        if [[ "$DRY_RUN" == true ]]; then
          log "[dry-run] 为编译器准备 sample profile: $CSS_PROFILE"
          CSS_PROFILE=/tmp/ak-css-profile.dry-run
        else
          css_build_file=$(target_exec mktemp /tmp/ak-css-profile.XXXXXXXX) || exit 1
          target_exec cp -- "$CSS_PROFILE" "$css_build_file" || exit 1
          CSS_PROFILE=$css_build_file
        fi
      fi
    fi
    ;;
  *) fail "未知优化类型: ${OPTIMIZE}（可选 lto|lto_pgo|css_pgo）" ;;
esac

# 源码准备：不传 --source-dir 时自动下载到默认目录；
# 显式指定的路径不存在视为用户输入错误，直接报错（避免误启动 GB 级下载）
if [[ -z "${SOURCE_DIR}" ]]; then
  SOURCE_DIR="/tmp/ak-build/${COMPONENT}"
  log "未指定源码目录，使用默认: ${SOURCE_DIR}"
  if ! run_in_target "[ -d \"${SOURCE_DIR}\" ]"; then
    CURRENT_VERSION=$(detect_current_version "${COMPONENT}")
    log "当前版本: ${CURRENT_VERSION}"
    slow_net_gate "${COMPONENT}" "${CURRENT_VERSION}"
    download_source "${COMPONENT}" "${CURRENT_VERSION}" "${SOURCE_DIR}"
  fi
elif ! run_in_target "[ -d \"${SOURCE_DIR}\" ]"; then
  fail "源码目录不存在: ${SOURCE_DIR}（自动下载仅在不传 --source-dir 时进行）"
fi
check_source "${SOURCE_DIR}" "${COMPONENT}"

# 通用分版补丁扩展点（函数定义见上；此处执行）：
# AK_PATCH_SCRIPT 单补丁（兼容存量）；AK_PATCH_SCRIPTS 冒号分隔多补丁按序执行
# （torch_npu 2.4.0 需同时挂 missing-includes + abi-env——历史单钩子无法满足）
run_patch_scripts

# 磁盘空间检查
case "${COMPONENT}" in
  python)  check_disk_space 3 ;;
  pytorch) check_disk_space 12 ;;
  torch_npu) check_disk_space 8 ;;
esac

calc_jobs

# 编译执行
log "=== 编译执行: ${COMPONENT} ${OPTIMIZE} ${STAGE} ==="

case "${COMPONENT}" in
  python)
    [[ -n "${INSTALL_DIR}" ]] || fail "Python 编译需要 --install-dir"
    compile_python "${SOURCE_DIR}" "${INSTALL_DIR}"
    ;;
  pytorch)
    if [[ "${OPTIMIZE}" == "lto_pgo" && "${STAGE}" == "profile_use" ]]; then
      check_profile "${PROFILE_DIR}" || exit 1
      merge_profile "${PROFILE_DIR}" || exit 1
    fi
    compile_pytorch "${SOURCE_DIR}" "${STAGE}" "${PROFILE_DIR}"
    ;;
  torch_npu)
    if [[ "${OPTIMIZE}" == "lto_pgo" && "${STAGE}" == "profile_use" ]]; then
      check_profile "${PROFILE_DIR}" || exit 1
      merge_profile "${PROFILE_DIR}" || exit 1
    fi
    compile_torch_npu "${SOURCE_DIR}" "${STAGE}" "${PROFILE_DIR}"
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
if [[ "${OPTIMIZE}" == "css_pgo" && "${STAGE}" == "gen" ]]; then
  NEXT_STAGE="collect"
  NEXT_INSTRUCTION="安装 gen 产物并跑真实负载，执行 --stage collect --pid <pid> --binary <gen产物> 采样，再执行 --stage use"
fi
if [[ "${OPTIMIZE}" == "lto_pgo" && "${STAGE}" == "profile_gen" ]]; then
  NEXT_STAGE="profile_use"
  NEXT_INSTRUCTION="采集清单: 1)确认插桩版已安装(readelf -SW <torch>/lib/libtorch_cpu.so | grep -c __llvm_prf 应>0) 2)前台运行 workload 确认无秒退 3)export OMP_PROC_BIND=false LLVM_PROFILE_FILE=${PROFILE_DIR}/default_%m_%p.profraw 4)正常退出(多进程一次性 SIGINT,勿 SIGTERM/SIGKILL) 5)ls ${PROFILE_DIR}/*.profraw 非空；vLLM 多进程需 sitecustomize.py 注入(见 playbook)；完成后执行 --stage profile_use"
fi

log "=== 完成 (${DURATION}s) ==="
output_json "success" \
  "\"duration_seconds\":${DURATION}" \
  "\"next_stage\":\"${NEXT_STAGE}\"" \
  "\"next_stage_instruction\":\"${NEXT_INSTRUCTION}\""
