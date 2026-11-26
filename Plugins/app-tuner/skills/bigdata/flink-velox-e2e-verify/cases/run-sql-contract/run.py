#!/usr/bin/env python3
"""run-sql-contract — tier-A case for flink-velox-e2e-verify.

Drives scripts/run-sql.sh end to end against a mock Flink — a fake
$FLINK_HOME whose bin/sql-client.sh is a bash mock driven by MOCK_* env
vars, plus the file-backed fake REST server in mock_flink.py — with no real
cluster. Pins:

  - the four per-side gates: lib build gate, REST FINISHED wait, plan stack
    check (gfv needs gluten nodes, native needs none), non-empty capture
  - the plan stack check's gluten detection: only engine-generated fields
    count (operator, operator_strategy, inputs[].ship_strategy/exchange);
    "description" embeds user SQL text (the mock's clean plan even reads
    from a table named gluten_orders) and must never mark a node — while
    an operator name carrying gluten still counts
  - the pre-submit single-node guard: every TaskManager REST entry must be
    on this machine (localhost / 127.0.0.1 / this hostname); a remote TM
    refuses the run (exit 2) before anything is submitted
  - the exit-code contract: 0 side complete / 1 job, plan, rows or
    submission failure / 2 REST unreachable, out-file or capture-directory
    refusal, remote-TaskManager refusal / 3 timeout
  - the capture directory is created up front (a missing e2e/out must not
    masquerade as a client failure) and its scratch file is exclusively
    created (mktemp): a pre-existing $out.tmp is never truncated or moved
  - the "nothing gets submitted past a failed gate" and "side complete is
    the only switch signal" messages
  - the capture model: snapshot diff excludes pre-existing rows, new .out
    files are taken whole, subtask prefixes are stripped, duplicates kept
  - flink_job.py REST URL resolution (flag, env, both conf layouts,
    flink-conf.yaml winning when both exist, default)
  - the out-file guard shared by run-sql.sh and flink_job.py --out:
    outside the project / symlink / existing without --overwrite / fifo all
    refused (exit 2) before anything is submitted, the rerun contract
    (an existing capture needs --overwrite; sidecars refresh with it)
  - the tool-abort contract: an unexpected error in flink_job.py wait
    still writes the same top-level key set (observation slots empty,
    error_message "tool aborted: ...", a next_step fix hint), and run-sql.sh
    reports the side as not complete
  - REST calls bypass environment proxies (no-proxy opener): with
    http_proxy set, the pre-submit snapshot and the whole run-sql.sh path
    still reach the cluster directly — a localhost REST address is never
    handed to a proxy
  - the sql client is validated up front: a missing or non-executable
    $FLINK_HOME/bin/sql-client.sh fails fast (exit 1, nothing submitted,
    no submit-log redirect, no wait), and a submit failure distinguishes
    "client did not start" (126/127) from "client started but exited
    non-zero"

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
RUN_SQL = ROOT / "scripts" / "run-sql.sh"
FLINK_JOB = ROOT / "scripts" / "flink_job.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

sys.path.insert(0, str(CASE_DIR))
import mock_flink  # noqa: E402

_spec = importlib.util.spec_from_file_location("flink_job", FLINK_JOB)
flink_job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flink_job)

SQL_LINES = [
    "CREATE TABLE src (a STRING, b STRING) WITH ("
    "'connector'='filesystem','path'='file:///data','format'='csv');",
    "CREATE TABLE sink (a STRING, b STRING) WITH ('connector'='print');",
    "INSERT INTO sink SELECT a, b FROM src;",
]

WAIT_KEYS = {'rest', 'mode', 'jobs', 'all_finished', 'stack_check',
             'tm_velox4j_lines', 'verdict', 'error_message', 'next_step'}

MOCK_CLIENT = textwrap.dedent('''\
    #!/usr/bin/env bash
    # mock sql-client.sh — stands in for the real client; everything is
    # driven by MOCK_* env vars (see run.py). Emulates both real shapes:
    # return after the job finished (MOCK_JOB_STATE=FINISHED) or return
    # immediately while the job keeps running (MOCK_JOB_STATE=RUNNING).
    set -u
    if [ -n "${MOCK_SUBMIT_MARKER:-}" ]; then : >"$MOCK_SUBMIT_MARKER"; fi
    if [ "${MOCK_ADD_JOB:-1}" = "1" ]; then
      jid="$(python3 "$MOCK_HELPER" add-job "$MOCK_STATE" \\
              "$(basename "${2:-case}")" "${MOCK_JOB_STATE:-FINISHED}" \\
              "${MOCK_GLUTEN:-0}" "${MOCK_EXCEPTION:-}")"
      if [ -n "${MOCK_JID_FILE:-}" ]; then printf '%s' "$jid" >"$MOCK_JID_FILE"; fi
    fi
    if [ -n "${MOCK_POISON:-}" ]; then
      python3 "$MOCK_HELPER" poison "$MOCK_STATE"
    fi
    if [ -s "${MOCK_ROWS_FILE:-/dev/null}" ]; then
      IFS=: read -ra targets <<<"${MOCK_TM_OUT:?MOCK_TM_OUT required when MOCK_ROWS_FILE is set}"
      for t in "${targets[@]}"; do
        mkdir -p "$(dirname "$t")"
        if [ -n "${MOCK_SUBTASK_PREFIX:-}" ]; then
          sed 's/^/3> /' "$MOCK_ROWS_FILE" | tee -a "$t" >/dev/null
        else
          cat "$MOCK_ROWS_FILE" >>"$t"
        fi
      done
    fi
    exit "${MOCK_RC:-0}"
    ''')


class RunSqlTestBase(unittest.TestCase):
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
        self.jid_file = self.workdir / "jid"

    # ---- fake cluster -------------------------------------------------
    def make_flink_home(self, gluten_jars=True, pre_rows=""):
        home = self.workdir / "flink"
        for sub in ("bin", "conf", "lib", "log"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        if gluten_jars:
            (home / "lib" / "velox4j-0.1.0-SNAPSHOT.jar").write_text("")
            (home / "lib" /
             "gluten-flink-runtime-1.9.0-SNAPSHOT.jar").write_text("")
        # Flink >= 1.19 nested config.yaml layout (comments included on
        # purpose — the parser must tolerate them)
        (home / "conf" / "config.yaml").write_text(
            f"jobmanager.rpc.address: localhost\n"
            f"rest:\n  # the REST endpoint\n"
            f"  address: 127.0.0.1\n  port: {self.port}\n")
        client = home / "bin" / "sql-client.sh"
        client.write_text(MOCK_CLIENT)
        client.chmod(0o755)
        tm1 = home / "log" / "flink-root-taskexecutor-1-host.out"
        tm1.write_text(pre_rows)
        (home / "log" / "flink-root-standalonesession-0-host.out").write_text("")
        return home

    # ---- driving run-sql.sh -------------------------------------------
    def side_env(self, home=None, **extra):
        env = dict(os.environ)
        env.pop("FLINK_REST_URL", None)
        env["FLINK_HOME"] = str(home or self.flink_home)
        env["MOCK_HELPER"] = str(CASE_DIR / "mock_flink.py")
        env["MOCK_STATE"] = str(self.state_path)
        env["MOCK_SUBMIT_MARKER"] = str(self.marker)
        env["MOCK_JID_FILE"] = str(self.jid_file)
        env["FLINK_JOB_POLL"] = "0.05"
        env["FLINK_JOB_TIMEOUT"] = "3"
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def side_cmd(self, mode, sql_name="case.sql", out_name=None,
                 overwrite=False):
        sql = self.workdir / sql_name
        sql.write_text("\n".join(SQL_LINES) + "\n")
        out = self.workdir / (out_name or f"case.{mode}.out")
        argv = (["bash", str(RUN_SQL)] +
                (["--overwrite"] if overwrite else []) +
                [mode, str(sql), str(out)])
        return argv, out

    def run_side(self, mode, home=None, overwrite=False, out_name=None,
                 **extra):
        cmd, out = self.side_cmd(mode, out_name=out_name, overwrite=overwrite)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                               check=False, timeout=120,
                               env=self.side_env(home, **extra),
                               cwd=str(self.workdir))
        return proc, out

    def rows_file(self, lines):
        path = self.workdir / "rows.txt"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def job_json(self, out):
        return json.loads(
            (out.parent / (out.name[:-len(".out")] + ".job.json"))
            .read_text(encoding="utf-8"))

    def combined(self, proc):
        return (proc.stdout or "") + (proc.stderr or "")


class SideGateTests(RunSqlTestBase):
    """The four checks that gate a side, and their exit codes."""

    def test_gfv_happy_path(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(
                ["+I[a, b]", "+I[c]",
                 "Created work directory /tmp/velox4j-123.", "INFO: noise"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=2,
            MOCK_SUBTASK_PREFIX=1,  # rows land as "3> +I[...]"
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a, b]\n+I[c]\n")
        report = self.job_json(out)
        self.assertEqual(report["verdict"], "ok")
        self.assertEqual(report["stack_check"], "ok")
        self.assertTrue(report["all_finished"])
        self.assertEqual(report["jobs"][0]["gluten_nodes"], 2)
        self.assertEqual(report["tm_velox4j_lines"], 1)
        self.assertIn("side complete", self.combined(proc))
        self.assertIn("may be switched now", self.combined(proc))
        # submit sidecar exists; no timing sidecar (this skill does not time)
        self.assertTrue((self.workdir / "case.gfv.submit.log").exists())
        self.assertFalse((self.workdir / "case.gfv.elapsed_ms").exists())

    def test_native_happy_path(self):
        self.flink_home = self.make_flink_home(gluten_jars=False)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "native",
            MOCK_ROWS_FILE=self.rows_file(["+I[a, b]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=0,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a, b]\n")
        report = self.job_json(out)
        self.assertEqual(report["verdict"], "ok")
        self.assertEqual(report["jobs"][0]["gluten_nodes"], 0)

    def test_gfv_plan_without_gluten_nodes_fails(self):
        # jars in lib AND velox4j in the TM logs, but the plan fell back
        # to native operators — the diagnostic must say which case it is.
        self.flink_home = self.make_flink_home(
            gluten_jars=True,
            pre_rows="Created work directory /tmp/velox4j-99.\n")
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=0,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no gluten nodes", self.combined(proc))
        self.assertIn("SQL fell back to native operators",
                      self.combined(proc))
        self.assertIn("side NOT complete", self.combined(proc))
        self.assertEqual(self.job_json(out)["verdict"], "plan-mismatch")

    def test_native_plan_with_gluten_nodes_fails(self):
        # jars removed but the cluster never restarted: the GlutenFlink planner is
        # still active and the native "baseline" would be a fake one.
        self.flink_home = self.make_flink_home(gluten_jars=False)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "native",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=2,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("mentions gluten", self.combined(proc))
        self.assertIn("not enough, restart on the native build",
                      self.combined(proc))
        self.assertEqual(self.job_json(out)["verdict"], "plan-mismatch")

    def test_lib_gate_blocks_before_anything_is_submitted(self):
        # gfv side without GlutenFlink jars, and native side with them: both must be
        # blocked by the lib gate before the client runs (no marker file).
        self.flink_home = self.make_flink_home(gluten_jars=False)
        proc, _ = self.run_side("gfv")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no gluten/velox jars", self.combined(proc))
        self.assertFalse(self.marker.exists())

        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("native")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gluten/velox jar present in lib", self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_build_check_override_still_requires_the_other_gates(self):
        self.flink_home = self.make_flink_home(gluten_jars=False)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            RUN_SQL_SKIP_BUILD_CHECK=1,
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.job_json(out)["verdict"], "ok")

    def test_plan_check_override(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            RUN_SQL_SKIP_PLAN_CHECK=1,
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=0,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.job_json(out)["stack_check"], "skipped")

    def test_rest_unreachable_before_submit_fails_clean(self):
        self.flink_home = self.make_flink_home()
        # point the conf at a closed port: snapshot must fail BEFORE the
        # client runs, and nothing may have been submitted.
        (self.flink_home / "conf" / "config.yaml").write_text(
            "rest:\n  address: 127.0.0.1\n  port: 1\n")
        proc, _ = self.run_side("gfv")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unreachable", self.combined(proc))
        self.assertIn("nothing was submitted", self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_failed_job_fails_side(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
            MOCK_JOB_STATE="FAILED",
            MOCK_EXCEPTION="java.lang.RuntimeException: boom",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("did not finish", self.combined(proc))
        report = self.job_json(out)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["jobs"][0]["state"], "FAILED")
        self.assertIn("boom", report["jobs"][0]["exception"])
        self.assertIn("side NOT complete", self.combined(proc))

    def test_finished_but_zero_rows_fails(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, out = self.run_side("gfv", MOCK_GLUTEN=1)  # no rows file
        self.assertEqual(proc.returncode, 1)
        self.assertIn("0 changelog rows", self.combined(proc))
        self.assertIn("an empty capture is not a pass", self.combined(proc))
        self.assertEqual(self.job_json(out)["verdict"], "ok")

    def test_client_returning_early_is_caught_by_the_rest_wait(self):
        # sql-client returns while the job is still RUNNING; the REST wait
        # must follow it to FINISHED before the side can pass.
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        cmd, out = self.side_cmd("gfv")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.side_env(MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
                             MOCK_TM_OUT=tm1, MOCK_GLUTEN=1,
                             MOCK_JOB_STATE="RUNNING"),
            cwd=str(self.workdir))
        for _ in range(200):  # wait until the (early-returning) client ran
            if self.marker.exists() and self.jid_file.exists():
                break
            time.sleep(0.05)
        else:
            self.fail("mock client never ran")
        self.assertEqual(proc.poll(), None, "client must return before "
                                            "the job finishes")
        time.sleep(0.3)
        mock_flink.set_state(str(self.state_path),
                             self.jid_file.read_text().strip(), "FINISHED")
        stdout, stderr = proc.communicate(timeout=30)
        proc.stdout, proc.stderr = stdout, stderr
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")

    def test_never_finishing_job_times_out(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            FLINK_JOB_TIMEOUT=1,
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
            MOCK_JOB_STATE="RUNNING",
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("do not restart the cluster", self.combined(proc))
        self.assertIn("side NOT complete", self.combined(proc))
        self.assertEqual(self.job_json(out)["verdict"], "timeout")

    def test_submission_failure_without_job(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("gfv", MOCK_ADD_JOB=0, MOCK_RC=1,
                               FLINK_JOB_TIMEOUT=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no new job appeared", self.combined(proc))
        self.assertIn("sql client started but exited 1", self.combined(proc))
        self.assertIn("side NOT complete", self.combined(proc))


class CaptureModelTests(RunSqlTestBase):
    """Snapshot diff, whole new files, prefix strip, duplicate preservation."""

    def test_preexisting_rows_are_excluded(self):
        self.flink_home = self.make_flink_home(
            gluten_jars=True, pre_rows="+I[old, row]\n+I[older]\n")
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[new]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[new]\n")

    def test_rows_from_a_new_tm_log_are_taken_whole(self):
        self.flink_home = self.make_flink_home(
            gluten_jars=True, pre_rows="+I[old]\n")
        tm2 = self.flink_home / "log" / "flink-root-taskexecutor-9-host2.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[from-second-tm]"]),
            MOCK_TM_OUT=tm2,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[from-second-tm]\n")

    def test_rows_across_two_tms_are_merged_with_duplicates_kept(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        tm2 = self.flink_home / "log" / "flink-root-taskexecutor-2-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[x]"]),
            MOCK_TM_OUT=f"{tm1}:{tm2}",  # both TMs print the same row
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        # duplicates are preserved: the fold compares multisets
        self.assertEqual(out.read_text(), "+I[x]\n+I[x]\n")


class GlutenMentionTests(unittest.TestCase):
    """flink_job.gluten_mention: only engine-generated fields count. The
    "description" field embeds user SQL text — a table named gluten_orders
    must not mark a node; operator/strategy/input markers must."""

    def test_user_sql_text_in_description_is_not_gluten(self):
        node = {
            "description": "Source: gluten_orders[1]<br/>+- GroupAggregate"
                           "(key: [gluten_bid])",
            "operator": "n/a", "operator_strategy": "n/a",
            "inputs": [{"id": "0", "ship_strategy": "HASH",
                        "exchange": "pipelined_bounded"}],
        }
        self.assertFalse(flink_job.gluten_mention(node))

    def test_operator_name_with_gluten_marks(self):
        self.assertTrue(flink_job.gluten_mention(
            {"operator": "Gluten cal", "operator_strategy": "n/a",
             "description": "Source: datagen[1]"}))

    def test_operator_strategy_with_gluten_marks_case_insensitive(self):
        self.assertTrue(flink_job.gluten_mention(
            {"operator": "n/a", "operator_strategy": "glutenhash"}))

    def test_input_ship_strategy_and_exchange_mark(self):
        self.assertTrue(flink_job.gluten_mention(
            {"operator": "n/a",
             "inputs": [{"id": "0", "ship_strategy": "GlutenHASH",
                         "exchange": "pipelined_bounded"}]}))
        self.assertTrue(flink_job.gluten_mention(
            {"operator": "n/a",
             "inputs": [{"id": "0", "ship_strategy": "HASH",
                         "exchange": "gluten-local"}]}))

    def test_na_placeholder_fields_are_not_gluten(self):
        # "n/a" is what empty engine fields render as — placeholders, not
        # engine markers, and must not crash or match
        node = {"description": "n/a", "operator": "n/a",
                "operator_strategy": "n/a", "id": "1",
                "inputs": [{"id": "0", "ship_strategy": "n/a",
                            "exchange": "n/a"}]}
        self.assertFalse(flink_job.gluten_mention(node))

    def test_non_dict_and_missing_inputs_are_safe(self):
        self.assertFalse(flink_job.gluten_mention("n/a"))
        self.assertFalse(flink_job.gluten_mention(None))
        self.assertFalse(flink_job.gluten_mention(
            {"operator": "n/a", "inputs": None}))


class PlanDescriptionTests(RunSqlTestBase):
    """End to end: user SQL identifiers in node descriptions (the mock's
    clean plan reads from a table named gluten_orders) must not mark nodes
    as gluten — the native side passes, a fallen-back gfv side still
    fails."""

    def test_native_side_passes_with_user_sql_in_description(self):
        self.flink_home = self.make_flink_home(gluten_jars=False)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "native",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=0,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        report = self.job_json(out)
        self.assertEqual(report["jobs"][0]["gluten_nodes"], 0)
        # non-vacuous: the plan really does carry the user identifier in
        # the node description
        st = mock_flink.load_state(str(self.state_path))
        jid = report["jobs"][0]["jid"]
        descs = " ".join(n.get("description", "")
                         for n in st["plans"][jid]["nodes"])
        self.assertIn("gluten_orders", descs)

    def test_gfv_fallback_with_user_sql_in_description_still_fails(self):
        # same identifier-rich description, gfv mode, no engine markers:
        # the stack check must still refuse (no gluten nodes), not pass
        # on the user table name
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=0,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no gluten nodes", self.combined(proc))
        self.assertEqual(self.job_json(out)["jobs"][0]["gluten_nodes"], 0)


class TmGuardTests(RunSqlTestBase):
    """The pre-submit single-node guard: remote TaskManagers refuse the run
    before anything is submitted; all-local clusters (including this
    hostname) run as usual."""

    def tm_entries(self, hosts):
        return [{"id": f"tm-{i}", "path": f"akka://flink@{h}:1234/user/"
                                         f"taskmanager_{i}"}
                for i, h in enumerate(hosts)]

    def test_remote_taskmanager_refuses_before_submit(self):
        mock_flink.set_taskmanagers(
            str(self.state_path),
            self.tm_entries(["localhost", "worker-9.example.org"]))
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("gfv")
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("TaskManager", self.combined(proc))
        self.assertIn("worker-9.example.org", self.combined(proc))
        self.assertIn("nothing was submitted", self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_all_local_taskmanagers_pass_including_hostname(self):
        mock_flink.set_taskmanagers(
            str(self.state_path),
            self.tm_entries(["localhost", "127.0.0.1",
                             socket.gethostname()]))
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")

    def test_taskmanager_with_unknown_host_refuses(self):
        # an entry whose host cannot be determined may be remote — the
        # guard refuses (fail-safe) rather than guess
        mock_flink.set_taskmanagers(
            str(self.state_path),
            [{"id": "tm-odd"}])
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("gfv")
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("cannot be determined", self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_tms_local_subcommand_prints_local_hosts(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc = subprocess.run(
            [sys.executable, str(FLINK_JOB), "tms-local",
             "--rest", f"http://127.0.0.1:{self.port}"],
            capture_output=True, text=True, check=False, timeout=30,
            cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all 1 TaskManagers are on this machine", proc.stdout)
        self.assertIn("localhost", proc.stdout)


class CaptureDirTests(RunSqlTestBase):
    """The capture directory is created up front: a missing e2e/out must
    not masquerade as a client failure; an uncreatable path refuses
    before submitting."""

    def test_out_dir_created_when_missing(self):
        # e2e/out does not exist yet (fresh project): the script creates
        # it before the submit-log redirect runs — a path problem must not
        # masquerade as a client failure
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv", out_name="e2e/out/case.gfv.out",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")
        self.assertTrue((self.workdir / "e2e" / "out").is_dir())

    def test_uncreatable_out_dir_refused_before_submit(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        blocker = self.workdir / "blocker"
        blocker.write_text("a file where the capture dir would be\n")
        cmd, _ = self.side_cmd("gfv", out_name="blocker/case.gfv.out")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              check=False, timeout=60,
                              env=self.side_env(), cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("cannot create the capture directory",
                      self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_prestaged_tmp_file_is_never_touched(self):
        # a file at the old fixed scratch name ($out.tmp) must survive a
        # full successful run untouched: the filter scratch is created
        # exclusively by mktemp and only the file this run created is
        # moved or cleaned up
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        sentinel = self.workdir / "case.gfv.out.tmp"
        sentinel.write_text("precious pre-existing file\n", encoding="utf-8")
        proc, out = self.run_side(
            "gfv",
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")
        self.assertEqual(sentinel.read_text(),
                         "precious pre-existing file\n")
        leftovers = list(self.workdir.glob("case.gfv.out.??????"))
        self.assertEqual(leftovers, [])


class OutFileGuardTests(RunSqlTestBase):
    """run-sql.sh <out-file> shares the flink_job.py --out guard semantics:
    refused (exit 2) before anything is submitted."""

    def test_out_file_outside_project_refused_before_submit(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        cmd, _ = self.side_cmd("gfv", out_name="../outside.out")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              check=False, timeout=60,
                              env=self.side_env(), cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project directory", self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_existing_out_file_needs_overwrite_on_rerun(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv", MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1, MOCK_GLUTEN=1)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")
        # the evidence tree refreshes in place — but only with an explicit
        # --overwrite now; the old capture is kept, nothing is resubmitted
        self.marker.unlink()
        proc, _ = self.run_side(
            "gfv", MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1, MOCK_GLUTEN=1)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", self.combined(proc))
        self.assertFalse(self.marker.exists())
        self.assertEqual(out.read_text(), "+I[a]\n")
        # sidecars from the first run are refreshed by the --overwrite rerun
        # (the .job.json write carries the same overwrite consent)
        proc, out = self.run_side(
            "gfv", overwrite=True,
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1, MOCK_GLUTEN=1)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")
        self.assertEqual(self.job_json(out)["verdict"], "ok")

    def test_fifo_out_file_refused_even_with_overwrite(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        os.mkfifo(str(self.workdir / "case.gfv.out"))
        cmd, _ = self.side_cmd("gfv", out_name="case.gfv.out",
                               overwrite=True)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              check=False, timeout=60,
                              env=self.side_env(), cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a regular file", self.combined(proc))
        self.assertFalse(self.marker.exists())

    def test_symlinked_out_file_refused(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        real = self.workdir / "real.out"
        real.write_text("+I[x]\n", encoding="utf-8")
        (self.workdir / "case.gfv.out").symlink_to(real)
        cmd, _ = self.side_cmd("gfv", out_name="case.gfv.out")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              check=False, timeout=60,
                              env=self.side_env(), cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a symlink", self.combined(proc))
        self.assertFalse(self.marker.exists())


class FlinkJobOutGuardTests(unittest.TestCase):
    """flink_job.py wait --out: the same guard, refused before anything
    runs (REST stays untouched — the unreachable wait only proves the run
    was allowed to proceed)."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(dir=str(SCRATCH)))

    def wait_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(FLINK_JOB), "wait",
             "--rest", "http://127.0.0.1:1", "--before", "",
             "--mode", "gfv", "--poll", "0.05",
             "--first-job-timeout", "0.3", *args],
            capture_output=True, text=True, check=False, timeout=30,
            cwd=str(self.workdir))

    def test_outside_project_refused(self):
        proc = self.wait_tool("--out", "../outside.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project directory", proc.stderr)

    def test_existing_out_needs_overwrite(self):
        taken = self.workdir / "taken.json"
        taken.write_text("x", encoding="utf-8")
        proc = self.wait_tool("--out", "taken.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)
        self.assertEqual(taken.read_text(encoding="utf-8"), "x")
        # --overwrite lets the run proceed: it now reaches the (locally
        # unreachable) REST wait and the result lands in the file
        proc = self.wait_tool("--out", "taken.json", "--overwrite")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REST unreachable", proc.stderr)
        self.assertNotIn("already exists", proc.stderr)
        report = json.loads(taken.read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"], "rest-unreachable")

    def test_fifo_refused_even_with_overwrite(self):
        fifo = self.workdir / "fifo.json"
        os.mkfifo(str(fifo))
        proc = self.wait_tool("--out", "fifo.json", "--overwrite")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a regular file", proc.stderr)

    def test_symlink_refused(self):
        real = self.workdir / "real.json"
        real.write_text("x", encoding="utf-8")
        (self.workdir / "link.json").symlink_to(real)
        proc = self.wait_tool("--out", "link.json", "--overwrite")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project directory", proc.stderr)


class ToolAbortTests(RunSqlTestBase):
    """flink_job.py wait: an unexpected error still writes the same
    top-level key set (verdict tool-aborted, observation slots empty)."""

    def poisoned_wait(self, *args):
        # /jobs/overview answering valid JSON in a broken shape (a bare
        # list) crashes the poll loop with an unexpected AttributeError
        mock_flink.save_state(
            str(self.state_path), dict(mock_flink.empty_state(), poison=True))
        return subprocess.run(
            [sys.executable, str(FLINK_JOB), "wait",
             "--rest", f"http://127.0.0.1:{self.port}", "--before", "",
             "--mode", "gfv", "--poll", "0.05",
             "--first-job-timeout", "0.3", *args],
            capture_output=True, text=True, check=False, timeout=60,
            cwd=str(self.workdir))

    def test_abort_result_has_the_full_key_set(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc = self.poisoned_wait("--out", "job.json",
                                  "--flink-home", str(self.flink_home))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("tool aborted:", proc.stderr)
        report = json.loads(
            (self.workdir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(set(report), WAIT_KEYS, report)
        self.assertEqual(report["verdict"], "tool-aborted")
        self.assertEqual(report["jobs"], [])
        self.assertFalse(report["all_finished"])
        self.assertEqual(report["stack_check"], "not-run")
        self.assertIsNone(report["tm_velox4j_lines"])
        self.assertTrue(report["error_message"].startswith("tool aborted:"))
        self.assertTrue(report["next_step"])

    def test_abort_without_out_still_reports_on_stderr(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc = self.poisoned_wait()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("tool aborted:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_run_sql_reports_the_aborted_wait(self):
        # the poison flag flips between the pre-submit snapshot and the
        # wait: run-sql.sh must report the side as not complete, with the
        # abort shape in the job.json sidecar
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        proc, out = self.run_side(
            "gfv",
            MOCK_POISON=1,
            MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
            MOCK_TM_OUT=tm1,
            MOCK_GLUTEN=1,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("side NOT complete", self.combined(proc))
        report = self.job_json(out)
        self.assertEqual(set(report), WAIT_KEYS, report)
        self.assertEqual(report["verdict"], "tool-aborted")
        self.assertTrue(report["error_message"].startswith("tool aborted:"))
        self.assertTrue(report["next_step"])


DEAD_PROXY = "http://127.0.0.1:9"  # nothing listens there: routing through it fails fast


class ProxyBypassTests(RunSqlTestBase):
    """REST calls ignore environment proxies: with http_proxy pointing at a
    dead local port, the pre-submit snapshot and the whole run-sql.sh path
    still reach the mock cluster directly (a set no_proxy would quietly
    re-enable the direct route and make the test vacuous, so it is
    neutralized on purpose)."""

    def proxy_overlay(self, env):
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = DEAD_PROXY
        env["no_proxy"] = ""
        env["NO_PROXY"] = ""
        return env

    def test_snapshot_ignores_env_proxies(self):
        # the exact pre-submit call run-sql.sh makes: with the default
        # opener this routes through the dead proxy and fails (exit 2)
        env = dict(os.environ)
        env.pop("FLINK_REST_URL", None)
        proc = subprocess.run(
            [sys.executable, str(FLINK_JOB), "snapshot",
             "--rest", f"http://127.0.0.1:{self.port}"],
            capture_output=True, text=True, check=False, timeout=30,
            env=self.proxy_overlay(env), cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_run_sql_pre_submit_path_ignores_env_proxies(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        tm1 = self.flink_home / "log" / "flink-root-taskexecutor-1-host.out"
        cmd, out = self.side_cmd("gfv")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=120,
            env=self.proxy_overlay(self.side_env(
                MOCK_ROWS_FILE=self.rows_file(["+I[a]"]),
                MOCK_TM_OUT=tm1, MOCK_GLUTEN=1)),
            cwd=str(self.workdir))
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(out.read_text(), "+I[a]\n")


class SqlClientGuardTests(RunSqlTestBase):
    """The sql client is validated up front: a missing or non-executable
    $FLINK_HOME/bin/sql-client.sh fails fast (exit 1, nothing submitted —
    no submit-log redirect, no job.json, no wait) instead of surfacing as
    a bare client exit 126/127 deep in the wait phase."""

    def assert_client_refused(self):
        proc, out = self.run_side("gfv")
        self.assertEqual(proc.returncode, 1, self.combined(proc))
        self.assertIn("sql client not found or not executable",
                      self.combined(proc))
        self.assertIn("nothing was submitted", self.combined(proc))
        self.assertFalse(self.marker.exists())
        stem = out.name[:-len(".out")]
        self.assertFalse((out.parent / f"{stem}.submit.log").exists())
        self.assertFalse((out.parent / f"{stem}.job.json").exists())

    def test_missing_sql_client_refused_before_submit(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        (self.flink_home / "bin" / "sql-client.sh").unlink()
        self.assert_client_refused()

    def test_non_executable_sql_client_refused_before_submit(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        (self.flink_home / "bin" / "sql-client.sh").chmod(0o644)
        self.assert_client_refused()


class SubmitFailureWordingTests(RunSqlTestBase):
    """A submit failure distinguishes "client did not start" (exit 126/127
    — command missing/not executable or a redirect problem) from "client
    started but exited non-zero": same exit code 1, different detail."""

    def test_client_not_started_127_is_reported_as_not_started(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("gfv", MOCK_ADD_JOB=0, MOCK_RC=127,
                                FLINK_JOB_TIMEOUT=1)
        self.assertEqual(proc.returncode, 1, self.combined(proc))
        self.assertIn("sql client did not start", self.combined(proc))
        self.assertIn("127", self.combined(proc))
        self.assertNotIn("started but exited", self.combined(proc))
        self.assertIn("side NOT complete", self.combined(proc))

    def test_client_not_started_126_is_reported_as_not_started(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("gfv", MOCK_ADD_JOB=0, MOCK_RC=126,
                                FLINK_JOB_TIMEOUT=1)
        self.assertEqual(proc.returncode, 1, self.combined(proc))
        self.assertIn("sql client did not start", self.combined(proc))
        self.assertIn("126", self.combined(proc))
        self.assertNotIn("started but exited", self.combined(proc))

    def test_client_started_but_nonzero_exit_is_reported_as_such(self):
        self.flink_home = self.make_flink_home(gluten_jars=True)
        proc, _ = self.run_side("gfv", MOCK_ADD_JOB=0, MOCK_RC=2,
                                FLINK_JOB_TIMEOUT=1)
        self.assertEqual(proc.returncode, 1, self.combined(proc))
        self.assertIn("sql client started but exited 2", self.combined(proc))
        self.assertNotIn("did not start", self.combined(proc))
        self.assertIn("side NOT complete", self.combined(proc))


class RestUrlResolutionTests(unittest.TestCase):
    """flink_job.py rest-url: flag > env > conf (both layouts) > default."""

    def resolve(self, args, env_extra=None, home=None):
        env = dict(os.environ)
        env.pop("FLINK_REST_URL", None)
        env.pop("FLINK_HOME", None)
        env.update(env_extra or {})
        cmd = [sys.executable, str(FLINK_JOB), "rest-url"]
        if home:
            cmd += ["--flink-home", str(home)]
        cmd += args
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_explicit_flag_beats_everything(self):
        self.assertEqual(self.resolve(["--rest", "http://node:9999"]),
                         "http://node:9999")

    def test_env_beats_conf_and_adds_scheme(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "conf"), exist_ok=True)
        with open(os.path.join(home, "conf", "config.yaml"), "w") as fh:
            fh.write("rest:\n  address: 10.0.0.1\n  port: 1234\n")
        self.assertEqual(
            self.resolve([], env_extra={"FLINK_REST_URL": "localhost:1234"},
                         home=home),
            "http://localhost:1234")

    def test_nested_config_yaml(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "conf"), exist_ok=True)
        with open(os.path.join(home, "conf", "config.yaml"), "w") as fh:
            fh.write("rest:\n  # comment\n  address: 10.0.0.1\n"
                     "  port: 1234\n")
        self.assertEqual(self.resolve([], home=home),
                         "http://10.0.0.1:1234")

    def test_legacy_flat_flink_conf_yaml(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "conf"), exist_ok=True)
        with open(os.path.join(home, "conf", "flink-conf.yaml"), "w") as fh:
            fh.write("rest.address: example.internal # inline comment\n"
                     "rest.port: 7171\n")
        self.assertEqual(self.resolve([], home=home),
                         "http://example.internal:7171")

    def test_flink_conf_yaml_wins_when_both_exist(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "conf"), exist_ok=True)
        with open(os.path.join(home, "conf", "config.yaml"), "w") as fh:
            fh.write("rest:\n  address: 10.0.0.1\n  port: 1234\n")
        with open(os.path.join(home, "conf", "flink-conf.yaml"), "w") as fh:
            fh.write("rest.address: example.internal\nrest.port: 7171\n")
        self.assertEqual(self.resolve([], home=home),
                         "http://example.internal:7171")

    def test_default_when_no_conf(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "conf"), exist_ok=True)
        self.assertEqual(self.resolve([], home=home),
                         "http://localhost:8081")


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    module = sys.modules[__name__]
    loader = unittest.defaultTestLoader
    names = sys.argv[1:]
    suite = (loader.loadTestsFromNames(names, module) if names
             else loader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "run-sql-contract",
        "skill": "flink-velox-e2e-verify",
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
