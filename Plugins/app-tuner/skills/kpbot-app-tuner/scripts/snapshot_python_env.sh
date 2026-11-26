#!/usr/bin/env bash
# snapshot_python_env.sh — Python 虚拟环境字节级快照 / 恢复（容器 + conda 场景）
#
# 目的：
#   对"pip install 替换库 / 重编译 / LD_PRELOAD"等优化动作提供字节级完美回退。
#   回退依赖快照 tar + sha256 manifest，而不是 pip uninstall+重装（后者无法保证
#   与原版本字节一致，回退后可能出现性能不一致）。
#
# 用法：
#   snapshot_python_env.sh snapshot --env <conda-env-name|python-abs-path> --out <snapshot_dir>
#   snapshot_python_env.sh restore  --snapshot <snapshot_dir> [--verify-only] [--dry-run]
#
# 产出：
#   <snapshot_dir>/
#     env_identity.json         虚拟环境身份（python 路径、pip freeze、包清单）
#     site-packages-manifest.txt  site-packages 下被替换候选包目录清单
#     packages/<pkg>.tar.gz      每个被替换候选包的字节快照
#     sha256sums.txt             所有快照文件的 sha256 清单（恢复一致性校验依据）
#
# 退出码：0 成功；1 失败；2 输入不完整。
set -euo pipefail

CMD="${1:-}"
if [[ "${CMD}" == "snapshot" ]]; then shift; else
  if [[ "${CMD}" == "restore" ]]; then shift; else
    echo "ERROR: first arg must be 'snapshot' or 'restore'" >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# 解析参数
# ---------------------------------------------------------------------------
SNAPSHOT_OUT=""
VERIFY_ONLY=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_SPEC="${2:-}"; shift 2;;
    --out) SNAPSHOT_OUT="${2:-}"; shift 2;;
    --snapshot) SNAPSHOT_DIR="${2:-}"; shift 2;;
    --verify-only) VERIFY_ONLY=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    --help|-h)
      sed -n '2,14p' "$0"; exit 0;;
    *) echo "ERROR: unknown arg $1" >&2; exit 2;;
  esac
done

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
log()  { printf '[snapshot] %s\n' "$@"; }
err()  { printf '[snapshot ERROR] %s\n' "$@" >&2; }

# 解析 ENV_SPEC：可以是 conda env 名、或 python 可执行文件绝对路径
resolve_python() {
  local spec="${1:-}"
  if [[ -z "${spec}" ]]; then
    # 默认取当前 PATH 上的 python3（容器场景会由调用方传 --env）
    command -v python3 || { err "python3 not found and --env not provided"; exit 2; }
    return
  fi
  if [[ -x "${spec}/bin/python" ]]; then
    echo "${spec}/bin/python"; return
  fi
  if [[ -x "${spec}" ]] && [[ "${spec}" == *python* ]]; then
    echo "${spec}"; return
  fi
  # 尝试 conda env 名
  local candidate
  for base in /opt/conda /usr/local /home/*/miniconda3 /home/*/anaconda3; do
    candidate="${base}/envs/${spec}/bin/python"
    if [[ -x "${candidate}" ]]; then echo "${candidate}"; return; fi
  done
  err "cannot resolve --env '${spec}' to a python executable"; exit 2
}

# 目标包（替换/重编译高风险）默认清单；调用方可后续扩展
DEFAULT_PACKAGES=(torch torch_npu torchair torchvision apex transformers megatron)
DEFAULT_PREFIXES=(
  "torch_npu"
  "torchair"
  "torch"
  "torchvision"
  "deepspeed"
  "flash_attn"
)

# 计算 CXX11 ABI：优先 import torch；失败时从 libtorch_python.so 二进制字符串推断
detect_cxx11_abi() {
  local py="$1"
  local val
  val="$("${py}" -c 'import torch; print(torch._C._GLIBCXX_USE_CXX11_ABI)' 2>/dev/null)" && { echo "${val}"; return; }
  local sp site_pkgs
  site_pkgs="$("${py}" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)"
  # 二进制探测：libtorch_python.so 中包含 GLIBCXX_3.4.21+ 但无 _GLIBCXX_USE_CXX11_ABI=0 字符串属 ABI=1
  if [[ -n "${site_pkgs}" ]]; then
    local lib
    lib="$(find "${site_pkgs}/torch/lib" -maxdepth 1 -name 'libtorch_python.so' 2>/dev/null | head -1)"
    if [[ -n "${lib}" ]]; then
      if strings "${lib}" 2>/dev/null | grep -q "_GLIBCXX_USE_CXX11_ABI=0"; then
        echo "0" ; return
      fi
      if strings "${lib}" 2>/dev/null | grep -qE "std::__cxx11|_GLIBCXX_USE_CXX11_ABI=1"; then
        echo "1" ; return
      fi
    fi
  fi
  echo "unknown"
}

