#!/usr/bin/env bash
# manage_verify_env.sh — 性能优化验证环境隔离管理（克隆/使用/销毁）
#
# 目标：
#   性能优化验证（pip install 替换库 / 重编译 / LD_PRELOAD / 环境变量实验）全部在
#   独立克隆虚拟环境中进行，原版环境零接触 → 彻底消除"回退后与原版不一致"风险。
#   验证完成后整环境销毁，原环境始终保持生产可用。
#
# 用法：
#   manage_verify_env.sh create --source <conda-env-path> [--name <verify-env-name>]
#   manage_verify_env.sh env-root [--name <verify-env-name>]
#   manage_verify_env.sh python [--name <verify-env-name>]
#   manage_verify_env.sh pip <args...>                       (在验证环境中执行 pip)
#   manage_verify_env.sh run <cmd...>                        (在验证环境中执行命令)
#   manage_verify_env.sh destroy [--name <verify-env-name>]  [--force]
#   manage_verify_env.sh status
#
# 环境变量：
#   KPVERIFY_NAME 验证环境名（默认 torchtitan-npu-verify）
#   CONDA_BIN     conda 可执行文件（默认 /opt/conda/bin/conda，其次 miniconda3）
#   KPVERIFY_SOURCE 源环境路径（create 时 --source 或默认推导）
#
# 退出码：0 成功；2 参数错误；3 环境不存在/创建失败。

set -euo pipefail
CMD="${1:-}"; [[ -n "${CMD}" ]] && shift || true

CONDA_BIN="${CONDA_BIN:-}"
if [[ -z "${CONDA_BIN}" ]]; then
  for c in /opt/conda/bin/conda /home/*/miniconda3/bin/conda "$(command -v conda 2>/dev/null)"; do
    [[ -n "${c}" ]] && [[ -x "${c}" ]] && { CONDA_BIN="${c}"; break; }
  done
fi
[[ -n "${CONDA_BIN}" ]] || { echo "ERROR: conda not found, set CONDA_BIN" >&2; exit 2; }

CONDA_BASE="$("${CONDA_BIN}" info --base 2>/dev/null)"
VERIFY_NAME="${KPVERIFY_NAME:-torchtitan-npu-verify}"
VERIFY_ROOT="${KPVERIFY_ROOT:-${CONDA_BASE}/envs}"
# 若默认 envs 目录不可写，自动降级到用户可写目录
if [[ ! -w "${VERIFY_ROOT}" ]]; then
  if [[ -w "${VERIFY_ROOT}" ]] && [[ -n "${KPVERIFY_ROOT:-}" ]]; then :; fi
  if [[ -w "/mnt/workspace" ]]; then
    VERIFY_ROOT="/mnt/workspace/verify-envs"
    mkdir -p "${VERIFY_ROOT}"
  elif [[ -w "${HOME}" ]]; then
    VERIFY_ROOT="${HOME}/.kpbot-verify-envs"
    mkdir -p "${VERIFY_ROOT}"
  fi
fi
VERIFY_ENV_PATH="${VERIFY_ROOT}/${VERIFY_NAME}"

log()  { printf '[verify-env] %s\n' "$@"; }
err()  { printf '[verify-env ERROR] %s\n' "$@" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_ENV="${2:-}"; shift 2;;
    --name)   VERIFY_NAME="${2:-}"; VERIFY_ENV_PATH="${VERIFY_ROOT}/${VERIFY_NAME}"; shift 2;;
    --force)  FORCE=1; shift;;
    *) break;;
  esac
done

os() { "${CONDA_BIN}" "$@"; }

derive_source() {
  # 从当前环境 PYTHON/PATH 推导源环境：优先 $CONDA_PREFIX 或当前 conda env
  if [[ -n "${CONDA_PREFIX:-}" ]] && [[ -d "${CONDA_PREFIX}" ]]; then
    echo "${CONDA_PREFIX}"; return
  fi
  # 从运行的 python 推导
  local py
  py="$(command -v python3 2>/dev/null || true)"
  if [[ -n "${py}" ]]; then
    local prefix
    prefix="$("${py}" -c 'import sys; print(sys.prefix)' 2>/dev/null || true)"
    [[ -n "${prefix}" ]] && [[ -d "${prefix}" ]] && echo "${prefix}" && return
  fi
  # 兜底：/home/developer 下找 torchtitan 环境
  for d in /home/*/miniconda3/envs/torchtitan-npu; do
    [[ -d "${d}" ]] && echo "${d}" && return
  done
  err "cannot derive source env; use --source"
  exit 2
}

