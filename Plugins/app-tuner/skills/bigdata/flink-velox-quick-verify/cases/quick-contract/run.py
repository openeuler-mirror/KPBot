#!/usr/bin/env python3
"""quick-contract — tier-A case for flink-velox-quick-verify.

Drives scripts/quick_verify.py end to end against a mock Flink — a fake
$FLINK_HOME whose bin/sql-client.sh is a bash mock driven by MOCK_* env
vars, plus the file-backed fake REST server in mock_flink.py — with no
real cluster. The scenario library under test is the real one: the real
case.sql files, the real inputs, and the real reference.py models (the
reference implementations are pinned by hand-verified literals). Pins:

  - the five-step executor (gate / submit / capture / reference /
    compare): lib side detection, REST probe, {{INPUT_DIR}}
    substitution, FINISHED tracking, snapshot diff, prefix stripping,
    subprocess reference, multiset compare with -U/+U folds
  - the failure rules: empty capture, unterminated row, never-present
    retraction, GlutenFlink plan without gluten nodes (engine fields
    only — the mock's clean plan reads from a table named
    gluten_orders and must not count), NATIVE plan WITH gluten nodes,
    unreadable plan, a scenario missing case.sql/input/reference.py
    (by name, in plan and run — never silently dropped), a FINISHED
    record that cannot be written, a broken reference model
  - the leftover-job rule: a timed-out scenario cancels its job over
    REST and the rest still run; an unconfirmable cancel (REST gone or
    refusing) skips the remaining scenarios, the known job id on record.
    A hung sql client joins the same protocol: the jobs it submitted in
    the window are cancelled (none running counts as settled), and the
    same cancel/skip split applies
  - TM log rotation/truncation detected by file identity: rows are
    still captured from the top, the record notes the capture may be
    incomplete
  - "client did not start" (126/127) vs "started but exited non-zero";
    never-FINISHED fails within --timeout; the --yes gate; plan's
    zero-submit / zero-write contract
  - the --out guard (outside project / symlink / existing without
    --overwrite / fifo / link-dotdot escape / collision with an
    evidence path of a chosen scenario — all refused before anything
    runs) and the evidence guard (a pre-made symlink at the capture
    path fails the scenario instead of writing through)
  - the result contract: the same JSON key set on every path, including
    the tool-abort state (poisoned /jobs/overview)
  - field-level attribution: per-column mismatch counts with samples
    (the dow mutation replays the historic velox DayOfWeek bug:
    Wednesday 2026-07-22 printed 3 where Flink says 4); unpairable rows
    counted separately; the human report names the columns
  - the four v1 reference models pinned against hand-verified literals
    (year/ISO-week boundaries, Sun=1..Sat=7 DAYOFWEEK, fold
    counts/sums, filter boundaries, join pairs)

Writes results/result.json; exit 0 iff all tests pass.
"""
import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
QUICK = ROOT / "scripts" / "quick_verify.py"
SCENARIOS = ROOT / "scenarios"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

sys.path.insert(0, str(CASE_DIR))
import mock_flink  # noqa: E402  (tests drive quick_verify.py via subprocess)

# loaded for the hung-client tests: the subprocess timeout window itself
# (max(--timeout + 60, 120)s) is stdlib behavior — what those tests patch is
# the client call so run_scenario's TimeoutExpired branch runs in-process
# against the fake REST; the e2e paths stay subprocess-driven.
_quick_spec = importlib.util.spec_from_file_location("quick_verify", QUICK)
quick_verify = importlib.util.module_from_spec(_quick_spec)
_quick_spec.loader.exec_module(quick_verify)

RESULT_KEYS = {'success', 'checked_at', 'command', 'side', 'flink_home', 'rest',
               'evidence_dir', 'timeout', 'scenarios', 'next_step', 'error_message'}
SCENARIO_KEYS = {'name', 'status', 'side', 'rows_expected', 'rows_captured',
                 'column_diffs', 'unpaired_missing', 'unpaired_extra',
                 'capture', 'submit_log', 'gluten_nodes', 'detail'}

PINNED = {
    'datetime-expr': [
        "+I[1001, 2026, 7, 22, 203, 4, 3, 2, 6, 8, 30]",
        "+I[1002, 2026, 1, 1, 1, 5, 1, 0, 0, 0, 1]",
        "+I[1003, 2025, 12, 28, 362, 1, 4, 12, 34, 56, 52]",
        "+I[1004, 2027, 1, 1, 1, 6, 1, 9, 15, 22, 53]",
        "+I[1005, 2026, 3, 31, 90, 3, 1, 23, 59, 59, 14]",
        "+I[1006, 2026, 4, 1, 91, 4, 2, 13, 0, 0, 14]",
        "+I[1007, 2026, 10, 31, 304, 7, 4, 18, 45, 7, 44]",
        "+I[1008, 2026, 7, 23, 204, 5, 3, 2, 6, 9, 30]",
    ],
    'agg-changelog': ["+I[alice, 3, 21]", "+I[bob, 2, 12]",
                      "+I[carol, 2, 10]", "+I[dave, 1, 2]"],
    'filter-project': ["+I[2001, widget, 750, 6]", "+I[2003, gizmo, 660, 4]",
                       "+I[2006, gizmo, 202, 3]", "+I[2007, thing, 600, 3]"],
    'two-way-join': ["+I[1, alpha, s-one]", "+I[1, alpha, s-two]",
                     "+I[1, bravo, s-one]", "+I[1, bravo, s-two]",
                     "+I[2, charlie, s-three]"],
}

