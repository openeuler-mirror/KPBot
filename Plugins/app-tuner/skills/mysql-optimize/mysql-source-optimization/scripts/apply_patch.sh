#!/usr/bin/env bash

# 应用 MySQL 源码补丁（仅 git apply --index 检查/应用；不构建、不改配置）。
# 用法见 usage()（-h）：默认 --check，--apply 需 --approved-change-id。
# 回滚见 SKILL.md §回滚；构建/验证见 build-deployment / test-verification。
# 退出码：0=成功/检查通过；1=拒绝/失败；2=参数错误；3=已应用但回执不完整。

set -u

usage() {
  cat <<'EOF'
用法：
  SRC_ROOT=/abs/path/to/mysql-8.0.25 bash scripts/apply_patch.sh \
    --patch /abs/path/to/your-change.patch
  SRC_ROOT=/abs/path/to/mysql-8.0.25 bash scripts/apply_patch.sh \
    --patch /abs/path/to/your-change.patch --apply --approved-change-id CHG-001
默认 --check（dry-run）；--apply 需要 --approved-change-id。
--patch 必须由操作者提供真实补丁文件；SRC_ROOT 必须是 Git 工作树。
EOF
}

SRC_ROOT="${SRC_ROOT:-}"
PATCH=""
APPLY=0
CHANGE_ID=""
OUTDIR="${OUTDIR:-./patch_record_$(date +%Y%m%d_%H%M%S)}"