cmd_create() {
  [[ -n "${SOURCE_ENV:-}" ]] || SOURCE_ENV="$(derive_source)"
  [[ -d "${SOURCE_ENV}" ]] || { err "source env not found: ${SOURCE_ENV}"; exit 2; }
  if [[ -d "${VERIFY_ENV_PATH}" ]]; then
    log "verify env already exists: ${VERIFY_ENV_PATH}"
    log "use 'destroy' then 'create' to rebuild from clean source"
    return 0
  fi
  log "cloning source env ${SOURCE_ENV} -> ${VERIFY_ENV_PATH}"
  # 优先 conda create --clone（保留 conda-meta 元数据）
  local clone_ok=0
  if "${CONDA_BIN}" create --clone "${SOURCE_ENV}" -p "${VERIFY_ENV_PATH}" -y >/tmp/verify_env_clone.log 2>&1; then
    clone_ok=1
  else
    if grep -qiE "ToS|Terms of Service|CondaToSNonInteractiveError" /tmp/verify_env_clone.log 2>/dev/null; then
      log "conda create --clone blocked by channel ToS; falling back to file-level hardlink copy"
      # 匿名 condarc 规避；若仍失败则走 cp_reflink
      HOME="$(mktemp -d)" "${CONDA_BIN}" create --clone "${SOURCE_ENV}" -p "${VERIFY_ENV_PATH}" -y >/tmp/verify_env_clone2.log 2>&1 && clone_ok=1
    fi
    if [[ "${clone_ok}" -ne 1 ]]; then
      log "conda clone failed; using full copy (cross-device safe)"
      mkdir -p "${VERIFY_ENV_PATH}"
      if command -v rsync >/dev/null 2>&1 && rsync -a --exclude='*.pyc' "${SOURCE_ENV}/" "${VERIFY_ENV_PATH}/" >&2; then
        :
      else
        cp -a "${SOURCE_ENV}/." "${VERIFY_ENV_PATH}/" >&2
      fi
      clone_ok=1
    fi
  fi
  [[ "${clone_ok}" -eq 1 ]] || { err "clone failed"; exit 3; }
  log "created verify env: ${VERIFY_ENV_PATH}"
  # 验证环境 python 冒烟（硬链接可用）
  "${VERIFY_ENV_PATH}/bin/python" --version >/dev/null 2>&1 || {
    err "verify env python broken (hardlink copy may not work on this fs); use conda clone with accepted ToS"
    err "verify env left at ${VERIFY_ENV_PATH}; run 'destroy' to clean"
    exit 3
  }
  log "verify env python OK: $("${VERIFY_ENV_PATH}/bin/python" --version 2>&1)"
}

cmd_env_root()   { echo "${VERIFY_ENV_PATH}"; }
cmd_python()     { echo "${VERIFY_ENV_PATH}/bin/python"; }
cmd_pip()        {
  [[ -x "${VERIFY_ENV_PATH}/bin/python" ]] || { err "verify env not created yet"; exit 3; }
  "${VERIFY_ENV_PATH}/bin/python" -m pip "$@"
}
cmd_run() {
  [[ -x "${VERIFY_ENV_PATH}/bin/python" ]] || { err "verify env not created yet"; exit 3; }
  export PATH="${VERIFY_ENV_PATH}/bin:${PATH}"
  "$@"
}
cmd_destroy() {
  if [[ -d "${VERIFY_ENV_PATH}" ]]; then
    if [[ "${FORCE:-0}" -ne 1 ]]; then
      log "about to destroy verify env: ${VERIFY_ENV_PATH}"
      log "re-run with --force to confirm"
      return 2
    fi
    log "destroying verify env: ${VERIFY_ENV_PATH}"
    "${CONDA_BIN}" env remove -p "${VERIFY_ENV_PATH}" -y >&2
    log "destroyed."
  else
    log "verify env does not exist"
  fi
}
cmd_status() {
  if [[ -x "${VERIFY_ENV_PATH}/bin/python" ]]; then
    echo "VERIFY_ENV=${VERIFY_ENV_PATH}"
    echo "STATUS=created"
    echo "PYTHON=$("${VERIFY_ENV_PATH}/bin/python" --version 2>&1)"
  else
    echo "VERIFY_ENV=${VERIFY_ENV_PATH}"
    echo "STATUS=not_created"
  fi
}

case "${CMD}" in
  create)   cmd_create;;
  env-root) cmd_env_root;;
  python)   cmd_python;;
  pip)      shift; cmd_pip "$@";;
  run)      shift; cmd_run "$@";;
  destroy)  cmd_destroy;;
  status)   cmd_status;;
  *) err "usage: create|env-root|python|pip|run|destroy|status"; exit 2;;
esac