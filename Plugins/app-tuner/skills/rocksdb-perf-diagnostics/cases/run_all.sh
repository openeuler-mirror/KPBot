#!/usr/bin/env bash
# =============================================================================
# One-click runner for THIS skill's cases. Self-contained: it does not modify
# KPBot core (bin/, tests/, docs/). It only discovers the sibling
# cases/*/case.yaml, builds the image declared there, and calls each case's
# own run_ab.sh (with / without).
#
# Usage:
#   ./cases/run_all.sh [options]
#
# Options:
#   --case NAME      run only one case directory (default: all)
#   --model MODEL    forwarded to the case container as MODEL
#   --out DIR        artifact root (default: ${TMPDIR:-/tmp}/rocksdb-cases)
#   --timeout SEC    per-case timeout (default: 2400; per-arm RUN_TIMEOUT = half)
#   --no-build       skip docker build (use existing images)
#   --dry-run        print build/run commands, do not execute
#   --list           list discovered cases and exit
#   --strict         require with-success and with > without
#   --lenient        always exit 0 (still prints the summary)
#   -h, --help       show this help
#
# Requirements: bash, python3 (PyYAML optional), docker, opencode binary/auth
# for the case containers. It never touches a real database.
# =============================================================================
set -uo pipefail

CASES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${TMPDIR:-/tmp}/rocksdb-cases"
MODEL="${OPENCODE_MODEL:-}"
ONLY_CASE=""
DO_BUILD=1
DRY_RUN=0
LIST=0
STRICT=0
LENIENT=0
TIMEOUT_SEC=2400

while [[ $# -gt 0 ]]; do
    case "$1" in
        --case) ONLY_CASE="${2:?}"; shift 2 ;;
        --model) MODEL="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --timeout) TIMEOUT_SEC="${2:?}"; shift 2 ;;
        --no-build) DO_BUILD=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --list) LIST=1; shift ;;
        --strict) STRICT=1; shift ;;
        --lenient) LENIENT=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# 未显式 --model 时，在 CANNBot 环境里从 ANTHROPIC_MODEL 推导模型 ID，