MOCK_CLIENT = textwrap.dedent('''\
    #!/usr/bin/env bash
    # mock sql-client.sh — stands in for the real client; everything is
    # driven by MOCK_* env vars (see run.py). The rows file is per scenario:
    # its name comes from the submitted SQL file (<scenario>.submit.sql).
    set -u
    if [ -n "${MOCK_SUBMIT_MARKER:-}" ]; then : >"$MOCK_SUBMIT_MARKER"; fi
    if [ -n "${MOCK_ARGS_FILE:-}" ]; then printf '%s\\n' "$*" >"$MOCK_ARGS_FILE"; fi
    stem="$(basename "${@: -1}")"
    stem="${stem%.submit.sql}"
    state="${MOCK_JOB_STATE:-FINISHED}"
    for kv in ${MOCK_STATE_MAP:-}; do [ "${kv%%=*}" = "$stem" ] && state="${kv#*=}"; done
    if [ "${MOCK_ADD_JOB:-1}" = "1" ]; then
      jid="$(python3 "$MOCK_HELPER" add-job "$MOCK_STATE" "$stem" \\
              "$state" "${MOCK_GLUTEN:-0}" "${MOCK_EXCEPTION:-}")"
      if [ -n "${MOCK_JID_FILE:-}" ]; then printf '%s' "$jid" >"$MOCK_JID_FILE"; fi
    fi
    [ -z "${MOCK_POISON:-}" ] || python3 "$MOCK_HELPER" poison "$MOCK_STATE"
    [ -z "${MOCK_DEAD_AFTER_ADD:-}" ] || python3 "$MOCK_HELPER" dead "$MOCK_STATE"
    [ -z "${MOCK_CANCEL_REFUSED:-}" ] || python3 "$MOCK_HELPER" refuse-cancel "$MOCK_STATE"
    if [ -n "${MOCK_JOBJSON_SYMLINK:-}" ]; then
      mkdir -p e2e/quick && ln -sfn "$MOCK_JOBJSON_SYMLINK" "e2e/quick/$stem.job.json"
    fi
    rows_file="${MOCK_ROWS_DIR:-}/$stem.rows"
    if [ -s "$rows_file" ]; then
      IFS=: read -ra targets <<<"${MOCK_TM_OUT:?MOCK_TM_OUT required with MOCK_ROWS_DIR}"
      for t in "${targets[@]}"; do
        mkdir -p "$(dirname "$t")"
        if [ -n "${MOCK_ROTATE:-}" ]; then mv -f "$t" "$t.1"; fi
        if [ -n "${MOCK_TRUNCATE:-}" ]; then : >"$t"; fi
        while IFS= read -r line; do
          [ -n "$line" ] || continue
          out=""
          if [ -n "${MOCK_LOG4J_PREFIX:-}" ]; then
            out="2026-11-26 10:00:00,123 INFO  org.apache.flink.PrintSink  - "
          fi
          if [ -n "${MOCK_SUBTASK_PREFIX:-}" ]; then out="${out}3> "; fi
          printf '%s\\n' "$out$line" >>"$t"
        done <"$rows_file"
      done
    fi
    exit "${MOCK_RC:-0}"
    ''')


class QuickTestBase(unittest.TestCase):
    """One fake REST server + one fake $FLINK_HOME per test."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.state_path = self.workdir / "state.json"
        mock_flink.save_state(str(self.state_path), mock_flink.empty_state())
        self.server = mock_flink.serve(str(self.state_path))
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        self.marker = self.workdir / "submit.marker"
        self.args_file = self.workdir / "client.args"
        self.rows_dir = self.workdir / "rows"
        self.rows_dir.mkdir()
        self.evidence = self.workdir / "e2e" / "quick"

    # ---- fake cluster -------------------------------------------------
    def make_flink_home(self, gluten_jars=False, pre_rows=""):
        home = self.workdir / "flink"
        for sub in ("bin", "conf", "lib", "log"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        if gluten_jars:
            (home / "lib" / "velox4j-0.1.0-SNAPSHOT.jar").write_text("")
            (home / "lib" / "gluten-flink-runtime-1.19.0.jar").write_text("")
        (home / "conf" / "config.yaml").write_text(
            f"jobmanager.rpc.address: localhost\n"
            f"rest:\n  address: 127.0.0.1\n  port: {self.port}\n")
        client = home / "bin" / "sql-client.sh"
        client.write_text(MOCK_CLIENT)
        client.chmod(0o755)
        self.tm_out = home / "log" / "flink-root-taskexecutor-1-host.out"
        self.tm_out.write_text(pre_rows)
        (home / "log" / "flink-root-standalonesession-0-host.out").write_text("")
        return home

    # ---- driving quick_verify.py --------------------------------------
    def tool_env(self, home=None, **extra):
        env = dict(os.environ)
        env.pop("FLINK_REST_URL", None)
        env["FLINK_HOME"] = str(home or self.flink_home)
        env["MOCK_HELPER"] = str(CASE_DIR / "mock_flink.py")
        env["MOCK_STATE"] = str(self.state_path)
        env["MOCK_SUBMIT_MARKER"] = str(self.marker)
        env["MOCK_ARGS_FILE"] = str(self.args_file)
        env["MOCK_ROWS_DIR"] = str(self.rows_dir)
        env["MOCK_TM_OUT"] = str(self.tm_out)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run_tool(self, argv, home=None, json_mode=True, extra_env=None):
        cmd = [sys.executable, str(QUICK)] + argv
        if json_mode:
            cmd.append("--json")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              check=False, timeout=180,
                              env=self.tool_env(home, **(extra_env or {})),
                              cwd=str(self.workdir))
        return proc

    def result(self, proc):
        return json.loads(proc.stdout)

    def run_scenarios(self, *names, home=None, extra_env=None):
        argv = ["run", "--yes"]
        if names:
            argv += ["--scenario", names[0]]
            for n in names[1:]:
                argv += ["--scenario", n]
        else:
            argv.append("--all")
        argv += ["--timeout", "3"]
        return self.run_tool(argv, home=home, extra_env=extra_env)

    # ---- rows ----------------------------------------------------------
    def reference_rows(self, scenario):
        """The REAL reference model's output — the ground truth itself."""
        proc = subprocess.run(
            [sys.executable, str(SCENARIOS / scenario / "reference.py"),
             "expected", str(SCENARIOS / scenario / "input")],
            capture_output=True, text=True, check=True, timeout=60)
        return json.loads(proc.stdout)

    def write_rows(self, scenario, lines):
        (self.rows_dir / f"{scenario}.rows").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def pass_rows(self, scenario, prefix=False):
        self.write_rows(scenario, self.reference_rows(scenario))
        return {"MOCK_LOG4J_PREFIX": "1", "MOCK_SUBTASK_PREFIX": "1"} if prefix else {}

    def combined(self, proc):
        return (proc.stdout or "") + (proc.stderr or "")

    def scenario_rec(self, result, name):
        """result is the already-unwrapped flink_velox_quick_verify_result."""
        return next(s for s in result["scenarios"] if s["name"] == name)

    def mutate_column(self, row, index, value):
        """Rebuild one +I[...] row with column <index> replaced."""
        flag, payload = row[:3], row[3:-1]
        cells = payload.split(", ")
        cells[index] = str(value)
        return flag + ", ".join(cells) + "]"

    def hide_file(self, path):
        """Temporarily rename a shipped scenario file away (restored on
        cleanup) — an incomplete scenario must fail, not vanish."""
        hidden = Path(str(path) + ".hidden")
        path.rename(hidden)
        self.addCleanup(hidden.rename, path)


