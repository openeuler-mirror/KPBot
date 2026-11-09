#!/usr/bin/env bash
# =============================================================================
# Skill-level A/B eval runner (opencode CLI, with vs without skill).
#
# Each mode runs in its own clean workspace:
#   $OUT/with     has .opencode/skills/rocksdb-perf-diagnostics
#   $OUT/without  has no project skill
# The only variable is whether the skill is present / mentioned.
#
# Usage:
#   ./run_ab_subagent.sh [options]
#
# Options:
#   --eval ID        eval id from evals/evals.json (default: 2)
#   --model MODEL    opencode model (default: $OPENCODE_MODEL or
#                    opencode/deepseek-v4.1-flash-free)
#   --out DIR        output root (default: ${TMPDIR:-/tmp}/rocksdb-skill-ab)
#   --timeout SEC    per-run timeout (default: 300)
#   --grade          also run an LLM grader and write grading.json
#   --dry-run        prepare workspaces/prompts, do not call opencode
#   --lenient        exit 0 even if a run fails
#   -h, --help       show this help
#
# Requires: bash, python3, opencode (unless --dry-run).
# The script never touches a real database.
# =============================================================================
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVALS_JSON="$SKILL_DIR/evals/evals.json"
MODEL="${OPENCODE_MODEL:-opencode/deepseek-v4.1-flash-free}"
EVAL_ID="2"
OUT="${TMPDIR:-/tmp}/rocksdb-skill-ab"
TIMEOUT_SEC=300
GRADE=0
DRY_RUN=0
LENIENT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --eval) EVAL_ID="${2:?}"; shift 2 ;;
        --model) MODEL="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --timeout) TIMEOUT_SEC="${2:?}"; shift 2 ;;
        --grade) GRADE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --lenient) LENIENT=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -f "$EVALS_JSON" ]]; then
    echo "error: $EVALS_JSON not found" >&2
    exit 2
fi

# Prepare clean workspaces. Only runtime skill content goes into the with arm:
# SKILL.md + references/ + scripts/. evals/ and cases/ are answer/test assets
# and must never be copied into an agent workspace.
rm -rf "$OUT/with" "$OUT/without" "$OUT/testdata"
mkdir -p "$OUT/with/.opencode/skills/rocksdb-perf-diagnostics" "$OUT/with/results"
mkdir -p "$OUT/without/.opencode" "$OUT/without/results"
mkdir -p "$OUT/testdata"
cp "$SKILL_DIR/SKILL.md" "$OUT/with/.opencode/skills/rocksdb-perf-diagnostics/SKILL.md"
cp -a "$SKILL_DIR/references" "$OUT/with/.opencode/skills/rocksdb-perf-diagnostics/references"
cp -a "$SKILL_DIR/scripts" "$OUT/with/.opencode/skills/rocksdb-perf-diagnostics/scripts"
cp -a "$SKILL_DIR/evals/fixtures/." "$OUT/testdata/"

# Build prompts from evals.json. Fixtures live in $OUT/testdata, outside the
# skill directory; neither arm is told the skill directory path.
python3 - "$EVALS_JSON" "$EVAL_ID" "$OUT" <<'PY'
import json
import os
import sys

evals_json, eval_id, out = sys.argv[1:4]
data = json.load(open(evals_json, encoding="utf-8"))
ev = next((e for e in data.get("evals", []) if str(e.get("id")) == str(eval_id)), None)
if ev is None:
    raise SystemExit(f"eval id {eval_id} not found in {evals_json}")

testdata = os.path.join(out, "testdata")
base = ev["prompt"].replace("evals/fixtures/", testdata + "/")

for mode in ("with", "without"):
    workdir = os.path.join(out, mode)
    prompt = base.replace("/tmp/opencode/eval3/", os.path.join(workdir, "results") + "/")
    if mode == "with":
        prompt += (
            "\n\n---\n执行环境（with）:\n"
            f"- 工作目录: {workdir}\n"
            "- 可用 skill: `.opencode/skills/rocksdb-perf-diagnostics`"
            "（先读取其 SKILL.md 并严格遵循）\n"
            f"- fixtures 绝对路径: {testdata}/\n"
            f"- 需要写文件时写到: {os.path.join(workdir, 'results')}/\n"
            f"- 使用 mock_db_bench.sh 时设置 MOCK_LOG={os.path.join(workdir, 'results', 'mock_invocations.log')}\n"
        )
    else:
        prompt += (
            "\n\n---\n执行环境:\n"
            f"- 工作目录: {workdir}\n"
            f"- fixtures 绝对路径: {testdata}/\n"
            f"- 需要写文件时写到: {os.path.join(workdir, 'results')}/\n"
            f"- 使用 mock_db_bench.sh 时设置 MOCK_LOG={os.path.join(workdir, 'results', 'mock_invocations.log')}\n"
        )
    open(os.path.join(workdir, "prompt.txt"), "w", encoding="utf-8").write(prompt)
PY

echo "skill : $SKILL_DIR"
echo "eval  : id=$EVAL_ID"
echo "model : $MODEL"
echo "out   : $OUT"
echo

RUN_RC=0
for mode in with without; do
    workdir="$OUT/$mode"
    echo "==> $mode"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [dry-run] prompt: $workdir/prompt.txt"
        continue
    fi

    (
        cd "$workdir" || exit 1
        MOCK_LOG="$workdir/results/mock_invocations.log" \
        timeout "$TIMEOUT_SEC" opencode run --auto --format json \
            -m "$MODEL" "$(cat prompt.txt)" \
            > trace.jsonl 2> opencode.log
    )
    rc=$?
    echo "$rc" > "$workdir/exit_code"
    [[ "$rc" -ne 0 ]] && RUN_RC=1

    python3 - "$workdir" <<'PY' || true
