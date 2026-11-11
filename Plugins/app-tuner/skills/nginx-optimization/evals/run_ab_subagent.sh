#!/usr/bin/env bash
# =============================================================================
# Skill-level A/B eval runner (opencode CLI, with vs without skill).
#
# Each mode runs in its own clean workspace:
#   $OUT/with     has .opencode/skills/nginx-optimization
#   $OUT/without  has no project skill
# The only variable is whether the skill is present / mentioned.
#
# 内置 PR#100 检视加固：
#   P1  with 工作区只复制 SKILL.md 与 references/（评分答案 evals/ 绝不进工作区）；
#   P2  OUT 只在绝对路径且只清理 with/without 两个自有子目录（清理前打印路径）。
#
# Usage:
#   ./run_ab_subagent.sh [options]
#
# Options:
#   --eval ID        eval id from evals/evals.json (default: 1)
#   --model MODEL    opencode model (default: $OPENCODE_MODEL or
#                    opencode/deepseek-v4.1-flash-free)
#   --out DIR        output root, must be absolute (default: /tmp/nginx-skill-ab)
#   --timeout SEC    per-run timeout (default: 300)
#   --dry-run        prepare workspaces/prompts, do not call opencode
#   --lenient        exit 0 even if a run fails
#   -h, --help       show this help
#
# Requires: bash, python3, opencode (unless --dry-run).
# The script never touches a real nginx or any system service.
# =============================================================================
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVALS_JSON="$SKILL_DIR/evals/evals.json"
MODEL="${OPENCODE_MODEL:-opencode/deepseek-v4.1-flash-free}"
EVAL_ID="1"
OUT="${TMPDIR:-/tmp}/nginx-skill-ab"
TIMEOUT_SEC=300
DRY_RUN=0
LENIENT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --eval) EVAL_ID="${2:?}"; shift 2 ;;
        --model) MODEL="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --timeout) TIMEOUT_SEC="${2:?}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --lenient) LENIENT=1; shift ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -f "$EVALS_JSON" ]]; then
    echo "error: $EVALS_JSON not found" >&2
    exit 2
fi

# P2：OUT 必须绝对路径
if [[ "${OUT}" != /* ]]; then
    echo "error: --out must be an absolute path, got: ${OUT}" >&2
    exit 2
fi

# P2：只清理自有子目录，清理动作可见
for sub in with without; do
    if [[ -e "${OUT}/${sub}" ]]; then
        echo "[cleanup] ${OUT}/${sub} -> ${OUT}/${sub}.bak.$(date +%Y%m%d%H%M%S)"
        mv "${OUT}/${sub}" "${OUT}/${sub}.bak.$(date +%Y%m%d%H%M%S)"
    fi
done
mkdir -p "${OUT}/with/.opencode/skills/nginx-optimization" "${OUT}/with/results" \
         "${OUT}/without/results"

# P1：被测内容只含 SKILL.md 与 references/（评分答案不进 with 工作区）
cp "${SKILL_DIR}/SKILL.md" "${OUT}/with/.opencode/skills/nginx-optimization/SKILL.md"
cp -r "${SKILL_DIR}/references" "${OUT}/with/.opencode/skills/nginx-optimization/references"

PROMPT="$(python3 - "$EVALS_JSON" "$EVAL_ID" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
eid = int(sys.argv[2])
for e in data.get("evals", []):
    if int(e.get("id", -1)) == eid:
        print(e.get("prompt", ""))
        sys.exit(0)
sys.exit(1)
PY
)"
if [[ -z "${PROMPT}" ]]; then
    echo "error: eval id ${EVAL_ID} not found in ${EVALS_JSON}" >&2
    exit 2
fi

WITH_PROMPT="请先调用 nginx-optimization skill（用 skill 工具加载），再完成下面的任务。

${PROMPT}

把完整过程与结果写到 results/report.md。"
WITHOUT_PROMPT="${PROMPT}

把完整过程与结果写到 results/report.md。"

printf '%s\n' "${WITH_PROMPT}" > "${OUT}/with/prompt.md"
printf '%s\n' "${WITHOUT_PROMPT}" > "${OUT}/without/prompt.md"

if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "[dry-run] workspaces ready:"
    echo "  ${OUT}/with     (skill: SKILL.md + references/)"
    echo "  ${OUT}/without  (no skill)"
    echo "[dry-run] prompt length: with=$(wc -c < "${OUT}/with/prompt.md") without=$(wc -c < "${OUT}/without/prompt.md")"
    exit 0
fi

RC=0
for mode in with without; do
    echo "==> ${mode}"
    ws="${OUT}/${mode}"
    if ! timeout "${TIMEOUT_SEC}" opencode run --auto --format json -m "${MODEL}" \
            "$(cat "${ws}/prompt.md")" \
            > "${ws}/results/trace.jsonl" 2> "${ws}/results/opencode.log"; then
        echo "    run failed (see ${ws}/results/opencode.log)" >&2
        RC=1
        continue
    fi
    cp -f "${ws}/results/trace.jsonl" "${ws}/results/trace.copy.jsonl" 2>/dev/null || true
done

[[ ${RC} -ne 0 && ${LENIENT} -eq 0 ]] && exit 1
exit 0