class ConsentAndPlanTests(QuickTestBase):
    """plan submits and writes nothing; run needs --yes; prerequisites
    refuse before anything is submitted."""

    def test_plan_submits_and_writes_nothing(self):
        self.flink_home = self.make_flink_home()
        proc = self.run_tool(["plan", "--all"], json_mode=False)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.evidence.exists())  # not even created
        self.assertIn("PLAN: 4 scenario(s)", proc.stdout)
        self.assertIn("run --yes", proc.stdout)  # the copy-paste command
        result = json.loads(self.run_tool(["plan", "--all"]).stdout)
        r = result["flink_velox_quick_verify_result"]
        self.assertTrue(r["success"])
        self.assertEqual(r["side"], "native")
        self.assertEqual([s["status"] for s in r["scenarios"]],
                         ["skipped"] * 4)
        self.assertIn("planned", r["scenarios"][0]["detail"])

    def test_run_without_yes_refused(self):
        self.flink_home = self.make_flink_home()
        proc = self.run_tool(["run", "--all", "--timeout", "3"])
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(result["success"])
        self.assertIn("--yes", result["error_message"])
        self.assertFalse(self.marker.exists())
        for s in result["scenarios"]:
            self.assertEqual(s["status"], "skipped")

    def test_rest_unreachable_refused_before_submit(self):
        self.flink_home = self.make_flink_home()
        (self.flink_home / "conf" / "config.yaml").write_text(
            "rest:\n  address: 127.0.0.1\n  port: 1\n")
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not answer", result["error_message"])
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.scenario_rec(result, "filter-project")["status"],
                         "skipped")

    def test_missing_sql_client_refused_before_submit(self):
        self.flink_home = self.make_flink_home()
        (self.flink_home / "bin" / "sql-client.sh").unlink()
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertIn("sql client not found or not executable",
                      result["error_message"])
        self.assertFalse(self.marker.exists())

    def test_nonexecutable_sql_client_refused_before_submit(self):
        self.flink_home = self.make_flink_home()
        (self.flink_home / "bin" / "sql-client.sh").chmod(0o644)
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not executable", result["error_message"])
        self.assertFalse(self.marker.exists())


class HappyPathTests(QuickTestBase):
    """A scenario passes only when the stub capture folds to exactly the
    real reference model's rows."""

    def test_single_scenario_pass_with_prefixed_rows(self):
        self.flink_home = self.make_flink_home()
        extra = self.pass_rows("filter-project", prefix=True)
        proc = self.run_scenarios("filter-project", extra_env=extra)
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertTrue(result["success"])
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(rec["status"], "pass")
        self.assertEqual(rec["side"], "native")
        self.assertEqual(rec["gluten_nodes"], 0)  # clean plan read on the native side
        self.assertEqual(rec["rows_expected"], 4)
        self.assertEqual(rec["rows_captured"], 4)
        self.assertEqual(rec["column_diffs"], [])
        # the prefixes (log4j timestamp + "3> " subtask) were stripped
        self.assertEqual(
            (self.evidence / "filter-project.out").read_text(),
            "".join(r + "\n" for r in self.reference_rows("filter-project")))
        self.assertTrue((self.evidence / "filter-project.submit.sql").exists())
        self.assertTrue((self.evidence / "filter-project.submit.log").exists())
        self.assertTrue((self.evidence / "filter-project.job.json").exists())

    def test_all_scenarios_pass_with_retraction_chains(self):
        self.flink_home = self.make_flink_home()
        # agg-changelog goes through the changelog fold: an -U/+U chain per
        # final row must reconstruct exactly the reference multiset
        chain = []
        for row in self.reference_rows("agg-changelog"):
            chain += [row, row.replace("+I", "-U", 1), row.replace("+I", "+U", 1)]
        self.write_rows("agg-changelog", chain)
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            self.pass_rows(name)
        proc = self.run_scenarios()
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual([s["status"] for s in result["scenarios"]],
                         ["pass"] * 4)
        agg = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(agg["rows_captured"], 4)
        self.assertIn("-U=4", agg["detail"])
        self.assertIn("+U=4", agg["detail"])

    def test_gluten_side_pass_records_nodes(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project",
                                  extra_env={"MOCK_GLUTEN": "2"})
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(result["side"], "GlutenFlink")
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(rec["status"], "pass")
        self.assertEqual(rec["gluten_nodes"], 2)


