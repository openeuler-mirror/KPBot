#!/usr/bin/env bash
# run-sql.sh — submit one SQL case file to a Flink cluster, wait for the job
# to reach FINISHED over REST, and capture the print-sink changelog rows.
#
# Usage: run-sql.sh [--overwrite] {native|gfv} <case.sql> <out-file>
#
# A side counts as complete (exit 0) only after four checks:
#   1. build gate  — $FLINK_HOME/lib holds what the mode expects (native:
#                    no gluten/velox jars; gfv: some present);
#                    RUN_SQL_SKIP_BUILD_CHECK=1 overrides
#   2. FINISHED    — flink_job.py follows the submitted job(s) to a terminal
#                    state; anything other than FINISHED fails the side
#   3. stack check — the job plan must match the mode (gfv: gluten nodes in
#                    the plan; native: none); catches "jars swapped but the
#                    cluster was never restarted";
#                    RUN_SQL_SKIP_PLAN_CHECK=1 overrides
#   4. non-empty   — a zero-row capture is a failure, not an empty result
#
# The mode tag says which build the cluster is expected to run; both modes
# submit to the currently running cluster. Switching builds means
# restarting the cluster — that is a caller decision, never this script:
# restart only after the previous side printed "side complete" (exit 0). A
# failed side (non-zero exit) must be fixed and rerun, never switched away
# from. Restarts wipe the Flink log dir; the evidence is already on disk in
# the capture file by then.
#
# Pre-submit guard (single node): the print-sink capture reads only this
# machine's TaskManager logs. flink_job.py tms-local refuses the run
# (exit 2, nothing submitted) when any registered TaskManager is not on
# this machine — on a distributed cluster the remote partitions' rows
# would be invisible and a false MATCH could pass.
#
# Flink location: $FLINK_HOME if set; otherwise fall back to the GlutenFlink
# workspace archive .gfvbot/env.json (field stack.flink.path), searched by
# walking upward from the CURRENT directory. Missing both -> fail fast.
# REST endpoint: FLINK_REST_URL env, else the cluster conf
# (rest.address/rest.port) — the address sql-client.sh submits to.
# FLINK_JOB_TIMEOUT (seconds, default 600) bounds waiting for FINISHED;
# FLINK_JOB_POLL (seconds, default 1) is the poll interval.
#
# The sql client ($FLINK_HOME/bin/sql-client.sh) is validated up front:
# missing or not executable fails fast (exit 1, nothing submitted) instead
# of surfacing later as a bare "client exited 127" deep in the wait phase.
# A submit failure that does run distinguishes "client did not start"
# (126/127: redirect or executability problem) from "client started but
# exited non-zero" in its message.
#
# Capture model: snapshot every TaskManager .out file's line count before
# submission, then keep only the lines appended after it (new .out files are
# taken whole). Duplicate rows are PRESERVED — the changelog fold in
# changelog_diff.py treats output as a multiset. At parallelism > 1 the print
# sink prefixes each row with its subtask id ("3> +I[...]"); the prefix is
# stripped during capture so downstream sees pure FLAG[payload] rows.
#
# Sidecars: <out-minus-.out>.submit.log (sql-client output) and .job.json
# (job states, durations, gluten node counts — written by flink_job.py).
#
# Out-file guard (same semantics as flink_job.py --out): the capture path
# and its sidecars must be regular paths inside the project directory (the
# working directory), not symlinks; an existing capture is only replaced
# with --overwrite, and an existing non-regular file (fifo, directory) is
# never written. The sidecars refresh in place with the capture. The
# capture directory itself is created up front (before the submit-log
# redirect runs) and must be writable; the filter scratch is created
# exclusively via mktemp next to the capture — never a fixed name a
# pre-existing file could occupy, and only the file this run created is
# ever moved or cleaned up.
#
# Exit codes: 0 side complete; 1 failed job / plan mismatch / empty capture /
# submission error; 2 REST unreachable, out-file or capture-directory
# refusal, or remote-TaskManager refusal (distributed cluster); 3 job did
# not finish in time.
set -euo pipefail

OVERWRITE=0
if [ "${1:-}" = "--overwrite" ]; then OVERWRITE=1; shift; fi
mode="${1:?usage: run-sql.sh [--overwrite] native-or-gfv case.sql out-file}"
sql="${2:?usage: run-sql.sh [--overwrite] native-or-gfv case.sql out-file}"
out="${3:?usage: run-sql.sh [--overwrite] native-or-gfv case.sql out-file}"
case "$mode" in
  native|gfv) ;;
  *) echo "usage: run-sql.sh [--overwrite] {native|gfv} <case.sql> <out-file>" >&2; exit 1 ;;
esac
[ -f "$sql" ] || { echo "case sql not found: $sql" >&2; exit 1; }

submit_log="${out%.out}.submit.log"
job_json="${out%.out}.job.json"