while [ $# -gt 0 ]; do
  case "$1" in
    --patch)
      if [ $# -lt 2 ]; then echo "[FAIL] --patch 缺少取值" >&2; exit 2; fi
      PATCH="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --approved-change-id)
      if [ $# -lt 2 ]; then echo "[FAIL] --approved-change-id 缺少取值" >&2; exit 2; fi
      CHANGE_ID="$2"; shift 2 ;;
    --outdir)
      if [ $# -lt 2 ]; then echo "[FAIL] --outdir 缺少取值" >&2; exit 2; fi
      OUTDIR="$2"; shift 2 ;;
    --check) APPLY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[FAIL] 未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${SRC_ROOT}" ] || [ ! -d "${SRC_ROOT}" ]; then
  echo "[FAIL] 需要 SRC_ROOT 指向 MySQL 源码根" >&2
  exit 1
fi
if [ -z "${PATCH}" ]; then
  echo "[FAIL] 需要 --patch <file> 指定补丁文件（仓内不附带补丁原文，须由操作者提供）" >&2
  exit 1
fi

# 关键：PATCH/OUTDIR 在 cd 之前绝对化，否则 --check 用调用 CWD 校验、git apply 用 SRC_ROOT 解析，
# 相对路径会错位（"无法打开补丁" 常被误诊为版本不匹配）。
case "${PATCH}" in
  /*) ;;
  *) PATCH="${PWD}/${PATCH}" ;;
esac
case "${OUTDIR}" in
  /*) ;;
  *) OUTDIR="${PWD}/${OUTDIR}" ;;
esac

if [ ! -f "${PATCH}" ] || [ ! -r "${PATCH}" ]; then
  echo "[FAIL] 补丁文件不存在或不可读：${PATCH}" >&2
  echo "提示：--patch 需指向操作者提供的真实补丁文件（本 skill 不附带补丁原文）。" >&2
  exit 1
fi
if [ "${APPLY}" -eq 1 ] && [ -z "${CHANGE_ID}" ]; then
  echo "[FAIL] --apply 需要 --approved-change-id <ID>" >&2
  exit 1
fi

echo "== apply MySQL patch =="
echo "SRC_ROOT=${SRC_ROOT}"
echo "PATCH=${PATCH}"
echo "APPLY=${APPLY} CHANGE_ID=${CHANGE_ID:-<none>}"
echo

# 私有临时目录（0700）内用 mktemp 原子创建日志，避免共享目录中 PID 命名文件的符号链接竞争。
TMP_PRIV_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mysql_patch.XXXXXXXX")" || {
  echo "[FAIL] 无法创建私有临时目录（mktemp -d 失败）" >&2
  exit 1
}
CHECK_ERR="$(mktemp "${TMP_PRIV_DIR}/check.XXXXXX")" || {
  echo "[FAIL] 无法创建检查日志（mktemp 失败）" >&2
  rm -rf "${TMP_PRIV_DIR}"
  exit 1
}
APPLY_ERR="$(mktemp "${TMP_PRIV_DIR}/apply.XXXXXX")" || {
  echo "[FAIL] 无法创建应用日志（mktemp 失败）" >&2
  rm -rf "${TMP_PRIV_DIR}"
  exit 1
}
trap 'rm -rf "${TMP_PRIV_DIR}"' EXIT

cd "${SRC_ROOT}" || exit 1

# 预检与执行都带 --index，目标必须是 Git 工作树（有索引），否则 dry-run 绿灯会在执行期失败。
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[FAIL] 目标目录不是 Git 工作树：${SRC_ROOT}（--index 检查/应用要求索引与工作树同时存在）" >&2
  exit 1
fi
TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "${TOPLEVEL}" ] && [ -d "${TOPLEVEL}" ]; then
  cd "${TOPLEVEL}" || exit 1
fi
echo "[INFO] Git 工作树根目录：$(pwd -P)"
echo "[INFO] 当前 commit：$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# 与实际应用（git apply --index）保持同一判据：索引与工作树都必须可应用。
if git apply --index --check "${PATCH}" 2>"${CHECK_ERR}"; then
  echo "[PASS] git apply --index --check 通过（工作树与索引均可应用）"
else
  echo "[FAIL] git apply --index --check 失败：" >&2
  cat "${CHECK_ERR}" >&2 2>/dev/null || true
  echo "提示：版本/上下文不匹配，或目标文件存在未暂存修改导致索引不一致；不要强行 patch。" >&2
  exit 1
fi

if [ "${APPLY}" -eq 0 ]; then
  echo
  echo "Dry-run：未应用。使用 --apply --approved-change-id <ID> 应用。"
  exit 0
fi

mkdir -p "${OUTDIR}" || { echo "[FAIL] 无法创建 ${OUTDIR}" >&2; exit 1; }

# 先确认变更前回执完整落盘，再动源码；写失败就不应用补丁。
# 注意：`! { ...; } > file` 在重定向失败时不会可靠地取反，必须显式捕获退出码。
BEFORE_RC=0
if [ -d "${OUTDIR}/before.txt" ] || [ -L "${OUTDIR}/before.txt" ]; then
  BEFORE_RC=1
else
  {
    echo "# change_id=${CHANGE_ID} time=$(date -Is)"
    echo "src_root=${SRC_ROOT}"
    echo "patch=${PATCH}"
    echo "md5=$(md5sum "${PATCH}" 2>/dev/null | awk '{print $1}')"
    echo "commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    git status --short 2>/dev/null | sed -n '1,50p'
  } > "${OUTDIR}/before.txt" 2>&1 || BEFORE_RC=$?
fi
if [ "${BEFORE_RC}" -ne 0 ] || [ ! -s "${OUTDIR}/before.txt" ]; then
  echo "[FAIL] 变更前回执 ${OUTDIR}/before.txt 写入失败（或为空/为目录/为符号链接）；未应用补丁，索引与工作树未改动。" >&2
  exit 1
fi
echo "[INFO] 变更前回执已写入：${OUTDIR}/before.txt"

if git apply --index "${PATCH}" 2>"${APPLY_ERR}"; then
  echo "[PASS] 补丁已应用（工作树与索引均已更新）"
else
  echo "[FAIL] 应用失败：" >&2
  cat "${APPLY_ERR}" >&2 2>/dev/null || true
  cp "${APPLY_ERR}" "${OUTDIR}/apply.err" 2>/dev/null || true
  echo "提示：应用失败；检查 git status 与 ${OUTDIR}/before.txt 后再决定回滚。" >&2
  exit 1
fi

# after.txt 只影响回执完整性，不改变"补丁已应用"的事实，必须用独立退出码明确告知。
AFTER_RC=0
if [ -d "${OUTDIR}/after.txt" ] || [ -L "${OUTDIR}/after.txt" ]; then
  AFTER_RC=1
else
  {
    echo "# after time=$(date -Is)"
    git status --short 2>/dev/null | sed -n '1,80p'
    git diff --cached --stat 2>/dev/null | sed -n '1,40p'
  } > "${OUTDIR}/after.txt" 2>&1 || AFTER_RC=$?
fi
if [ "${AFTER_RC}" -ne 0 ] || [ ! -s "${OUTDIR}/after.txt" ]; then
  echo "[WARN][INCOMPLETE] 补丁已应用并暂存（change_id=${CHANGE_ID}），但变更后回执 ${OUTDIR}/after.txt 写入失败或为空（也可能被目录/符号链接占用）。" >&2
  echo "回执不完整：请手工保存 git status --short 与 git diff --cached --stat 的输出。" >&2
  echo "回滚（先反向 --index --check，再同时撤销工作树与索引，保留其他暂存改动）：" >&2
  echo "  git apply -R --index --check ${PATCH} && git apply -R --index ${PATCH}" >&2
  exit 3
fi

echo "[INFO] 记录：${OUTDIR}"
echo "[INFO] 下一步：构建 → MTR/冒烟 → 四场景 A/B"
echo "[INFO] 回滚（先反向 --index --check，再同时撤销工作树与索引，保留其他暂存改动）："
echo "  git apply -R --index --check ${PATCH} && git apply -R --index ${PATCH}"
exit 0