class FailureRulesTests(QuickTestBase):
    """Empty capture, capture error, fold error, plan fallback, submit
    failures and timeouts all fail the scenario — none of them passes."""

    def test_field_attribution_on_column_mismatch(self):
        self.flink_home = self.make_flink_home()
        # replay the historic velox DayOfWeek bug: Wednesday 2026-07-22
        # printed 3 where Flink says 4, and 2026-07-23 (Thursday) 4 vs 5
        rows = self.reference_rows("datetime-expr")
        rows = [self.mutate_column(r, 5, int(r[3:-1].split(", ")[5]) - 1)
                 if r.startswith("+I[1001,") or r.startswith("+I[1008,") else r
                 for r in rows]
        self.write_rows("datetime-expr", rows)
        proc = self.run_scenarios("datetime-expr")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 1)
        rec = self.scenario_rec(result, "datetime-expr")
        self.assertEqual(rec["status"], "fail")
        self.assertEqual(rec["rows_expected"], 8)
        self.assertEqual(rec["rows_captured"], 8)
        self.assertEqual(rec["unpaired_missing"], 0)
        self.assertEqual(rec["unpaired_extra"], 0)
        dow = next(d for d in rec["column_diffs"] if d["column"] == "dow")
        self.assertEqual(dow["mismatches"], 2)
        self.assertEqual(dow["samples"][0], {"expected": "4", "actual": "3"})
        self.assertTrue(all(d["column"] != "id" for d in rec["column_diffs"]))

    def test_unpaired_rows_counted_separately(self):
        self.flink_home = self.make_flink_home()
        rows = [r for r in self.reference_rows("filter-project")
                if not r.startswith("+I[2001,")]
        rows.append("+I[9999, ghost, 1, 1]")
        self.write_rows("filter-project", rows)
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(rec["status"], "fail")
        self.assertEqual(rec["rows_expected"], 4)
        self.assertEqual(rec["rows_captured"], 4)
        self.assertEqual(rec["unpaired_missing"], 1)
        self.assertEqual(rec["unpaired_extra"], 1)
        self.assertEqual(rec["column_diffs"], [])

    def test_extra_rows_fail_even_when_expected_is_covered(self):
        self.flink_home = self.make_flink_home()
        # every expected row IS captured, plus one extra: a superset is
        # not a match — exact multiset equality is the standard
        self.write_rows("filter-project",
                        self.reference_rows("filter-project")
                        + ["+I[9999, ghost, 1, 1]"])
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(rec["status"], "fail")
        self.assertEqual(rec["rows_expected"], 4)
        self.assertEqual(rec["rows_captured"], 5)
        self.assertEqual(rec["unpaired_missing"], 0)
        self.assertEqual(rec["unpaired_extra"], 1)

    def test_all_mixed_pass_and_fail(self):
        self.flink_home = self.make_flink_home()
        wrong = [self.mutate_column(r, 9, 0) if r.startswith("+I[1003,") else r
                 for r in self.reference_rows("datetime-expr")]
        self.write_rows("datetime-expr", wrong)
        for name in ("agg-changelog", "filter-project", "two-way-join"):
            self.pass_rows(name)
        proc = self.run_scenarios()
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(result["success"])
        by_name = {s["name"]: s["status"] for s in result["scenarios"]}
        self.assertEqual(by_name, {"agg-changelog": "pass",
                                   "datetime-expr": "fail",
                                   "filter-project": "pass",
                                   "two-way-join": "pass"})
        self.assertIn("datetime-expr", result["error_message"])
        self.assertIn("fix the failed scenario(s) datetime-expr",
                      result["next_step"])

    def test_gluten_side_plan_fallback_fails(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project",
                                  extra_env={"MOCK_GLUTEN": "0"})
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 1)
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(rec["status"], "fail")
        self.assertEqual(rec["gluten_nodes"], 0)
        self.assertIn("no gluten nodes in the plan", rec["detail"])
        # the mock's clean nodes read from a table named gluten_orders —
        # description text must not have counted them as gluten nodes
        st = mock_flink.load_state(str(self.state_path))
        jid = next(iter(st["plans"]))
        self.assertIn("gluten_orders",
                      st["plans"][jid]["nodes"][0]["description"])

    def test_empty_capture_fails(self):
        self.flink_home = self.make_flink_home()
        proc = self.run_scenarios("filter-project")  # no rows file at all
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 1)
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(rec["status"], "fail")
        self.assertEqual(rec["rows_captured"], 0)
        self.assertIn("empty capture", rec["detail"])
        self.assertIn("not a pass", rec["detail"])

    def test_capture_error_fails(self):
        self.flink_home = self.make_flink_home()
        self.write_rows("filter-project", ["+I[2001, widget, 750, 6",
                                           "+I[2003, broken"])
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("never terminates", rec["detail"])

    def test_fold_error_fails(self):
        self.flink_home = self.make_flink_home()
        self.write_rows("agg-changelog", ["+I[alice, 3, 21]",
                                          "-U[alice, 1, 5]"])
        proc = self.run_scenarios("agg-changelog")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("fold error", rec["detail"])
        self.assertIn("never present", rec["detail"])

    def test_sql_client_not_started_is_reported_as_not_started(self):
        self.flink_home = self.make_flink_home()
        proc = self.run_scenarios("filter-project",
                                  extra_env={"MOCK_ADD_JOB": "0", "MOCK_RC": "127"})
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("did not start", rec["detail"])
        self.assertIn("127", rec["detail"])
        self.assertNotIn("started but exited", rec["detail"])

    def test_sql_client_started_but_nonzero_is_reported_as_such(self):
        self.flink_home = self.make_flink_home()
        proc = self.run_scenarios("filter-project",
                                  extra_env={"MOCK_ADD_JOB": "0", "MOCK_RC": "2"})
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("started but exited 2", rec["detail"])
        self.assertNotIn("did not start", rec["detail"])

    def test_job_never_finishing_times_out(self):
        self.flink_home = self.make_flink_home()
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project",
                                   extra_env={"MOCK_JOB_STATE": "RUNNING"})
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("did not reach a terminal state", rec["detail"])
        self.assertIn("do not restart the cluster", rec["detail"])
        self.assertTrue(self.marker.exists())  # it really was submitted

    def test_broken_reference_fails_as_a_reference_failure(self):
        self.flink_home = self.make_flink_home()
        # valid rows first, so the capture is fine and only the reference
        # is broken: a scenario whose reference model returns a non-+I row
        # must fail the scenario AND say whose failure it is
        self.pass_rows("filter-project")
        bad = SCENARIOS / "filter-project" / "reference.py"
        original = bad.read_text(encoding="utf-8")
        bad.write_text(original.replace("'+I[%s, %s, %d, %d]'",
                                        "'X[%s, %s, %d, %d]'"),
                      encoding="utf-8")
        try:
            proc = self.run_scenarios("filter-project")
        finally:
            bad.write_text(original, encoding="utf-8")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("reference model broken", rec["detail"])
        self.assertIn("not of the cluster side", rec["detail"])

    def test_symlinked_capture_path_refused(self):
        self.flink_home = self.make_flink_home()
        self.pass_rows("filter-project")
        self.evidence.mkdir(parents=True)
        outside = self.workdir / "outside.txt"
        outside.write_text("precious\n")
        (self.evidence / "filter-project.out").symlink_to(outside)
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        rec = self.scenario_rec(result, "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(rec["status"], "fail")
        self.assertIn("symlink", rec["detail"])
        self.assertEqual(outside.read_text(), "precious\n")  # not written through


class OutGuardTests(QuickTestBase):
    """The --out guard refuses before anything runs (no submission)."""

    def out_run(self, out, overwrite=False, existing=None):
        self.flink_home = self.make_flink_home()
        if existing is not None:
            (self.workdir / existing).write_text("keep me", encoding="utf-8")
        argv = ["run", "--yes", "--scenario", "filter-project",
                "--timeout", "3", "--out", out]
        if overwrite:
            argv.append("--overwrite")
        proc = self.run_tool(argv)
        return proc

    def test_out_outside_project_refused(self):
        proc = self.out_run("../outside.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project directory", proc.stderr)
        self.assertFalse(self.marker.exists())

    def test_out_symlink_refused(self):
        real = self.workdir / "real.json"
        real.write_text("x", encoding="utf-8")
        (self.workdir / "link.json").symlink_to(real)
        proc = self.out_run("link.json", overwrite=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project directory", proc.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(real.read_text(encoding="utf-8"), "x")

    def test_existing_out_needs_overwrite(self):
        self.pass_rows("filter-project")
        taken = self.workdir / "taken.json"
        taken.write_text("keep me", encoding="utf-8")
        proc = self.out_run("taken.json", existing="taken.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(taken.read_text(encoding="utf-8"), "keep me")
        # --overwrite lets the same run proceed and write the result
        proc = self.out_run("taken.json", overwrite=True, existing="taken.json")
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        written = json.loads(taken.read_text(encoding="utf-8"))
        self.assertTrue(written["flink_velox_quick_verify_result"]["success"])

    def test_fifo_out_refused_even_with_overwrite(self):
        os.mkfifo(str(self.workdir / "fifo.json"))
        proc = self.out_run("fifo.json", overwrite=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a regular file", proc.stderr)
        self.assertFalse(self.marker.exists())

    def test_out_link_dotdot_escape_refused(self):
        # a lexical fold sees link/../x as <cwd>/x and would pass, but the
        # kernel resolves the link first: the write would land in the link
        # target's parent (reviewer's minimal repro). The link points at a
        # dedicated directory outside the project, so that landing spot is
        # SCRATCH/<workdir>-escape.json exactly — and one level further out
        # is watched too: neither may be created or truncated.
        target = self.workdir.parent / (self.workdir.name + "-target")
        target.mkdir()
        escape = self.workdir.parent / (self.workdir.name + "-escape.json")
        further = self.workdir.parent.parent / (self.workdir.name + "-escape.json")
        (self.workdir / "link").symlink_to(target)
        proc = self.out_run("link/../%s" % escape.name, overwrite=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("never written through", proc.stderr)
        self.assertFalse(self.marker.exists())
        self.assertFalse(escape.exists())  # the kernel landing spot: no file
        self.assertFalse(further.exists())  # one level further out: none either

    def test_out_colliding_with_capture_refused(self):
        # the file does not exist yet: the scenario writes its capture
        # there first, the end-of-run report would then fail (or, with
        # --overwrite, destroy the capture) — refused up front
        self.pass_rows("filter-project")
        out = "e2e/quick/filter-project.out"
        for overwrite in (False, True):
            proc = self.out_run(out, overwrite=overwrite)
            self.assertEqual(proc.returncode, 2, self.combined(proc))
            self.assertIn("collides with an evidence path", proc.stderr)
        self.assertFalse(self.marker.exists())


class AbortAndContractTests(QuickTestBase):
    """The tool-abort state keeps the full key set, and so does every
    normal result — the JSON contract is stable across paths."""

    def test_tool_abort_has_the_full_key_set(self):
        self.flink_home = self.make_flink_home()
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project", extra_env={"MOCK_POISON": "1"})
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(set(result), RESULT_KEYS, result)
        self.assertFalse(result["success"])
        self.assertTrue(result["error_message"].startswith("tool aborted:"))
        self.assertEqual(result["scenarios"], [])
        self.assertTrue(result["next_step"])

    def test_result_contract_keys_on_pass_and_fail(self):
        self.flink_home = self.make_flink_home()
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project")
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual({k for s in result["scenarios"] for k in s},
                         SCENARIO_KEYS)
        # and on a refused run: same top-level keys, scenarios skipped
        proc = self.run_tool(["run", "--all", "--timeout", "3"])
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual({k for s in result["scenarios"] for k in s},
                         SCENARIO_KEYS)

    def test_human_report_renders_attribution(self):
        self.flink_home = self.make_flink_home()
        rows = [self.mutate_column(r, 5, 3) if r.startswith("+I[1001,") else r
                for r in self.reference_rows("datetime-expr")]
        self.write_rows("datetime-expr", rows)
        proc = self.run_tool(["run", "--yes", "--scenario", "datetime-expr",
                              "--timeout", "3"], json_mode=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL     datetime-expr", proc.stdout)
        self.assertIn("column dow", proc.stdout)
        self.assertIn("expected=4 actual=3", proc.stdout)
        self.assertIn("QUICK VERIFY on the native side: 0/1 scenario(s) PASS",
                     proc.stdout)
        self.assertIn("next: ", proc.stdout)


class SubstitutionTests(QuickTestBase):
    """{{INPUT_DIR}} is replaced with the scenario input directory before
    the client ever sees the SQL."""

    def test_input_dir_substituted_in_submitted_sql(self):
        self.flink_home = self.make_flink_home()
        self.pass_rows("two-way-join")
        proc = self.run_scenarios("two-way-join")
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        args = self.args_file.read_text().split()
        submitted = Path(args[-1])  # "sql-client.sh -f <path>"
        self.assertTrue(str(submitted).endswith("two-way-join.submit.sql"))
        text = submitted.read_text(encoding="utf-8")
        input_dir = str(SCENARIOS / "two-way-join" / "input")
        self.assertNotIn("{{INPUT_DIR}}", text)
        self.assertIn("file://%s/orders" % input_dir, text)
        self.assertIn("file://%s/shipments" % input_dir, text)


class SymmetryAndEvidenceTests(QuickTestBase):
    """The plan gate is symmetric (native reads the plan too); an evidence
    write that cannot land fails the scenario."""

    def test_native_side_gluten_plan_fails(self):
        # lib says native (no gluten jars) but the plan carries gluten
        # nodes: the cluster still runs the GlutenFlink planner — a native
        # PASS here would be a lie (removing jars is not a stack switch)
        self.flink_home = self.make_flink_home()
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project",
                                  extra_env={"MOCK_GLUTEN": "2"})
        rec = self.scenario_rec(self.result(proc)["flink_velox_quick_verify_result"],
                                "filter-project")
        self.assertEqual(rec["status"], "fail")
        self.assertEqual(rec["side"], "native")
        self.assertEqual(rec["gluten_nodes"], 2)  # read on the native side too
        self.assertIn("cluster still runs the GlutenFlink planner", rec["detail"])
        self.assertIn("not a stack switch", rec["detail"])

    def test_jobjson_write_failure_fails_scenario(self):
        # the mock symlinks <scenario>.job.json outside the project
        # mid-run: the pre-submit check passed, the FINISHED record cannot
        # be written — a dropped error would leave a green scenario with
        # no evidence on disk
        self.flink_home = self.make_flink_home()
        self.pass_rows("filter-project")
        target = self.workdir.parent / (self.workdir.name + "-outside.json")
        proc = self.run_scenarios("filter-project",
                                  extra_env={"MOCK_JOBJSON_SYMLINK": str(target)})
        rec = self.scenario_rec(self.result(proc)["flink_velox_quick_verify_result"],
                                "filter-project")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(rec["status"], "fail")
        self.assertIn(".job.json", rec["detail"])
        self.assertIn("could not be written", rec["detail"])
        self.assertFalse(target.exists())  # never written through


class CaptureRotationTests(QuickTestBase):
    """A TM .out replaced or truncated mid-run is detected by file
    identity, read from the top, and noted — no silent line loss."""

    def _rotated_run(self, extra, pre_rows):
        self.flink_home = self.make_flink_home(pre_rows=pre_rows)
        self.pass_rows("filter-project")
        proc = self.run_scenarios("filter-project", extra_env=extra)
        rec = self.scenario_rec(self.result(proc)["flink_velox_quick_verify_result"],
                                "filter-project")
        self.assertEqual(rec["status"], "pass", rec["detail"])
        self.assertEqual(rec["rows_captured"], 4)  # the old line count lost no rows
        self.assertIn("capture may be incomplete", rec["detail"])

    def test_rotated_or_truncated_log_is_reread_with_a_warning(self):
        self._rotated_run({"MOCK_ROTATE": "1"}, "old noise\n")    # new inode
        self._rotated_run({"MOCK_TRUNCATE": "1"}, "a\nb\n")        # shrunk, rewritten


class CompletenessTests(QuickTestBase):
    """A scenario missing a component fails by name in plan and run — it
    never silently vanishes; a zero-scenario all-pass is impossible."""

    def test_run_incomplete_scenarios_fail_by_name(self):
        self.flink_home = self.make_flink_home()
        self.hide_file(SCENARIOS / "datetime-expr" / "case.sql")
        self.hide_file(SCENARIOS / "two-way-join" / "reference.py")
        self.pass_rows("filter-project")
        proc = self.run_scenarios()
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 1)
        dt = self.scenario_rec(result, "datetime-expr")
        self.assertEqual(dt["status"], "fail")
        self.assertIn("case.sql is missing", dt["detail"])
        join = self.scenario_rec(result, "two-way-join")
        self.assertEqual(join["status"], "fail")
        self.assertIn("reference.py is missing", join["detail"])
        self.assertFalse((self.evidence / "two-way-join.submit.sql").exists())
        # the complete scenarios still run and pass — the failure is per scenario
        self.assertEqual(self.scenario_rec(result, "filter-project")["status"],
                         "pass")

    def test_plan_marks_incomplete_scenario_failed(self):
        self.flink_home = self.make_flink_home()
        self.hide_file(SCENARIOS / "agg-changelog" / "reference.py")
        proc = self.run_tool(["plan", "--all"])
        result = self.result(proc)["flink_velox_quick_verify_result"]
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(result["success"])
        rec = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(rec["status"], "fail")
        self.assertIn("reference.py is missing", rec["detail"])
        self.assertEqual(self.scenario_rec(result, "datetime-expr")["status"],
                         "skipped")
        self.assertIn("incomplete scenario(s)", result["error_message"])


class LeftoverJobTests(QuickTestBase):
    """A timed-out scenario cancels its leftover over REST and the rest
    still run; an unconfirmable cancel skips the rest with the job id."""

    def _run_with_stuck_agg(self, **extra):
        self.flink_home = self.make_flink_home()
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            self.pass_rows(name)
        # agg-changelog (first alphabetically) never finishes
        proc = self.run_scenarios(extra_env=dict(
            {"MOCK_STATE_MAP": "agg-changelog=RUNNING"}, **extra))
        result = self.result(proc)["flink_velox_quick_verify_result"]
        agg = self.scenario_rec(result, "agg-changelog")
        st = mock_flink.load_state(str(self.state_path))
        jid = next(j["jid"] for j in st["overview"] if j["name"] == "agg-changelog")
        return result, agg, st, jid

    def test_timeout_cancels_leftover_then_continues(self):
        result, agg, st, jid = self._run_with_stuck_agg()
        self.assertEqual(agg["status"], "fail")
        self.assertIn("did not reach a terminal state", agg["detail"])
        self.assertIn("cancelled over REST", agg["detail"])
        self.assertIn(jid, agg["detail"])  # the known job id stays on record
        self.assertEqual(self.scenario_rec(result, "filter-project")["status"],
                         "pass")
        self.assertTrue(all(j["state"] == "CANCELED" for j in st["overview"]
                            if j["name"] == "agg-changelog"))

    def test_uncancellable_leftover_skips_remaining_with_job_id(self):
        result, agg, st, jid = self._run_with_stuck_agg(MOCK_CANCEL_REFUSED="1")
        self.assertEqual(agg["status"], "fail")
        self.assertIn("could not cancel", agg["detail"])
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            rec = self.scenario_rec(result, name)
            self.assertEqual(rec["status"], "skipped", rec["detail"])
            self.assertIn("could not be confirmed", rec["detail"])
            self.assertIn(jid, rec["detail"])
        self.assertEqual(len(st["overview"]), 1)  # nothing else was submitted

    def test_rest_lost_after_submit_skips_remaining(self):
        self.flink_home = self.make_flink_home()
        # the REST dies right after the first job is registered: its state
        # is unknown, no id could be read, nothing else may be submitted
        result = self.result(self.run_scenarios(
            extra_env={"MOCK_DEAD_AFTER_ADD": "1"}))["flink_velox_quick_verify_result"]
        agg = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(agg["status"], "fail")
        self.assertIn("unreachable", agg["detail"])
        self.assertIn("no job id could be read", agg["detail"])
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            rec = self.scenario_rec(result, name)
            self.assertEqual(rec["status"], "skipped", rec["detail"])
            self.assertIn("id unknown", rec["detail"])


class ClientHangTests(QuickTestBase):
    """A sql client that never returns (the subprocess window expires) is a
    leftover-job case like a wait timeout: the jobs it submitted in the
    window are cancelled over REST and, once confirmed, the remaining
    scenarios still run. Nothing new running counts as settled — but an
    unreadable overview, or a cancel that cannot be confirmed, skips the
    rest (the review finding: this branch used to bypass the protocol)."""

    STEM_SUFFIX = ".submit.sql"

    def setUp(self):
        super().setUp()
        self.flink_home = self.make_flink_home()
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            self.pass_rows(name)
        # the in-process executor works with absolute evidence paths, which
        # the guard compares against the real cwd — be the project dir, like
        # the e2e tests' subprocess cwd
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, self.old_cwd)
        # the mock client runs as a real subprocess: it needs MOCK_* in the
        # inherited environment (run_tool would have passed them)
        env_patch = mock.patch.dict(os.environ, self.tool_env(self.flink_home))
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def run_all_with_hang(self, stems, register="running"):
        """run --all with the listed scenarios' clients hung; register says
        what the hung client left behind: "running" (a detached submit that
        reached the cluster), "nothing", or "then-dead" (the REST goes away
        while the window's jobs are being collected)."""
        names = ["agg-changelog", "datetime-expr", "filter-project",
                 "two-way-join"]
        opts = {"flink_home": str(self.flink_home), "scenarios": names,
                "all": True, "timeout": 3,
                "evidence_dir": str(self.workdir / "e2e" / "quick"),
                "explicit": {}}
        stems = set(stems)
        real_run = subprocess.run
        jids = []

        def hung_client(cmd, **kwargs):
            submitted = Path(cmd[-1]).name
            stem = submitted[:-len(self.STEM_SUFFIX)]
            if submitted.endswith(self.STEM_SUFFIX) and stem in stems:
                if register == "running":
                    jids.append(mock_flink.add_job(
                        str(self.state_path), stem, "RUNNING"))
                elif register == "then-dead":
                    mock_flink.add_job(str(self.state_path), stem, "RUNNING")
                    mock_flink.set_dead(str(self.state_path))
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            return real_run(cmd, **kwargs)

        with mock.patch.object(quick_verify.subprocess, "run", hung_client):
            findings, state = quick_verify.probe(opts, names)
            self.assertEqual(findings, [], [f["detail"] for f in findings])
            result, code = quick_verify.run_all(opts, True, names)
        return result["flink_velox_quick_verify_result"], code, jids

    def test_hung_client_cancels_window_jobs_then_rest_run(self):
        result, code, jids = self.run_all_with_hang({"agg-changelog"})
        agg = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(agg["status"], "fail")
        self.assertIn("did not return within", agg["detail"])
        self.assertIn("cancelled over REST", agg["detail"])
        self.assertIn(jids[0], agg["detail"])
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            self.assertEqual(self.scenario_rec(result, name)["status"], "pass")
        st = mock_flink.load_state(str(self.state_path))
        by_jid = {j["jid"]: j["state"] for j in st["overview"]}
        self.assertEqual(by_jid[jids[0]], "CANCELED")
        self.assertEqual(code, 1)  # the hung scenario itself failed

    def test_hung_client_with_no_new_job_counts_as_settled(self):
        result, code, jids = self.run_all_with_hang(
            {"agg-changelog"}, register="nothing")
        agg = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(agg["status"], "fail")
        self.assertIn("no job it submitted is still running over REST",
                      agg["detail"])
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            self.assertEqual(self.scenario_rec(result, name)["status"], "pass")
        st = mock_flink.load_state(str(self.state_path))
        self.assertEqual(len(st["overview"]), 3)  # only the pass scenarios'

    def test_uncancellable_hang_skips_remaining(self):
        mock_flink.refuse_cancel(str(self.state_path))
        result, code, jids = self.run_all_with_hang({"agg-changelog"})
        agg = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(agg["status"], "fail")
        self.assertIn("could not cancel", agg["detail"])
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            rec = self.scenario_rec(result, name)
            self.assertEqual(rec["status"], "skipped", rec["detail"])
            self.assertIn("could not be confirmed", rec["detail"])
            self.assertIn(jids[0], rec["detail"])

    def test_hang_while_rest_gone_skips_remaining(self):
        result, code, jids = self.run_all_with_hang(
            {"agg-changelog"}, register="then-dead")
        agg = self.scenario_rec(result, "agg-changelog")
        self.assertEqual(agg["status"], "fail")
        self.assertIn("could not be read", agg["detail"])
        for name in ("datetime-expr", "filter-project", "two-way-join"):
            rec = self.scenario_rec(result, name)
            self.assertEqual(rec["status"], "skipped", rec["detail"])
            self.assertIn("id unknown", rec["detail"])


class ReferencePinTests(unittest.TestCase):
    """The four v1 reference models pinned against hand-verified literals —
    the reference implementations themselves are under test. The hand
    checks behind each literal live in each scenario's README.md
    (2026-07-22 is a Wednesday → DAYOFWEEK 4, not velox's 3; 2025-12-28 a
    Sunday → 1; 2027-01-01 → ISO week 53 of 2026; filter price=100 out /
    qty=2 in; the join yields exactly the 5 same-key pairs)."""

    def reference_rows(self, scenario):
        proc = subprocess.run(
            [sys.executable, str(SCENARIOS / scenario / "reference.py"),
             "expected", str(SCENARIOS / scenario / "input")],
            capture_output=True, text=True, check=True, timeout=60)
        return json.loads(proc.stdout)

    def test_datetime_reference_pinned(self):
        self.assertEqual(self.reference_rows("datetime-expr"),
                         PINNED["datetime-expr"])

    def test_agg_reference_pinned(self):
        self.assertEqual(self.reference_rows("agg-changelog"),
                         PINNED["agg-changelog"])

    def test_filter_reference_pinned(self):
        self.assertEqual(self.reference_rows("filter-project"),
                         PINNED["filter-project"])

    def test_join_reference_pinned(self):
        self.assertEqual(self.reference_rows("two-way-join"),
                         PINNED["two-way-join"])


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    module = sys.modules[__name__]
    loader = unittest.defaultTestLoader
    names = sys.argv[1:]
    suite = (loader.loadTestsFromNames(names, module) if names
             else loader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "quick-contract",
        "skill": "flink-velox-quick-verify",
        "success": result.wasSuccessful(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }
    (RESULTS / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