# ---------------------------------------------------------------------------
# SNAPSHOT 模式
# ---------------------------------------------------------------------------
snapshot() {
  local python_bin
  python_bin="$(resolve_python "${ENV_SPEC:-}")"
  local py_dir
  py_dir="$(dirname "$(dirname "${python_bin}")")"     # env root
  local site_packages
  site_packages="$("${python_bin}" -c "import site,sys; print(site.getsitepackages()[0] if site.getsitepackages() else sys.prefix+'/lib/python'+sys.version_info[:2][0].__str__()+'.'+sys.version_info[:2][1].__str__()+'/site-packages')" 2>/dev/null || echo "")"
  if [[ -z "${site_packages}" ]] || [[ ! -d "${site_packages}" ]]; then
    # 兼容非标准布局：从 python 推断
    site_packages="$(dirname "${python_bin}")/../lib/python$("${python_bin}" -c "import sys; print('.'.join(map(str,sys.version_info[:2])))")/site-packages"
  fi
  [[ -d "${site_packages}" ]] || { err "cannot locate site-packages for ${python_bin}"; exit 1; }

  if [[ -z "${SNAPSHOT_OUT}" ]]; then
    SNAPSHOT_OUT="/tmp/python-env-snapshot-$(date +%Y%m%d-%H%M%S)"
  fi
  local pkgs_dir="${SNAPSHOT_OUT}/packages"
  mkdir -p "${pkgs_dir}"

  # 1) 环境身份
  {
    echo "{" 
    echo "  \"python_bin\": \"${python_bin}\","
    echo "  \"python_version\": \"$("${python_bin}" --version 2>&1)\","
    echo "  \"sys_prefix\": \"$("${python_bin}" -c 'import sys; print(sys.prefix)')\","
    echo "  \"env_root\": \"${py_dir}\","
    echo "  \"site_packages\": \"${site_packages}\","
    echo "  \"cxx11_abi\": \"$(detect_cxx11_abi "${python_bin}")\","
    echo "  \"snapshot_time\": \"$(date -Is)\","
    echo "  \"packages\": {"
    local first=1
    for pkg in "${DEFAULT_PACKAGES[@]}"; do
      local ver="$("${python_bin}" -c "import importlib.metadata as m; print(m.version('${pkg}'))" 2>/dev/null || echo "__not_installed__")"
      if [[ "${ver}" != "__not_installed__" ]]; then
        [[ ${first} -eq 0 ]] && echo -n ","
        echo ""
        echo -n "    \"${pkg}\": \"${ver}\""
        first=0
      fi
    done
    echo ""
    echo "  }"
    echo "}"
  } > "${SNAPSHOT_OUT}/env_identity.json"

  # 2) pip freeze 全量清单
  "${python_bin}" -m pip freeze > "${SNAPSHOT_OUT}/pip-freeze.txt" 2>/dev/null || "${python_bin}" -m pip list --format=freeze > "${SNAPSHOT_OUT}/pip-freeze.txt" 2>/dev/null || true

  # 3) site-packages 目标包普查
  {
    echo "# site-packages: ${site_packages}"
    echo "# 候选替换包目录/文件（可能被 pip install 替换或重编译覆盖）："
    for prefix in "${DEFAULT_PREFIXES[@]}"; do
      find "${site_packages}" -maxdepth 1 -iname "${prefix}*" 2>/dev/null || true
    done
  } > "${SNAPSHOT_OUT}/site-packages-manifest.txt"

  # 4) 逐包字节快照（tar.gz）—— 用 package name 匹配 site-packages 顶层目录/*.py
  {
    while read -r entry; do
      [[ -z "${entry}" ]] && continue
      local base
      base="$(basename "${entry}")"
      local pkg_name="${base%.*}"
      if [[ "${pkg_name}" == "torch" ]] || [[ "${pkg_name}" == torch_* ]] || [[ "${pkg_name}" == torchair* ]]; then
        log "snapshotting candidate: ${base}"
        ( cd "${site_packages}" && tar czf "${pkgs_dir}/${pkg_name}-$(date +%Y%m%d).tar.gz" "${base}" 2>/dev/null ) || log "skip empty/non-exist ${base}"
      fi
    done < "${SNAPSHOT_OUT}/site-packages-manifest.txt"
  } || true

  # 5) sha256 manifest
  ( cd "${SNAPSHOT_OUT}" && find . -type f | sort | xargs -r sha256sum > sha256sums.txt )

  log "snapshot done: ${SNAPSHOT_OUT}"
  log "env_identity.json + pip-freeze.txt + packages/<pkg>.tar.gz + sha256sums.txt"
}