# --- out-file guard (before anything runs: a bad path must not touch the machine)
PROJECT_ROOT="$(pwd -P)"
out_refuse() { echo "run-sql.sh: $1" >&2; exit 2; }
inside_project() {  # <path>: realpath inside the working directory, not the root itself
  local real
  real="$(realpath -m -- "$1")"
  case "$real" in
    "$PROJECT_ROOT"/*) return 0 ;;
    *) return 1 ;;
  esac
}
for p in "$out" "$submit_log" "$job_json"; do
  [ -L "$p" ] && out_refuse "the out file must be a regular path inside the project directory (the working directory), not a symlink or a path outside it: $p"
  inside_project "$p" || out_refuse "the out file must be a regular path inside the project directory (the working directory), not a symlink or a path outside it: $p"
  if [ -e "$p" ]; then
    [ -f "$p" ] || out_refuse "the out file exists and is not a regular file, refusing to overwrite it: $p"
  fi
done
if [ -e "$out" ]; then
  [ "$OVERWRITE" = 1 ] || out_refuse "the out file already exists, pass --overwrite to replace it: $out"
fi

# Create the capture directory up front: the submit-log redirect below
# happens before the sql client runs, and a missing parent would turn a
# path problem into a bogus "client failed" plus a long wait for a job
# that was never submitted.
out_dir="$(dirname "$out")"
if ! mkdir -p "$out_dir" 2>/dev/null; then
  out_refuse "cannot create the capture directory: $out_dir"
fi
[ -d "$out_dir" ] && [ -w "$out_dir" ] || \
  out_refuse "capture directory not writable: $out_dir"

# Resolve FLINK_HOME: env var first, then .gfvbot/env.json walked up from CWD.
FLINK_HOME="${FLINK_HOME:-}"
if [ -z "$FLINK_HOME" ]; then
  env_json=""
  search_dir="$(pwd -P)"
  while [ "$search_dir" != "/" ]; do
    if [ -f "$search_dir/.gfvbot/env.json" ]; then
      env_json="$search_dir/.gfvbot/env.json"
      break
    fi
    search_dir="$(dirname "$search_dir")"
  done
  if [ -n "$env_json" ]; then
    FLINK_HOME="$(python3 - "$env_json" <<'PY'
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    path = cfg.get("stack", {}).get("flink", {}).get("path", "")
    print(path if isinstance(path, str) else "")
except Exception:
    print("")
PY
)"
  fi
fi
[ -n "$FLINK_HOME" ] || {
  echo "FLINK_HOME not set and no .gfvbot/env.json (stack.flink.path) found upward from CWD" >&2
  exit 1
}
[ -d "$FLINK_HOME" ] || { echo "FLINK_HOME is not a directory: $FLINK_HOME" >&2; exit 1; }

sql_client="$FLINK_HOME/bin/sql-client.sh"
if [ ! -f "$sql_client" ] || [ ! -x "$sql_client" ]; then
  echo "sql client not found or not executable: $sql_client" >&2
  echo "  check the Flink installation under \$FLINK_HOME (bin/sql-client.sh); nothing was submitted" >&2
  exit 1
fi

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
job_tool="$skill_dir/scripts/flink_job.py"

# Check 1 — build gate: which build is the cluster expected to run?
if [ "${RUN_SQL_SKIP_BUILD_CHECK:-0}" != "1" ]; then
  gluten_jar="$(ls "$FLINK_HOME/lib" 2>/dev/null | grep -iE 'gluten|velox' | head -1 || true)"
  case "$mode" in
    native) [ -z "$gluten_jar" ] || {
                echo "[$mode] gluten/velox jar present in lib ($gluten_jar) — cluster is NOT native Flink" >&2
                echo "  restart the cluster on the native build, or set RUN_SQL_SKIP_BUILD_CHECK=1 to override" >&2
                exit 1; } ;;
    gfv)    [ -n "$gluten_jar" ] || {
                echo "[$mode] no gluten/velox jars in $FLINK_HOME/lib — cluster is NOT the GlutenFlink build" >&2
                echo "  load the GlutenFlink build (velox4j .so + gluten-flink jars into lib) and restart, or set RUN_SQL_SKIP_BUILD_CHECK=1 to override" >&2
                exit 1; } ;;
  esac
fi

# REST endpoint + pre-submit job snapshot. Unreachable REST fails the side
# BEFORE anything is submitted.
rest_url="$(python3 "$job_tool" rest-url --flink-home "$FLINK_HOME")"
if ! jobs_before="$(python3 "$job_tool" snapshot --rest "$rest_url")"; then
  echo "[$mode] REST $rest_url unreachable — nothing was submitted" >&2
  exit 2
fi

# Single-node guard: a distributed cluster's remote TaskManagers would put
# rows outside this machine's TM logs — the capture (and therefore the
# comparison) would be silently incomplete. Refused before submitting.
if ! python3 "$job_tool" tms-local --rest "$rest_url"; then
  echo "[$mode] side NOT complete: refusing to submit — TaskManagers are off this machine (distributed cluster); nothing was submitted" >&2
  exit 2
fi

# Capture snapshot: "<path>:<linecount>" for existing .out files.
log_dir="$FLINK_HOME/log"
mkdir -p "$log_dir"
snapshot() { for f in "$log_dir"/*.out; do [ -f "$f" ] && printf '%s:%s\n' "$f" "$(wc -l <"$f")"; done 2>/dev/null || true; }
before="$(snapshot)"

submit_rc=0
"$sql_client" -f "$sql" >"$submit_log" 2>&1 || submit_rc=$?

# Checks 2 and 3 — follow the submitted job(s) to a terminal state and
# compare the plan against the mode. The sql client may return before the
# job finishes; the REST wait is what decides FINISHED.
wait_args=(--rest "$rest_url" --before "$jobs_before" --mode "$mode"
           --flink-home "$FLINK_HOME" --out "$job_json"
           --timeout "${FLINK_JOB_TIMEOUT:-600}" --poll "${FLINK_JOB_POLL:-1}")
# the job.json sidecar refreshes in place with the capture (validated above);
# give flink_job.py the same overwrite consent when it already exists or
# --overwrite was passed
if [ "$OVERWRITE" = 1 ] || [ -f "$job_json" ]; then
  wait_args+=(--overwrite)
fi
if [ "${RUN_SQL_SKIP_PLAN_CHECK:-0}" = "1" ]; then
  wait_args+=(--skip-plan-check)
fi
wait_rc=0
python3 "$job_tool" wait "${wait_args[@]}" || wait_rc=$?

if [ "$wait_rc" -eq 2 ]; then
  echo "[$mode] side NOT complete: REST $rest_url unreachable AFTER submission — job state unknown" >&2
  echo "  find the job on the WebUI first; do not restart the cluster while its state is unknown" >&2
  exit 2
fi
if [ "$wait_rc" -eq 3 ]; then
  echo "[$mode] side NOT complete: job still not FINISHED after ${FLINK_JOB_TIMEOUT:-600}s — it may still be RUNNING" >&2
  echo "  cancel it or keep waiting, then RERUN this side; do not restart the cluster over a running job" >&2
  exit 3
fi
if [ "$submit_rc" -eq 126 ] || [ "$submit_rc" -eq 127 ]; then
  echo "[$mode] side NOT complete: sql client did not start (exit $submit_rc — command missing or not executable, or a submit-log redirect problem); see $sql_client and $submit_log" >&2
  exit 1
fi
if [ "$submit_rc" -ne 0 ]; then
  echo "[$mode] side NOT complete: sql client started but exited $submit_rc — see $submit_log" >&2
  exit 1
fi
if [ "$wait_rc" -ne 0 ]; then
  echo "[$mode] side NOT complete (exit $wait_rc) — see the messages above and $job_json" >&2
  echo "  fix and rerun this side; do not switch the cluster away from a failed side" >&2
  exit "$wait_rc"
fi

: >"$out"
# appended lines of pre-existing files, then whole new files
while IFS=: read -r f n; do
  [ -f "$f" ] || continue
  awk -v n="$n" 'NR>n' "$f" >>"$out"
done <<<"$before"
for f in "$log_dir"/*.out; do
  [ -f "$f" ] || continue
  case "$before" in *"$f:"*) continue ;; esac
  cat "$f" >>"$out"
done 2>/dev/null || true

# keep only changelog rows, in arrival order; strip any subtask-id prefix
# ("3> +I[...]") so the fold downstream sees pure FLAG[payload] rows.
# -a keeps grep honest if the native stack ever writes non-text bytes into
# the .out logs. The scratch is created EXCLUSIVELY (mktemp) next to the
# capture — never a fixed name that a pre-existing file could occupy, and
# the trap cleans only the file this run created.
tmp="$(mktemp "$out.XXXXXX")" || {
  echo "cannot create the filter scratch next to $out" >&2
  exit 2
}
trap 'rm -f "$tmp"' EXIT
grep -a -E '^([0-9]+> )?[-+][IUD]\[' "$out" | sed -E 's/^[0-9]+> //' >"$tmp" || true
mv "$tmp" "$out"
trap - EXIT

# Check 4 — a FINISHED side that captured zero rows is NOT an equivalence
# pass: it is almost always a failed capture or a job that produced nothing.
rows=$(wc -l <"$out")
if [ "$rows" -eq 0 ]; then
  echo "[$mode] side NOT complete: 0 changelog rows captured despite FINISHED — an empty capture is not a pass" >&2
  echo "  (a case that genuinely produces no rows cannot be verified by this protocol); see $submit_log and $job_json" >&2
  exit 1
fi

echo "[$mode] $(basename "$sql"): captured $rows changelog rows -> $out"
echo "[$mode] side complete: job FINISHED, plan matches '$mode', capture on disk"
echo "[$mode] the cluster may be switched now if the next side needs the other build — a restart wipes $log_dir, the evidence is already in $out"