# 例如 deepseek-v4-flash[1m] -> cannbot/deepseek-v4-flash。
CANNBOT_BIN=/usr/local/lib/node_modules/cannbot/node_modules/cannbot-linux-arm64/bin/cannbot
if [[ -z "$MODEL" && -x "$CANNBOT_BIN" && -n "${ANTHROPIC_MODEL:-}" ]]; then
    _m="${ANTHROPIC_MODEL%%[*}"
    if [[ "$_m" == */* ]]; then
        MODEL="$_m"
    else
        MODEL="cannbot/$_m"
    fi
fi
echo "model : ${MODEL:-<case default>}"

mapfile -t CASE_YAMLS < <(find "$CASES_DIR" -mindepth 2 -maxdepth 2 -name case.yaml | sort)
if [[ "${#CASE_YAMLS[@]}" -eq 0 ]]; then
    echo "error: no cases/*/case.yaml under $CASES_DIR" >&2
    exit 2
fi

# Print: name<TAB>tier<TAB>image<TAB>context<TAB>dockerfile
case_meta() {
    python3 - "$1" <<'PY'
import sys

path = sys.argv[1]
try:
    import yaml  # type: ignore
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    build = data.get("build") or {}
    values = [
        data.get("name"),
        data.get("tier"),
        data.get("image"),
        build.get("context"),
        build.get("dockerfile"),
    ]
except ImportError:
    data = {}
    build = {}
    section = None
    for raw in open(path, encoding="utf-8"):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("build:"):
            section = "build"
            continue
        if not line.startswith((" ", "\t")):
            section = None
            if ":" in line:
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip().strip('"').strip("'")
        elif section == "build" and ":" in line:
            key, value = line.split(":", 1)
            build[key.strip()] = value.strip().strip('"').strip("'")
    values = [
        data.get("name"),
        data.get("tier"),
        data.get("image"),
        build.get("context"),
        build.get("dockerfile"),
    ]

print("\t".join("" if v is None else str(v) for v in values))
PY
}

if [[ "$LIST" -eq 1 ]]; then
    printf '%-32s %-8s %s\n' "CASE" "TIER" "IMAGE"
    for yaml in "${CASE_YAMLS[@]}"; do
        IFS=$'\t' read -r name tier image _context _dockerfile < <(case_meta "$yaml")
        [[ -n "$ONLY_CASE" && "$name" != "$ONLY_CASE" ]] && continue
        printf '%-32s %-8s %s\n' "$name" "$tier" "$image"
    done
    exit 0
fi

if [[ -e "$OUT" && ! -d "$OUT" ]]; then
    echo "error: --out exists and is not a directory: $OUT" >&2
    exit 2
fi
mkdir -p "$OUT"
if [[ -n "$MODEL" ]]; then
    export DOCKER_RUN_ARGS="${DOCKER_RUN_ARGS:---network host} -e MODEL=$MODEL"
fi
FAILED=0
SELECTED=0
declare -a SUMMARY=()

for yaml in "${CASE_YAMLS[@]}"; do
    case_dir="$(dirname "$yaml")"
    IFS=$'\t' read -r name tier image context dockerfile < <(case_meta "$yaml")

    [[ -n "$ONLY_CASE" && "$name" != "$ONLY_CASE" ]] && continue
    SELECTED=$((SELECTED + 1))

    echo
    echo "==> case: ${name:-$(basename "$case_dir")} (tier=${tier:-?})"

    if [[ "${tier:-}" != "agent" ]]; then
        echo "  skip: only tier=agent cases use run_ab.sh"
        SUMMARY+=("${name:-$(basename "$case_dir")}|${tier:-?}|skipped|-|-|skipped|tier != agent")
        continue
    fi

    if [[ -z "$context" || -z "$dockerfile" ]]; then
        echo "  skip: case.yaml missing build.context/build.dockerfile"
        SUMMARY+=("${name:-?}|${tier}|missing-build|-|-|fail|case.yaml missing build.context/build.dockerfile")
        FAILED=1
        continue
    fi

    context_dir="$(cd "$case_dir/$context" && pwd)" || {
        echo "  fail: build context not found: $case_dir/$context"
        SUMMARY+=("${name:-?}|${tier}|bad-context|-|-|fail|build context not found")
        FAILED=1
        continue
    }
    # case.yaml 的 build.context 假定 KPBot 的 Plugins/<plugin>/skills/ 布局。
    # 在 some_skills / .opencode 等布局下，$context_dir 里没有
    # skills/rocksdb-perf-diagnostics；此时构造一个临时 context，使 Dockerfile
    # 里的 COPY skills/rocksdb-perf-diagnostics/... 仍然成立。
    if [[ ! -d "$context_dir/skills/rocksdb-perf-diagnostics" ]]; then
        BUILD_CTX="$OUT/.build-context"
        rm -rf "$BUILD_CTX"
        mkdir -p "$BUILD_CTX/skills"
        cp -a "$CASES_DIR/.." "$BUILD_CTX/skills/rocksdb-perf-diagnostics"
        context_dir="$BUILD_CTX"
    fi
    dockerfile_path="$context_dir/$dockerfile"

    if [[ "$DO_BUILD" -eq 1 ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo "  [dry-run] docker build --network host -f $dockerfile_path -t $image $context_dir"
        else
            if ! timeout "$TIMEOUT_SEC" docker build --network host \
                    -f "$dockerfile_path" -t "$image" "$context_dir"; then
                echo "  fail: docker build"
                SUMMARY+=("${name:-?}|${tier}|build-fail|-|-|fail|docker build failed")
                FAILED=1
                continue
            fi
        fi
    fi

    run_dir="$OUT/$name"
    rm -rf "$run_dir"
    mkdir -p "$run_dir"
    run_args="--save $run_dir $image"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [dry-run] DOCKER_RUN_ARGS='${DOCKER_RUN_ARGS:-}' (cd $case_dir && ./run_ab.sh $run_args)"
        SUMMARY+=("${name:-?}|${tier}|dry-run|-|-|dry-run|dry-run")
        continue
    fi

    if ! (cd "$case_dir" && RUN_TIMEOUT="${RUN_TIMEOUT:-$((TIMEOUT_SEC / 2))}" timeout "$TIMEOUT_SEC" ./run_ab.sh $run_args); then
        echo "  fail: run_ab.sh"
        SUMMARY+=("${name:-?}|${tier}|run-fail|-|-|fail|run_ab.sh failed")
        FAILED=1
        continue
    fi

    meta=$(python3 - "$yaml" "$run_dir" <<'PY'
import json
import os
import sys

yaml_path, run_dir = sys.argv[1], sys.argv[2]
try:
    import yaml  # type: ignore
    data = yaml.safe_load(open(yaml_path, encoding="utf-8")) or {}
except ImportError:
    data = {}

asserts = data.get("assert") or []
with_path = os.path.join(run_dir, "with.result.json")
without_path = os.path.join(run_dir, "without.result.json")


def load(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


with_result = load(with_path)
without_result = load(without_path)


def first_trace_error(run_dir, mode):
    path = os.path.join(run_dir, f"{mode}.trace.jsonl")
    if not os.path.exists(path):
        return ""
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "error":
            continue
        error = event.get("error") or {}
        data = error.get("data") or {}
        message = data.get("message") or error.get("name") or ""
        if message:
            return str(message)
    return ""


def reason_with_trace(run_dir, result, mode):
    base = result.get("reason", "")
    trace = first_trace_error(run_dir, mode)
    if trace and trace not in base:
        return f"{base}: {trace}" if base else trace
    return base


with_reason = reason_with_trace(run_dir, with_result, "with")
without_reason = reason_with_trace(run_dir, without_result, "without")
if with_reason and without_reason and with_reason != without_reason:
    reason = f"with={with_reason}; without={without_reason}"
else:
    reason = with_reason or without_reason

checks = []
for item in asserts:
    kind = item.get("type")
    if kind == "exit_code":
        ok = str(item.get("expect")) == "0" and bool(with_result)
        checks.append((f"exit_code={item.get('expect')}", ok))
    elif kind == "json_field":
        field = item.get("field")
        expect = item.get("expect")
        op = item.get("op", "eq")
        actual = with_result.get(field)
        if op == "gte":
            ok = actual is not None and actual >= expect
        elif op == "lte":
            ok = actual is not None and actual <= expect
        else:
            ok = actual == expect
        checks.append((f"{field}{op if op != 'eq' else '='}{expect}(actual={actual})", ok))
passed = sum(1 for _, ok in checks if ok)
total = len(checks)
status = "pass" if (total > 0 and passed == total) else ("fail" if total else "unknown")
print("\t".join([
    str(with_result.get("success", "")),
    str(without_result.get("success", "")),
    str(with_result.get("assertion_pass_rate", with_result.get("pass_rate", "")) or "-"),
    f"{passed}/{total}",
    status,
    reason.replace("\t", " ").replace("\n", " ") or "-",
]))
PY
)
    IFS=$'\t' read -r with_ok without_ok with_rate assert_ok status reason <<< "$meta"

    if [[ "$status" == "fail" ]]; then
        FAILED=1
    fi
    if [[ "$STRICT" -eq 1 && "$with_ok" != "True" ]]; then
        FAILED=1
    fi

    SUMMARY+=("${name:-?}|${tier}|with=${with_ok}|without=${without_ok}|rate=${with_rate:-?}|${assert_ok:-?} ${status:-?}|${reason:-}")
done

echo
echo "=== summary ==="
printf '%-32s %-6s %-10s %-12s %-8s %-12s %s\n' "CASE" "TIER" "WITH" "WITHOUT" "RATE" "ASSERTS" "REASON"
for row in "${SUMMARY[@]}"; do
    IFS='|' read -r c t w wo r a reason <<< "$row"
    printf '%-32s %-6s %-10s %-12s %-8s %-12s %s\n' "$c" "$t" "$w" "$wo" "$r" "$a" "$reason"
done

if [[ "$SELECTED" -eq 0 ]]; then
    echo "warning: no case matched --case ${ONLY_CASE}" >&2
    [[ "$LENIENT" -eq 1 ]] && exit 0 || exit 1
fi

[[ "$LENIENT" -eq 1 ]] && exit 0
[[ "$FAILED" -eq 0 ]] && exit 0 || exit 1