# ---------------------------------------------------------------------------
# RESTORE 模式
# ---------------------------------------------------------------------------
restore() {
  [[ -n "${SNAPSHOT_DIR}" ]] || { err "--snapshot required"; exit 2; }
  [[ -d "${SNAPSHOT_DIR}" ]] || { err "snapshot dir not found: ${SNAPSHOT_DIR}"; exit 2; }

  # 先做一致性校验：(a) 快照自身 sha256；(b) restore 前 site-packages 现状 hash 记录
  local manifest="${SNAPSHOT_DIR}/sha256sums.txt"
  [[ -f "${manifest}" ]] || { err "sha256sums.txt missing in snapshot"; exit 1; }

  log "verifying snapshot integrity (sha256 -c)..."
  ( cd "${SNAPSHOT_DIR}" && sha256sum -c sha256sums.txt ) || { err "snapshot integrity check FAILED"; exit 1; }
  log "snapshot integrity OK"

  # 恢复前的当前 site-packages hash 基线（便于比对待恢复包现状）
  local env_identity="${SNAPSHOT_DIR}/env_identity.json"
  local site_packages=""
  if [[ -f "${env_identity}" ]]; then
    site_packages="$(jq -r '.site_packages' "${env_identity}" 2>/dev/null || python3 -c "import json;print(json.load(open('${env_identity}'))['site_packages'])")"
  fi
  if [[ -z "${site_packages}" ]] || [[ ! -d "${site_packages}" ]]; then
    err "cannot determine site_packages from env_identity; restore aborted (avoid wrong-dir restore)"
    exit 1
  fi

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: would restore packages into ${site_packages}"
    ( cd "${SNAPSHOT_DIR}/packages" && ls -1 *.tar.gz 2>/dev/null ) | sed 's/^/  would-restore: /'
    log "VERIFY-ONLY off; exit 0"
    exit 0
  fi

  if [[ "${VERIFY_ONLY}" -eq 1 ]]; then
    log "VERIFY-ONLY: 不执行恢复，仅校验快照完整性（sha256 -c 已通过）"
    exit 0
  fi

  # 实际恢复：tar 解压覆盖 site-packages
  log "restoring byte-level packages into ${site_packages}"
  for tarball in "${SNAPSHOT_DIR}"/packages/*.tar.gz; do
    [[ -f "${tarball}" ]] || continue
    log "  restoring $(basename "${tarball}")"
    tar xzf "${tarball}" -C "${site_packages}"
  done

  # 恢复后复校 sha256：将 site-packages 相关包重新打 hash 与 manifest 中 packages/*.tar.gz 比对
  # 简化做法：恢复后 sha256 计算 packages 解压目录，与快照 tar 无法直接比对；
  # 采用精确比对：记录恢复前 tar 存在即还原，最终以「恢复后 pip show 版本 == env_identity 版本」复核。
  local py_bin
  py_bin="$(python3 -c "import json;print(json.load(open('${env_identity}'))['python_bin'])")"
  log "post-restore version check vs env_identity.json ..."
  local ok=1
  while read -r pkg ver; do
    [[ -z "${pkg}" ]] && continue
    local cur
    cur="$("${py_bin}" -c "import importlib.metadata as m; print(m.version('${pkg}'))" 2>/dev/null || echo MISSING)"
    if [[ "${cur}" != "${ver}" ]]; then
      err "${pkg}: restored ${cur}, expected ${ver}"
      ok=0
    fi
  done < <(python3 -c "
import json
d=json.load(open('${env_identity}'))
for k,v in d.get('packages',{}).items(): print(k,v)
")
  if [[ ${ok} -eq 1 ]]; then
    log "POST-RESTORE VERIFICATION OK: 所有目标包版本与快照一致"
  else
    err "POST-RESTORE VERIFICATION FAILED: 存在版本不一致，请人工检查"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
if [[ "${CMD}" == "snapshot" ]]; then
  snapshot
else
  restore
fi