import json
import os
import sys

workdir = sys.argv[1]
trace = os.path.join(workdir, "trace.jsonl")

texts = []
seen = set()
tools = []
skills = []


def walk(obj):
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


if os.path.exists(trace):
    for line in open(trace, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        for node in walk(e):
            text = node["text"]
            key = node.get("id") or text
            if key in seen:
                continue
            seen.add(key)
            texts.append(text)
        if e.get("type") != "tool_use":
            continue
        part = e.get("part") or {}
        if part.get("type") != "tool":
            continue
        tool = part.get("tool", "?")
        state = part.get("state") or {}
        if tool not in tools:
            tools.append(tool)
        if tool == "skill":
            name = (state.get("input") or {}).get("name")
            if name and name not in skills:
                skills.append(name)

answer = "\n".join(texts).strip()
open(os.path.join(workdir, "answer.md"), "w", encoding="utf-8").write(answer + "\n")
json.dump(
    {
        "answer_chars": len(answer),
        "tools_used": tools,
        "skills_invoked": skills,
    },
    open(os.path.join(workdir, "trace_summary.json"), "w"),
    ensure_ascii=False,
    indent=2,
)
PY

    echo "  exit: $(cat "$workdir/exit_code")  answer: $workdir/answer.md"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo
    echo "dry-run done. Inspect prompts under $OUT/{with,without}/prompt.txt"
    exit 0
fi

# Optional LLM grading, one call per mode.
if [[ "$GRADE" -eq 1 ]]; then
    for mode in with without; do
        workdir="$OUT/$mode"
        python3 - "$EVALS_JSON" "$EVAL_ID" "$workdir" <<'PY'
import json
import os
import sys

evals_json, eval_id, workdir = sys.argv[1:4]
data = json.load(open(evals_json, encoding="utf-8"))
ev = next((e for e in data["evals"] if str(e.get("id")) == str(eval_id)), None)
answer = open(os.path.join(workdir, "answer.md"), encoding="utf-8", errors="replace").read()
expectations = ev.get("expectations", [])

prompt = (
    "你是严格的评分员。逐条判断下面的 agent 回答是否满足每条断言。\n"
    "只输出一个 JSON 对象，不要解释，格式：\n"
    '{"results":[{"text":"断言原文","passed":true,"evidence":"回答中的依据"}],"pass_rate":0.0}\n\n'
    "断言：\n"
    + "\n".join(f"- {x}" for x in expectations)
    + "\n\nagent 回答：\n"
    + answer
)
open(os.path.join(workdir, "grader_prompt.txt"), "w", encoding="utf-8").write(prompt)
PY

        (
            cd "$workdir" || exit 1
            timeout "$TIMEOUT_SEC" opencode run --auto --format json \
                -m "$MODEL" "$(cat grader_prompt.txt)" \
                > grader.jsonl 2> grader.log
        )

        python3 - "$workdir" <<'PY' || true
import json
import os
import re
import sys

workdir = sys.argv[1]
texts = []


def walk(obj):
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            yield obj["text"]
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


path = os.path.join(workdir, "grader.jsonl")
if os.path.exists(path):
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        texts.extend(walk(e))

text = "\n".join(texts)
match = re.search(r"\{[\s\S]*\}", text)
if not match:
    json.dump({"error": "grader produced no JSON", "raw": text[:1000]},
              open(os.path.join(workdir, "grading.json"), "w"), ensure_ascii=False, indent=2)
else:
    try:
        grade = json.loads(match.group(0))
        json.dump(grade, open(os.path.join(workdir, "grading.json"), "w"),
                  ensure_ascii=False, indent=2)
    except Exception as exc:
        json.dump({"error": f"grader JSON parse failed: {exc}", "raw": match.group(0)[:1000]},
                  open(os.path.join(workdir, "grading.json"), "w"), ensure_ascii=False, indent=2)
PY
    done
fi

# Comparison table.
python3 - "$OUT" <<'PY'
import json
import os
import sys

out = sys.argv[1]
rows = []
for mode in ("with", "without"):
    workdir = os.path.join(out, mode)
    summary = {}
    grading = {}
    for name, dst in (("trace_summary.json", summary), ("grading.json", grading)):
        path = os.path.join(workdir, name)
        if os.path.exists(path):
            try:
                dst.update(json.load(open(path, encoding="utf-8")))
            except Exception:
                pass
    rows.append(
        {
            "mode": mode,
            "exit": open(os.path.join(workdir, "exit_code")).read().strip()
            if os.path.exists(os.path.join(workdir, "exit_code")) else "",
            "answer_chars": summary.get("answer_chars", ""),
            "skills_invoked": ",".join(summary.get("skills_invoked", [])),
            "pass_rate": grading.get("pass_rate", "-"),
        }
    )

keys = ["mode", "exit", "answer_chars", "skills_invoked", "pass_rate"]
print("| " + " | ".join(keys) + " |")
print("|" + "|".join("-" * len(k) for k in keys) + "|")
for row in rows:
    print("| " + " | ".join(str(row.get(k, "")) for k in keys) + " |")
PY

# Exit policy: fail on run error, or if the with-skill run never invoked the skill.
WITH_SKILLS="$OUT/with/trace_summary.json"
if [[ "$RUN_RC" -ne 0 ]]; then
    [[ "$LENIENT" -eq 1 ]] && exit 0 || exit 1
fi
if [[ -f "$WITH_SKILLS" ]] && ! grep -q '"rocksdb-perf-diagnostics"' "$WITH_SKILLS"; then
    echo "warning: with-skill run did not invoke rocksdb-perf-diagnostics" >&2
    [[ "$LENIENT" -eq 1 ]] && exit 0 || exit 1
fi
exit 0
