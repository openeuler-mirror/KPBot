#!/usr/bin/env bash
# run_eval.sh — 在容器内用 opencode 跑 with-skill / without-skill 评测
# 用法: ./run_eval.sh <with|without>
#   with:    --agent sra-eval（.opencode/agents/sra-eval.md，提示加载 sra-dag-tuning skill）
#   without: --agent plain-eval（对照组，不带 skill）
# 凭据: 由 docker run 挂载 ~/.cannbot/session.json 与 opencode auth.json（见 README）
set -euo pipefail
cd /case

MODE=${1:?usage: run_eval.sh <with|without>}
OUT=/case/results
mkdir -p "$OUT" 2>/dev/null || OUT=/tmp/results
chmod 755 "$OUT" 2>/dev/null || true

if [ "$MODE" = "with" ]; then
    PROMPT_FILE=prompts/with-skill.md
    AGENT=sra-eval
else
    PROMPT_FILE=prompts/without-skill.md
    AGENT=plain-eval
fi

PROMPT=$(cat "$PROMPT_FILE")

echo "=== opencode eval: $MODE (agent=$AGENT) ==="
# opencode run: 非交互单次执行；--auto 放开 bash/edit 权限（容器内隔离环境）
opencode run --agent "$AGENT" --auto "$PROMPT" 2>&1 | tee "$OUT/${MODE}-transcript.txt"

# agent 产出的优化代码应在 src/ 下；找出 verify.sh 可用的变体并做 A/B 验证
VARIANT=$(ls src/*.cpp | grep -v dag_baseline | head -1 || true)
if [ -z "$VARIANT" ]; then
    echo "EVAL FAILED: agent 未产出优化代码" | tee -a "$OUT/${MODE}-transcript.txt"
    exit 1
fi
echo "=== A/B verify: baseline vs $VARIANT ==="
./verify.sh "$VARIANT" 2>&1 | tee "$OUT/${MODE}-verify.txt"
echo "=== result saved: $OUT/${MODE}-*.txt ==="
