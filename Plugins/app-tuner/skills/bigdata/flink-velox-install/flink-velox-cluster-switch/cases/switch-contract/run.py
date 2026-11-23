#!/usr/bin/env python3
"""switch-contract — tier-A case for flink-velox-cluster-switch.

Drives scripts/cluster_switch.py (plan / apply / verify) against a fake machine laid
out in a scratch directory — a Flink home whose lib/ holds a chosen GFV set (and a
lib.gfv-backup/ with one), fake start-cluster.sh / stop-cluster.sh sh stubs that
flip a REST state file and (re)create fake daemon cmdlines under a fake /proc, a
controllable fake REST endpoint (answers /overview while the state file says "up",
drops the connection when it says "down"; the overview payload carries a chosen
jobs-running count), and a fake flink-velox-env family with cluster-check and
lib-state-check stubs — with nothing from the real machine in play; the script's
seams ENV_PREPARE_SYSROOT / ENV_PREPARE_PROC_ROOT / ENV_PREPARE_EUID /
ENV_PREPARE_PREFIX_WRITABLE / ENV_PREPARE_ENV_FAMILY_DIR carry the fake. Pins the
plan (jar moves both ways, already-in-state skips, blocked switch with no GFV set
anywhere, running-jobs warning), the apply contract (--yes and --to required, jobs
running refused, blocked refused, root required when lib is not writable, jars
moved then stop-cluster.sh → leftover TERM/KILL → wait REST down → start-cluster.sh
→ wait REST up with a TaskManager, timeout reports the taskexecutor log tail, stop
never completing), verify, the JSON shape (also for a crash), --out safety and that
plan touches nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "cluster_switch.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_cluster_switch_result"
GFV_JARS = ("gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar",
            "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar")
JM_CLASS = "org.apache.flink.runtime.entrypoint.StandaloneSessionClusterEntrypoint"
TM_CLASS = "org.apache.flink.runtime.taskexecutor.TaskManagerRunner"
STATE = {"up": {"flink-version": "1.19.2", "taskmanagers": 2, "jobs-running": 0},
         "up-no-tm": {"flink-version": "1.19.2", "taskmanagers": 0, "jobs-running": 0}, "down": None}

STOP_STUB = textwrap.dedent('''\
    echo "stop-cluster.sh FLINK_HOME=$FLINK_HOME" >> "$FAKE_LOG"
    rm -f "$FAKE_PROC/501/cmdline"   # the pid-file based stop (the JM always goes)
    [ "${FAKE_STOP_LEAVE:-}" = 1 ] || rm -f "$FAKE_PROC/502/cmdline"   # a TM whose pid file was lost may stay
    echo down > "$FAKE_REST_STATE"
    ''')
START_STUB = textwrap.dedent('''\
    echo "start-cluster.sh FLINK_HOME=$FLINK_HOME" >> "$FAKE_LOG"
    [ "${FAKE_START_FAIL:-}" = 1 ] && { echo "Could not start" >&2; exit 1; }
    [ "${FAKE_START_HANG:-}" = 1 ] && exit 0   # started, but never comes back
    [ "${FAKE_START_NO_TM:-}" = 1 ] && { echo up-no-tm > "$FAKE_REST_STATE"; exit 0; }   # JM up, no TM registers
    mkdir -p "$FAKE_PROC/601" "$FAKE_PROC/602"
    printf '%s\\0' "/usr/lib/jvm/java-17/bin/java" "-classpath" "$FLINK_HOME/lib/flink-dist-1.19.2.jar" "org.apache.flink.runtime.entrypoint.StandaloneSessionClusterEntrypoint" > "$FAKE_PROC/601/cmdline"
    printf '%s\\0' "/usr/lib/jvm/java-17/bin/java" "-classpath" "$FLINK_HOME/lib/flink-dist-1.19.2.jar" "org.apache.flink.runtime.taskexecutor.TaskManagerRunner" > "$FAKE_PROC/602/cmdline"
    echo up > "$FAKE_REST_STATE"
    ''')
CHECK_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    name = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ok = os.environ.get("FAKE_CHECK_FAIL") != name
    key = "flink_velox_" + name.replace("flink-velox-", "").replace("-", "_") + "_result"
    print(json.dumps({key: {"success": ok, "missing": [] if ok else ["x: missing"], "next_step": "stub"}}))
    sys.exit(0 if ok else 1)
    ''')


def load_module():
    spec = importlib.util.spec_from_file_location("switch_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Handler(BaseHTTPRequestHandler):
    state_file = None

    def do_GET(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                state = fh.read().strip()
        except OSError:
            state = "down"
        payload = STATE.get(state)
        if self.path.split("?")[0] != "/overview" or payload is None:
            self.close_connection = True  # drop the connection: "nothing answers"
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RestServer:
    def __init__(self, state_file):
        handler = type("Handler", (_Handler,), {"state_file": str(state_file)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]


class FakeMachine:
    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.proc = self.root / "proc"
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.log = self.root / "log"
        self.state = self.root / "rest-state"
        self.family = self.root / "flink-velox-env"
        self.flink = self.sys / "opt" / "flink"
        self.lib = self.flink / "lib"
        self.backup = self.flink / "lib.gfv-backup"
        self.write(self.flink / "bin" / "flink", "#!/bin/sh\nexit 0\n", 0o755)
        self.write(self.flink / "bin" / "stop-cluster.sh", "#!/bin/sh\n" + STOP_STUB, 0o755)
        self.write(self.flink / "bin" / "start-cluster.sh", "#!/bin/sh\n" + START_STUB, 0o755)
        self.write(self.flink / "conf" / "config.yaml", "rest:\n  address: localhost\n  port: 8081\n")
        self.write(self.lib / "flink-dist-1.19.2.jar", "jar\n")
        self.rest_url = None
        self.closed_url = closed_port_url()

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def gfv_in_lib(self, jars=GFV_JARS):
        for jar in jars:
            self.write(self.lib / jar, "gfv\n")

    def gfv_in_backup(self, jars=GFV_JARS):
        for jar in jars:
            self.write(self.backup / jar, "gfv\n")

    def daemon(self, pid, role, dist=None):
        entry = JM_CLASS if role == "jobmanager" else TM_CLASS
        dist = str(dist or (self.lib / "flink-dist-1.19.2.jar"))
        argv = ["/usr/lib/jvm/java-17/bin/java", "-classpath", dist, entry]
        self.write(self.proc / str(pid) / "cmdline", "\0".join(argv) + "\0")

    def running(self, overview=None, jobs=0):
        """REST up with the given overview (default: 2 TMs, `jobs` running) and two daemons."""
        STATE["up"] = overview or {"flink-version": "1.19.2", "taskmanagers": 2, "jobs-running": jobs}
        self.state.write_text("up\n", encoding="utf-8")
        self.daemon(501, "jobmanager")
        self.daemon(502, "taskmanager")

    def stopped(self):
        self.state.write_text("down\n", encoding="utf-8")

    def fake_family(self):
        for skill, script in (("flink-velox-cluster-check", "cluster_check.py"),
                              ("flink-velox-lib-state-check", "lib_state_check.py")):
            self.write(self.family / skill / "scripts" / script, "#!/usr/bin/env python3\n" + CHECK_STUB, 0o755)

    def env(self, **overrides):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys), "ENV_PREPARE_PROC_ROOT": str(self.proc),
               "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family), "HOME": str(self.root), "LC_ALL": "C",
               "FLINK_HOME": str(self.flink), "FLINK_REST_URL": self.rest_url or self.closed_url,
               "FAKE_LOG": str(self.log), "FAKE_PROC": str(self.proc), "FAKE_REST_STATE": str(self.state)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def snapshot(self):
        seen = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            base = Path(dirpath)
            for name in dirnames:
                seen[str(base / name)] = ("d", (base / name).lstat().st_mode)
            for name in filenames:
                st = (base / name).lstat()
                seen[str(base / name)] = ("f", st.st_mode, st.st_size, st.st_mtime_ns)
        return seen


class SwitchCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")
        server = RestServer(self.m.state)
        self.addCleanup(server.close)
        self.m.rest_url = server.url
        self.m.stopped()

    def run_tool(self, *args, env=None, cwd=None):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, errors="replace",
                              env=self.m.env() if env is None else env, cwd=str(self.tmp if cwd is None else cwd),
                              stdin=subprocess.DEVNULL, timeout=120)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def step(self, result, name):
        return next(s for s in result["steps"] if s["name"] == name)

    def log(self):
        return self.m.log.read_text(encoding="utf-8") if self.m.log.exists() else ""

    # -- plan -----------------------------------------------------------------
    def test_plan_to_native_with_gfv_in_lib(self):
        self.m.gfv_in_lib()
        self.m.write(self.m.lib / "guava-33.4.0-jre.jar", "dep\n")
        self.m.running(jobs=0)
        code, out, err, r = self.run_tool("--json", "--to", "native")
        self.assertEqual((code, r["success"], r["command"], r["to"]), (0, True, "plan", "native"))
        self.assertEqual((r["flink_home"], r["rest_url"]), (str(self.m.flink), self.m.rest_url))
        jars, stop, start = (self.step(r, n) for n in ("jars", "stop", "start"))
        self.assertEqual((jars["status"], jars["in_lib"], jars["moving"], jars["needs_root"]), ("todo", sorted(GFV_JARS), sorted(GFV_JARS), False))
        self.assertEqual(jars["plan"][0], f"mkdir -p {self.m.backup}")
        self.assertEqual(jars["plan"][1], f"mv {self.m.lib}/{sorted(GFV_JARS)[0]} {self.m.backup}/{sorted(GFV_JARS)[0]}")
        self.assertEqual((stop["status"], stop["jobs_running"]), ("todo", 0))
        self.assertEqual(stop["plan"][0], f"{self.m.flink}/bin/stop-cluster.sh")
        self.assertEqual(start["plan"][1], f"# poll GET {self.m.rest_url}/overview until taskmanagers >= 1 (timeout 180s)")
        self.assertEqual(r["next_step"], "review the plan above, then run cluster_switch.py apply --yes --to native (restarting the cluster needs the user's consent)")
        _, out, _, _ = self.run_tool("--to", "native")
        self.assertIn(f"cluster-switch plan  (-> native, Flink {self.m.flink}, REST {self.m.rest_url})", out)
        self.assertIn("  jars    TODO    move 4 gluten/velox jar(s) out of lib: gluten-flink-loader-1.9.0-SNAPSHOT.jar", out)
        self.assertIn("PLAN: 3 step(s) to apply", out)
        self.assertEqual(err, "")

    def test_plan_to_gfv_restores_from_backup_and_skips_when_already_gfv(self):
        self.m.gfv_in_backup()
        self.m.running()
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual((jars["status"], jars["in_backup"], jars["moving"]), ("todo", sorted(GFV_JARS), sorted(GFV_JARS)))
        self.assertEqual(jars["plan"][0], f"mv {self.m.backup}/{sorted(GFV_JARS)[0]} {self.m.lib}/{sorted(GFV_JARS)[0]}")
        self.assertEqual(jars["plan"][-1], f"rmdir {self.m.backup}   # when empty")
        shutil.rmtree(self.m.backup)
        self.m.gfv_in_lib()
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        self.assertEqual((self.step(r, "jars")["status"], self.step(r, "stop")["status"]), ("skipped", "todo"))  # restart still happens

    def test_plan_blocked_when_no_gfv_set_exists(self):
        code, out, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(jars["status"], "blocked")
        self.assertEqual(jars["detail"], "no GFV jar in lib and nothing in the backup directory: there is no GFV set to switch to")
        self.assertEqual(jars["fix"], "run flink-velox-stack-build to build and deploy the GFV set first")
        self.assertIn("  jars    BLOCKED no GFV jar in lib", self.run_tool("--to", "gfv")[1])

    def test_plan_warns_about_running_jobs(self):
        self.m.gfv_in_lib()
        self.m.running(jobs=3)
        _, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual(self.step(r, "stop")["jobs_running"], 3)
        self.assertIn("3 job(s) are running — a restart would interrupt them", r["next_step"])

    def test_plan_when_the_cluster_is_down(self):
        self.m.gfv_in_lib()
        _, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual((self.step(r, "jars")["status"], self.step(r, "stop")["status"], self.step(r, "start")["status"]),
                         ("todo", "skipped", "todo"))
        self.assertEqual(self.step(r, "stop")["detail"], "the cluster is not running")

    def test_plan_without_flink_home(self):
        (self.m.flink / "bin" / "flink").unlink()
        code, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual((code, r["flink_home"], r["steps"]), (0, None, []))
        self.assertIn("no Flink installation", r["next_step"])

    def test_plan_touches_nothing(self):
        self.m.gfv_in_lib()
        before = self.m.snapshot()
        self.run_tool("--json", "--to", "native")
        self.assertEqual(self.m.snapshot(), before)

    # -- apply ----------------------------------------------------------------
    def test_apply_refuses_without_yes_or_target(self):
        self.m.gfv_in_lib()
        code, out, err, r = self.run_tool("--json", "apply", "--to", "native")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertTrue(r["error_message"].startswith("refused: apply needs --yes"))
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("gluten*")), sorted(j for j in GFV_JARS if j.startswith("gluten")))
        self.assertIn("REFUSED: apply needs --yes", self.run_tool("apply", "--to", "native")[1])
        code, out, err, r = self.run_tool("apply", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("--to native|gfv is required", err)

    def test_apply_refuses_while_jobs_are_running(self):
        self.m.gfv_in_lib()
        self.m.running(jobs=2)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("2 job(s) running on the cluster — a restart would interrupt them", r["error_message"])
        self.assertEqual(self.log(), "")
        self.assertTrue((self.m.lib / GFV_JARS[0]).exists())

    def test_apply_refuses_blocked_and_root_steps(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual(code, 2)
        self.assertIn("the jars step is blocked: no GFV jar in lib", r["error_message"])
        self.m.gfv_in_lib()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn('steps jars need root (lib not writable): run "sudo python3 cluster_switch.py apply --yes --to native"', r["error_message"])
        self.assertEqual(self.log(), "")

    def test_apply_to_native_moves_jars_and_restarts(self):
        self.m.gfv_in_lib()
        self.m.write(self.m.lib / "guava-33.4.0-jre.jar", "dep\n")
        self.m.running(jobs=0)
        # a daemon of ANOTHER installation on the same host: never touched
        self.m.daemon(701, "taskmanager", dist=self.tmp / "other-flink" / "lib" / "flink-dist-1.18.1.jar")
        # the stop script leaves the TaskManager behind (lost pid file): the leftover path runs
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", env=self.m.env(FAKE_STOP_LEAVE="1"))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("jars", "done"), ("stop", "done"), ("start", "done")])
        self.assertEqual(sorted(p.name for p in self.m.backup.iterdir()), sorted(GFV_JARS))
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("*.jar")), ["flink-dist-1.19.2.jar", "guava-33.4.0-jre.jar"])
        self.assertEqual(self.step(r, "start")["detail"], "cluster back: Flink 1.19.2, 2 TaskManager(s), 0 job(s) running")
        actions = [(c["step"], c["action"]) for c in r["changes"]]
        self.assertEqual(actions[0], ("jars", "moved out"))
        self.assertEqual(actions[1], ("stop", "stop-cluster.sh"))
        self.assertIn(("stop", "TERM failed"), actions)  # the fake pid cannot be signalled; the attempt is recorded
        self.assertIn(("stop", "KILL failed"), actions)
        self.assertIn(("start", "start-cluster.sh"), actions)
        term = next(c for c in r["changes"] if c["action"] == "TERM failed")
        self.assertTrue(term["detail"].startswith("taskmanager pid 502"))
        self.assertFalse(any("701" in c["detail"] for c in r["changes"]))  # the other installation's daemon
        self.assertTrue((self.m.proc / "701" / "cmdline").exists())
        self.assertEqual(r["next_step"], "switched to the native build and restarted; run cluster_switch.py verify --to native")
        self.assertIn(f"stop-cluster.sh FLINK_HOME={self.m.flink}\nstart-cluster.sh FLINK_HOME={self.m.flink}", self.log())
        report = load_module().render({KEY: r})
        self.assertIn("SWITCHED: lib holds the native build and the cluster was restarted", report)
        self.assertIn("    - jars: moved out — 4 jar(s) -> " + str(self.m.backup), report)
        # and back: --to gfv restores from the backup and removes the empty backup dir
        self.m.running(jobs=0)
        for pid in (601, 602):
            shutil.rmtree(self.m.proc / str(pid), ignore_errors=True)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("gluten*")), sorted(j for j in GFV_JARS if j.startswith("gluten")))
        self.assertFalse(self.m.backup.exists())
        self.assertEqual(r["changes"][0]["action"], "moved in")

    def test_apply_skips_stop_when_down_and_fails_on_start_timeout_with_log_tail(self):
        self.m.gfv_in_lib()
        self.m.stopped()
        self.m.write(self.m.flink / "log" / "flink-root-taskexecutor-1.host.log", "x\n" * 3 + "java.lang.OutOfMemoryError: Java heap space\n")
        code, out, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--timeout", "2", env=self.m.env(FAKE_START_HANG="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][1:], [("stop", "skipped"), ("start", "failed")])
        start = self.step(r, "start")
        self.assertIn("the cluster did not come back with a TaskManager within 2s", start["detail"])
        self.assertIn("tail of " + str(self.m.flink / "log/flink-root-taskexecutor-1.host.log") + ":", start["detail"])
        self.assertIn("OutOfMemoryError: Java heap space", start["detail"])
        self.assertEqual((r["error_message"], r["changes"][1]["action"], r["changes"][-1]["action"]),
                         ("start failed", "skipped", "failed"))
        self.assertEqual(sorted(p.name for p in self.m.backup.iterdir()), sorted(GFV_JARS))  # jars moved, cluster not back
        self.assertIn("NOT SWITCHED: start failed; later steps not run", self.run_tool("apply", "--yes", "--to", "native", "--timeout", "2", env=self.m.env(FAKE_START_HANG="1"))[1])

    def test_apply_fails_when_the_jm_comes_back_without_a_taskmanager(self):
        self.m.gfv_in_lib()
        self.m.stopped()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--timeout", "2", env=self.m.env(FAKE_START_NO_TM="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("the cluster did not come back with a TaskManager within 2s", self.step(r, "start")["detail"])
        self.assertEqual(r["error_message"], "start failed")

    def test_apply_fails_when_start_cluster_script_fails(self):
        self.m.gfv_in_lib()
        self.m.stopped()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--timeout", "2", env=self.m.env(FAKE_START_FAIL="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("(start-cluster.sh exit 1)", self.step(r, "start")["detail"])
        self.assertEqual(next(c for c in r["changes"] if c["action"] == "start-cluster.sh")["detail"], "exit 1: Could not start")

    def test_apply_fails_when_rest_keeps_answering_after_the_stop(self):
        self.m.gfv_in_lib()
        self.m.running(jobs=0)
        # a start stub cannot make REST go down: simulate a daemon of another installation owning the port
        env = self.m.env(FAKE_REST_STATE=str(self.m.root / "nowhere"))  # the server reads a missing file -> "down"
        self.m.state.write_text("up\n", encoding="utf-8")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--timeout", "2")
        # the stub flips the real state file to "down", so this run succeeds; force the failure with a frozen state
        self.assertEqual((code, r["success"]), (0, True))
        self.m.running(jobs=0)
        frozen = self.m.root / "frozen"
        frozen.write_text("up\n", encoding="utf-8")
        server = RestServer(frozen)
        self.addCleanup(server.close)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv", "--timeout", "2", env=self.m.env(FLINK_REST_URL=server.url))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("still answers 2s after the stop — a daemon of another installation may own the port", self.step(r, "stop")["detail"])
        self.assertEqual(r["error_message"], "stop failed")

    # -- verify ---------------------------------------------------------------
    def test_verify_runs_cluster_and_lib_state_checks(self):
        self.m.fake_family()
        code, out, _, r = self.run_tool("--json", "verify", "--to", "native")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "verify"))
        self.assertEqual([(c["name"], c["status"]) for c in r["checks"]], [("cluster", "ok"), ("lib-state", "ok")])
        self.assertIn("VERIFIED: the cluster runs the native build", self.run_tool("verify", "--to", "native")[1])
        code, _, _, r = self.run_tool("--json", "verify", "--to", "gfv", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-lib-state-check"))
        self.assertEqual((code, r["error_message"]), (1, "1 check(s) failing"))

    def test_verify_reports_missing_siblings(self):
        _, _, _, r = self.run_tool("--json", "verify", "--to", "native")
        self.assertEqual([c["status"] for c in r["checks"]], ["skipped", "skipped"])

    # -- shape, --out, crash --------------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        self.m.gfv_in_lib()
        _, _, _, plan = self.run_tool("--json", "--to", "native")
        _, _, _, verified = self.run_tool("--json", "verify", "--to", "native")
        self.assertEqual(set(plan), {"success", "checked_at", "command", "to", "flink_home", "rest_url", "steps", "changes",
                                    "root_steps", "next_step", "error_message"})
        self.assertEqual(set(verified), set(plan) | {"checks"})
        self.assertEqual(set(plan["steps"][0]), {"name", "needs_root", "actions", "in_lib", "in_backup", "backup", "status", "detail",
                                                "plan", "moving", "lib", "direction"})
        module = load_module()

        def boom(explicit):
            raise RuntimeError("probe exploded")

        module.find_flink_home = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json", "--to", "native"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["command"], a["error_message"][:14]), (2, False, "plan", "tool aborted: "))
        self.assertEqual(set(a), set(plan))
        self.assertEqual((a["steps"], a["changes"], a["flink_home"]), ([], [], None))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.m.gfv_in_lib()
        archive = self.tmp / "project" / "switch.json"
        _, stdout, _, _ = self.run_tool("--json", "--to", "native", "--out", str(archive))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), json.loads(stdout))
        first = archive.read_text(encoding="utf-8")
        code, _, err, _ = self.run_tool("--json", "--to", "native", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        code, _, err, _ = self.run_tool("--json", "--to", "native", "--out", str(self.tmp / "outside.json"), cwd=str(elsewhere))
        self.assertEqual(code, 2)
        self.assertIn("inside the project directory", err)
        target = self.tmp / "keep.json"
        target.write_text("keep me\n", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        code, _, err, _ = self.run_tool("--json", "--to", "native", "--out", str(link))
        self.assertEqual((code, target.read_text(encoding="utf-8")), (2, "keep me\n"))
        self.assertIn("not a symlink", err)
        self.assertEqual(self.run_tool("--json", "--to", "native", "--out", str(archive), "--overwrite")[0], 0)

    def test_invalid_command_is_2_without_json(self):
        code, out, err, r = self.run_tool("restart", "--to", "native")
        self.assertEqual((code, r), (2, None))
        self.assertIn("usage", err.lower())


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"case": "switch-contract", "skill": "flink-velox-cluster-switch",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
