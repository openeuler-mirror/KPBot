#!/usr/bin/env python3
"""switch-contract — tier-A case for flink-velox-cluster-switch.

Drives scripts/cluster_switch.py (plan / apply / verify) against a fake machine laid
out in a scratch directory — a Flink home whose lib/ holds a chosen GlutenFlink set (and a
lib.gfv-backup/ with one), fake start-cluster.sh / stop-cluster.sh sh stubs that
flip a REST state file and (re)create fake daemon cmdlines under a fake /proc, a
controllable fake REST endpoint (answers /overview while the state file says "up",
drops the connection when it says "down"; the overview payload carries a chosen
jobs-running count), and a fake flink-velox-env family with cluster-check and
lib-state-check stubs — with nothing from the real machine in play: the fake daemon
PIDs sit above kernel.pid_max so no host process can own them, and under the fake
/proc the script records signal intents without ever signalling. The script's seams
ENV_PREPARE_SYSROOT / ENV_PREPARE_PROC_ROOT / ENV_PREPARE_EUID /
ENV_PREPARE_PREFIX_WRITABLE / ENV_PREPARE_ENV_FAMILY_DIR carry the fake. Pins the
plan (jar moves both ways, already-in-state skips, blocked switch when the GlutenFlink set
cannot become complete — components or the 14 runtime dependencies stack-build deploys,
same-name clashes never overwritten, running-jobs and unknown-jobs warnings, explicit
--flink-home being the only candidate, next_step keeping the reviewed options, only the
effective conf file being read), the apply
contract (--yes and --to required, jobs running / unknown refused, remote REST
refused, blocked refused, root required when lib is not writable, jars moved one by
one then stop-cluster.sh → leftover TERM/KILL → wait REST down → start-cluster.sh
→ wait REST up with a TaskManager, timeout reports the taskexecutor log tail, stop
never completing), verify (ok / failing / missing siblings reported as NOT VERIFIED,
siblings found in the installed flat layout), the JSON shape (also for a crash),
--out safety (checked before anything runs, a fifo is never opened) and that plan
touches nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "cluster_switch.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_cluster_switch_result"
GFV_JARS = ("gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar",
            "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar")
# The 14 runtime dependencies stack-build deploys with the components (the versions of the
# G:A:V list in stack_build.py, matched by artifactId prefix in the script).
DEP_IDS = ("guava", "failureaccess", "arrow-c-data", "arrow-format", "arrow-memory-core",
           "arrow-memory-unsafe", "arrow-vector", "commons-io", "flatbuffers-java",
           "jackson-annotations", "jackson-core", "jackson-databind",
           "jackson-datatype-jdk8", "jackson-datatype-jsr310")
DEP_JARS = ("guava-33.4.0-jre.jar", "failureaccess-1.0.2.jar", "arrow-c-data-18.1.0.jar",
            "arrow-format-18.1.0.jar", "arrow-memory-core-18.1.0.jar", "arrow-memory-unsafe-18.1.0.jar",
            "arrow-vector-18.1.0.jar", "commons-io-2.18.0.jar", "flatbuffers-java-24.3.25.jar",
            "jackson-annotations-2.18.0.jar", "jackson-core-2.18.0.jar", "jackson-databind-2.18.0.jar",
            "jackson-datatype-jdk8-2.18.0.jar", "jackson-datatype-jsr310-2.18.0.jar")
JM_CLASS = "org.apache.flink.runtime.entrypoint.StandaloneSessionClusterEntrypoint"
TM_CLASS = "org.apache.flink.runtime.taskexecutor.TaskManagerRunner"
STATE = {"up": {"flink-version": "1.19.2", "taskmanagers": 2, "jobs-running": 0},
         "up-no-jobs-key": {"flink-version": "1.19.2", "taskmanagers": 2},
         "down": None}


def pids_above_pid_max(count):
    """PIDs no host process can own: above kernel.pid_max."""
    with open("/proc/sys/kernel/pid_max", encoding="utf-8") as fh:
        pid_max = int(fh.read().strip())
    return [pid_max + i for i in range(1, count + 1)]


JM_PID, TM_PID, JM_PID2, TM_PID2, OTHER_PID = pids_above_pid_max(5)
SANDBOXED_TERM = "sent TERM (sandboxed proc root: not sent)"
SANDBOXED_KILL = "sent KILL (sandboxed proc root: not sent)"

STOP_STUB = textwrap.dedent('''\
    echo "stop-cluster.sh FLINK_HOME=$FLINK_HOME" >> "$FAKE_LOG"
    rm -f "$FAKE_PROC/$FAKE_JM_PID/cmdline"   # the pid-file based stop (the JM always goes)
    [ "${FAKE_STOP_LEAVE:-}" = 1 ] || rm -f "$FAKE_PROC/$FAKE_TM_PID/cmdline"   # a TM whose pid file was lost may stay
    echo down > "$FAKE_REST_STATE"
    ''')
START_STUB = textwrap.dedent('''\
    echo "start-cluster.sh FLINK_HOME=$FLINK_HOME" >> "$FAKE_LOG"
    [ "${FAKE_START_FAIL:-}" = 1 ] && { echo "Could not start" >&2; exit 1; }
    [ "${FAKE_START_HANG:-}" = 1 ] && exit 0   # started, but never comes back
    [ "${FAKE_START_NO_TM:-}" = 1 ] && { echo up-no-tm > "$FAKE_REST_STATE"; exit 0; }   # JM up, no TM registers
    mkdir -p "$FAKE_PROC/$FAKE_JM_PID2" "$FAKE_PROC/$FAKE_TM_PID2"
    printf '%s\\0' "/usr/lib/jvm/java-17/bin/java" "-classpath" "$FLINK_HOME/lib/flink-dist-1.19.2.jar" "org.apache.flink.runtime.entrypoint.StandaloneSessionClusterEntrypoint" > "$FAKE_PROC/$FAKE_JM_PID2/cmdline"
    printf '%s\\0' "/usr/lib/jvm/java-17/bin/java" "-classpath" "$FLINK_HOME/lib/flink-dist-1.19.2.jar" "org.apache.flink.runtime.taskexecutor.TaskManagerRunner" > "$FAKE_PROC/$FAKE_TM_PID2/cmdline"
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
# Helper process for fault injection: runs the script as __main__ with subprocess.run raising
# for the commands the test names (the script calls subprocess.run, so the patch takes effect).
FAULTY_RUNNER = textwrap.dedent('''\
    import runpy, subprocess, sys
    real_run = subprocess.run
    faults = dict(item.split("=", 1) for item in sys.argv[1].split(","))

    def faulty(argv, *args, **kwargs):
        for tail, kind in faults.items():
            if str(argv[0]).endswith(tail):
                if kind == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 120)
                raise FileNotFoundError(2, "No such file or directory", str(argv[0]))
        return real_run(argv, *args, **kwargs)

    subprocess.run = faulty
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
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

    def gfv_in_lib(self, jars=GFV_JARS, text="gfv\n"):
        for jar in jars:
            self.write(self.lib / jar, text)

    def gfv_in_backup(self, jars=GFV_JARS, text="gfv\n"):
        for jar in jars:
            self.write(self.backup / jar, text)

    def deps_in_lib(self, jars=DEP_JARS):
        for jar in jars:
            self.write(self.lib / jar, "dep\n")

    def daemon(self, pid, role, dist=None):
        entry = JM_CLASS if role == "jobmanager" else TM_CLASS
        dist = str(dist or (self.lib / "flink-dist-1.19.2.jar"))
        argv = ["/usr/lib/jvm/java-17/bin/java", "-classpath", dist, entry]
        self.write(self.proc / str(pid) / "cmdline", "\0".join(argv) + "\0")

    def running(self, overview=None, jobs=0):
        """REST up with the given overview (default: 2 TMs, `jobs` running) and two daemons."""
        STATE["up"] = overview or {"flink-version": "1.19.2", "taskmanagers": 2, "jobs-running": jobs}
        self.state.write_text("up\n", encoding="utf-8")
        self.daemon(JM_PID, "jobmanager")
        self.daemon(TM_PID, "taskmanager")

    def stopped(self):
        self.state.write_text("down\n", encoding="utf-8")

    def fake_family(self, root=None):
        for skill, script in (("flink-velox-cluster-check", "cluster_check.py"),
                              ("flink-velox-lib-state-check", "lib_state_check.py")):
            self.write(Path(root or self.family) / skill / "scripts" / script, "#!/usr/bin/env python3\n" + CHECK_STUB, 0o755)

    def env(self, **overrides):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys), "ENV_PREPARE_PROC_ROOT": str(self.proc),
               "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family), "HOME": str(self.root), "LC_ALL": "C",
               "FLINK_HOME": str(self.flink), "FLINK_REST_URL": self.rest_url or self.closed_url,
               "FAKE_LOG": str(self.log), "FAKE_PROC": str(self.proc), "FAKE_REST_STATE": str(self.state),
               "FAKE_JM_PID": str(JM_PID), "FAKE_TM_PID": str(TM_PID), "FAKE_JM_PID2": str(JM_PID2), "FAKE_TM_PID2": str(TM_PID2)}
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
        STATE["up"] = {"flink-version": "1.19.2", "taskmanagers": 2, "jobs-running": 0}  # another test may have reshaped it

    def run_tool(self, *args, env=None, cwd=None, timeout=120):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, errors="replace",
                              env=self.m.env() if env is None else env, cwd=str(self.tmp if cwd is None else cwd),
                              stdin=subprocess.DEVNULL, timeout=timeout)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def run_tool_with_faults(self, faults, *args):
        """The script in its own process with subprocess.run failing for chosen commands
        ("stop-cluster.sh=timeout,start-cluster.sh=missing"). Faults are always injected in a
        child process that carries the fake environment: the script must never run inside this
        test process, where the real FLINK_HOME / PATH would point it at the machine."""
        runner = self.tmp / "faulty_runner.py"
        runner.write_text(FAULTY_RUNNER, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(runner), faults, str(SCRIPT), *args], capture_output=True, text=True,
                              errors="replace", env=self.m.env(), cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr, json.loads(proc.stdout)[KEY]

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
        self.assertEqual(r["conf_file"], str(self.m.flink / "conf" / "config.yaml"))
        jars, stop, start = (self.step(r, n) for n in ("jars", "stop", "start"))
        self.assertEqual((jars["status"], jars["in_lib"], jars["moving"], jars["needs_root"]), ("todo", sorted(GFV_JARS), sorted(GFV_JARS), False))
        self.assertEqual(jars["plan"][0], f"mkdir -p {self.m.backup}")
        self.assertEqual(jars["plan"][1], f"mv {self.m.lib}/{sorted(GFV_JARS)[0]} {self.m.backup}/{sorted(GFV_JARS)[0]}")
        self.assertEqual((stop["status"], stop["jobs_running"], stop["rest_was_up"]), ("todo", 0, True))
        self.assertEqual(stop["plan"][0], f"{self.m.flink}/bin/stop-cluster.sh")
        self.assertEqual(start["plan"][1], f"# poll GET {self.m.rest_url}/overview until taskmanagers >= 1 (timeout 180s)")
        self.assertEqual(r["next_step"], f"review the plan above, then run python3 {SCRIPT} apply --yes --to native (restarting the cluster needs the user's consent)")
        _, out, _, _ = self.run_tool("--to", "native")
        self.assertIn(f"cluster-switch plan  (-> native, Flink {self.m.flink}, REST {self.m.rest_url})", out)
        self.assertIn("  jars    TODO    move 4 gluten/velox jar(s) out of lib: gluten-flink-loader-1.9.0-SNAPSHOT.jar", out)
        self.assertIn("PLAN: 3 step(s) to apply", out)
        self.assertEqual(err, "")

    def test_plan_to_gfv_restores_from_backup_and_skips_when_already_gfv(self):
        self.m.deps_in_lib()
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

    def test_plan_to_gfv_blocked_without_the_runtime_dependencies(self):
        # all four components in lib but none of the 14 runtime dependencies stack-build
        # deploys: not "already GlutenFlink" — restarting on this set is what stack-build
        # explicitly says not to do, so the gate must block the switch
        self.m.gfv_in_lib()
        self.m.running(jobs=0)
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual(jars["status"], "blocked")
        self.assertIn("lib holds an incomplete GlutenFlink set (runtime dependencies missing: guava, failureaccess", jars["detail"])
        self.assertEqual((jars["missing"], jars["missing_deps"]), ([], list(DEP_IDS)))
        self.assertEqual(jars["fix"], "run flink-velox-stack-build to build and deploy the complete GlutenFlink set first, "
                                      "or switch to the native build instead (--to native)")
        self.assertNotIn("review the plan above", r["next_step"])
        # apply refuses on the blocked step: no jar moves, no restart happens
        before = self.m.snapshot()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, r["success"], self.log(), self.m.backup.exists()), (2, False, "", False))
        self.assertIn("the jars step is blocked: lib holds an incomplete GlutenFlink set", r["error_message"])
        self.assertEqual(self.m.snapshot(), before)
        self.assertTrue((self.m.lib / GFV_JARS[0]).exists())
        # the full set switches as before: components + dependencies in lib -> skip jars, restart
        self.m.deps_in_lib()
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped", "todo", "todo"])  # regression: switches as before
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertIn("SWITCHED: lib holds the gfv build and the cluster was restarted", self.run_tool("apply", "--yes", "--to", "gfv")[1])

    def test_plan_blocked_when_no_gfv_set_exists(self):
        code, out, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(jars["status"], "blocked")
        self.assertEqual(jars["detail"], "no GlutenFlink jar in lib and nothing in the backup directory: there is no GlutenFlink set to switch to")
        self.assertEqual(jars["fix"], "run flink-velox-stack-build to build and deploy the complete GlutenFlink set first, "
                                       "or switch to the native build instead (--to native)")
        self.assertEqual((jars["missing"], jars["missing_deps"]), (list(load_module().GFV_COMPONENTS), list(DEP_IDS)))
        self.assertIn("  jars    BLOCKED no GlutenFlink jar in lib", self.run_tool("--to", "gfv")[1])
        self.assertTrue(r["next_step"].startswith("run flink-velox-stack-build"))

    def test_plan_blocked_when_the_gfv_set_cannot_become_complete(self):
        # a lone velox4j jar in lib is not "already GlutenFlink": the set is incomplete
        self.m.gfv_in_lib(jars=("velox4j-0.1.0-SNAPSHOT.jar",))
        _, out, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual(jars["status"], "blocked")
        self.assertIn("lib holds an incomplete GlutenFlink set (missing: gluten-flink-planner, gluten-flink-loader, "
                      "gluten-flink-runtime; runtime dependencies missing: guava", jars["detail"])
        self.assertEqual((jars["missing"], jars["missing_deps"]),
                         (["gluten-flink-planner", "gluten-flink-loader", "gluten-flink-runtime"], list(DEP_IDS)))
        # the backup restores only part of the set
        self.m.gfv_in_backup(jars=("gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar"))
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertIn("moving the backup back would still leave the GlutenFlink set incomplete (missing: gluten-flink-loader; "
                      "runtime dependencies missing: guava", jars["detail"])
        self.assertEqual((jars["missing"], jars["missing_deps"]), (["gluten-flink-loader"], list(DEP_IDS)))
        self.assertEqual(jars["fix"], "run flink-velox-stack-build to build and deploy the complete GlutenFlink set first, "
                                      "or switch to the native build instead (--to native)")
        self.assertEqual([s["status"] for s in r["steps"]], ["blocked", "skipped", "todo"])  # the cluster is down: nothing to stop
        # a full backup that clashes with the incomplete lib set is not moved over it
        self.m.gfv_in_backup()
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual(jars["status"], "blocked")
        self.assertIn("1 backup jar(s) have the same name as jar(s) already in lib, moving them back would overwrite the lib copies: velox4j-0.1.0-SNAPSHOT.jar", jars["detail"])
        self.assertIn("  jars    BLOCKED 1 backup jar(s)", self.run_tool("--to", "gfv")[1])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, self.log()), (2, ""))

    def test_plan_to_gfv_restores_the_runtime_dependencies_from_the_backup_too(self):
        # the gate counts the dependencies towards completeness and lets them come back from
        # the backup as well: an empty lib plus a backup holding components AND dependencies
        # switches, while the same backup without the dependencies stays blocked
        self.m.gfv_in_backup(jars=GFV_JARS + DEP_JARS)
        self.m.running(jobs=0)
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual((jars["status"], jars["moving"]), ("todo", sorted(GFV_JARS + DEP_JARS)))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("*.jar")),
                         sorted(GFV_JARS + DEP_JARS + ("flink-dist-1.19.2.jar",)))
        self.assertFalse(self.m.backup.exists())
        # the same backup without the dependencies cannot complete the set: blocked, no restart
        for jar in GFV_JARS + DEP_JARS:
            (self.m.lib / jar).unlink()
        self.m.gfv_in_backup()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual(code, 2)
        self.assertIn("moving the backup back would still leave the GlutenFlink set incomplete", r["error_message"])
        self.assertEqual(self.log().count("start-cluster.sh"), 1)  # only the first apply restarted

    def test_plan_warns_about_running_jobs(self):
        self.m.gfv_in_lib()
        self.m.running(jobs=3)
        _, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual(self.step(r, "stop")["jobs_running"], 3)
        self.assertIn("3 job(s) are running — a restart would interrupt them", r["next_step"])

    def test_plan_warns_when_the_job_count_is_unknown(self):
        # daemons of this installation are up but REST is silent: "no job running" cannot be confirmed
        self.m.gfv_in_lib()
        self.m.daemon(JM_PID, "jobmanager")
        self.m.daemon(TM_PID, "taskmanager")
        _, out, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual((self.step(r, "stop")["status"], self.step(r, "stop")["jobs_running"]), ("todo", None))
        self.assertIn("cannot confirm that no job is running: REST http://127.0.0.1:", r["next_step"])
        self.assertIn("did not answer with jobs-running while daemons of this installation are up — apply will refuse", r["next_step"])
        self.assertNotIn("review the plan above", r["next_step"])
        # REST answers but without the jobs-running key: still unknown
        self.m.running(overview={"flink-version": "1.19.2", "taskmanagers": 2})
        _, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertIn("cannot confirm that no job is running", r["next_step"])

    def test_plan_when_the_cluster_is_down(self):
        self.m.gfv_in_lib()
        _, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual((self.step(r, "jars")["status"], self.step(r, "stop")["status"], self.step(r, "start")["status"]),
                         ("todo", "skipped", "todo"))
        self.assertEqual((self.step(r, "stop")["detail"], self.step(r, "stop")["rest_was_up"]), ("the cluster is not running", False))

    def test_plan_without_flink_home(self):
        (self.m.flink / "bin" / "flink").unlink()
        code, _, _, r = self.run_tool("--json", "--to", "native")
        self.assertEqual((code, r["flink_home"], r["steps"]), (0, None, []))
        self.assertIn("no Flink installation", r["next_step"])

    def test_explicit_flink_home_is_the_only_candidate(self):
        self.m.gfv_in_lib()
        typo = self.tmp / "no-such-flink"
        code, out, _, r = self.run_tool("--json", "--to", "native", "--flink-home", str(typo))
        self.assertEqual((code, r["success"], r["flink_home"]), (2, False, None))
        self.assertEqual(r["error_message"], f"refused: --flink-home {typo} is not a Flink installation (bin/flink, bin/start-cluster.sh and bin/stop-cluster.sh are needed)")
        report = self.run_tool("--to", "native", "--flink-home", str(typo))[1]
        self.assertIn(f"REFUSED: --flink-home {typo} is not a Flink installation", report)
        self.assertIn("next: --flink-home", report)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--flink-home", str(typo))
        self.assertEqual((code, self.log(), self.m.backup.exists()), (2, "", False))  # FLINK_HOME points at a real one: not used
        # a directory without the start/stop scripts is not an installation either
        half = self.m.sys / "opt" / "half-flink"
        self.m.write(half / "bin" / "flink", "#!/bin/sh\nexit 0\n", 0o755)
        code, _, _, r = self.run_tool("--json", "--to", "native", "--flink-home", str(half))
        self.assertEqual(code, 2)
        # a valid explicit path wins over FLINK_HOME
        other = self.m.sys / "opt" / "other-flink"
        for name in ("flink", "start-cluster.sh", "stop-cluster.sh"):
            self.m.write(other / "bin" / name, "#!/bin/sh\nexit 0\n", 0o755)
        self.m.write(other / "lib" / "flink-dist-1.19.2.jar", "jar\n")
        code, _, _, r = self.run_tool("--json", "--to", "native", "--flink-home", str(other))
        self.assertEqual((code, r["flink_home"], r["steps"][0]["status"]), (0, str(other), "skipped"))

    def test_next_step_keeps_the_reviewed_options(self):
        self.m.gfv_in_lib()
        args = ("--flink-home", str(self.m.flink), "--timeout", "7")
        _, _, _, r = self.run_tool("--json", "--to", "native", *args)
        self.assertEqual(r["next_step"], f"review the plan above, then run python3 {SCRIPT} apply --yes --to native --flink-home {self.m.flink} --timeout 7 "
                                         "(restarting the cluster needs the user's consent)")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", *args)
        self.assertEqual((code, r["next_step"]), (0, f"switched to the native build and restarted; run python3 {SCRIPT} verify --to native "
                                                    f"--flink-home {self.m.flink} --timeout 7"))
        shutil.rmtree(self.m.backup)
        self.m.gfv_in_lib()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", *args,
                                      env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertIn(f"steps jars need root (lib not writable): sudo python3 {SCRIPT} apply --yes --to native --flink-home {self.m.flink} --timeout 7",
                      r["error_message"])

    def test_the_suggested_commands_run_as_printed(self):
        """next_step / the sudo hint give complete commands: interpreter and the script's own
        path, so that copying them into a shell from any directory works (the bare file name is
        neither on PATH nor in the working directory)."""
        self.m.gfv_in_lib()
        self.m.deps_in_lib()
        self.m.fake_family()
        self.m.running(jobs=0)
        _, report, _, _ = self.run_tool("--to", "native")
        self.assertTrue(str(SCRIPT).startswith(str(ROOT) + os.sep))  # the skill's real location, not a relative name
        self.assertIn(f"next: review the plan above, then run python3 {SCRIPT} apply --yes --to native", report)
        _, _, _, r = self.run_tool("--json", "--to", "native")
        # the plan's command, taken verbatim from next_step, pasted into a shell elsewhere
        command = r["next_step"].split("then run ", 1)[1].rsplit(" (restarting", 1)[0]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("SWITCHED: lib holds the native build and the cluster was restarted", proc.stdout)
        self.assertFalse((self.m.lib / GFV_JARS[0]).exists())
        # the command suggested after a successful switch, again verbatim
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native")
        self.assertEqual(code, 0)
        command = r["next_step"].split("run ", 1)[1]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("VERIFIED: the cluster runs the native build", proc.stdout)
        # the sudo hint names the same script; "sudo" is the only addition
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn(f"need root (lib not writable): sudo python3 {SCRIPT} apply --yes --to gfv", r["error_message"])

    def test_only_the_effective_conf_file_is_read(self):
        self.m.gfv_in_lib()
        _, _, _, r = self.run_tool("--json", "--to", "native", env=self.m.env(FLINK_REST_URL=None))
        self.assertEqual((r["conf_file"], r["rest_url"]), (str(self.m.flink / "conf" / "config.yaml"), "http://localhost:8081"))
        # Flink 1.19 reads flink-conf.yaml when both exist and ignores config.yaml: so does the tool
        self.m.write(self.m.flink / "conf" / "flink-conf.yaml", "rest:\n  address: localhost\n  port: 1\n")
        _, _, _, r = self.run_tool("--json", "--to", "native", env=self.m.env(FLINK_REST_URL=None))
        self.assertEqual((r["conf_file"], r["rest_url"]), (str(self.m.flink / "conf" / "flink-conf.yaml"), "http://localhost:1"))

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

    def test_apply_refuses_when_the_job_count_is_unknown(self):
        self.m.gfv_in_lib()
        self.m.daemon(JM_PID, "jobmanager")
        self.m.daemon(TM_PID, "taskmanager")
        code, out, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("cannot confirm that no job is running: REST http://127.0.0.1:", r["error_message"])
        self.assertIn("a restart could interrupt them", r["error_message"])
        # nothing was moved and no stop/start script ran
        self.assertEqual((self.log(), self.m.backup.exists()), ("", False))
        self.assertTrue((self.m.lib / GFV_JARS[0]).exists())
        self.assertTrue((self.m.proc / str(TM_PID) / "cmdline").exists())
        report = self.run_tool("apply", "--yes", "--to", "native")[1]
        self.assertIn("REFUSED: cannot confirm that no job is running", report)

    def test_apply_refuses_a_rest_address_that_is_not_this_machine(self):
        self.m.gfv_in_lib()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", env=self.m.env(FLINK_REST_URL="http://10.255.255.1:8081"))
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("REST http://10.255.255.1:8081 is not on this machine: this tool only restarts the local installation", r["error_message"])
        _, _, _, r = self.run_tool("--json", "--to", "native", env=self.m.env(FLINK_REST_URL="http://10.255.255.1:8081"))
        self.assertIn("is not on this machine — apply will refuse", r["next_step"])
        self.assertEqual(self.log(), "")

    def test_apply_refuses_blocked_and_root_steps(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual(code, 2)
        self.assertIn("the jars step is blocked: no GlutenFlink jar in lib", r["error_message"])
        self.m.gfv_in_lib()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn(f"steps jars need root (lib not writable): sudo python3 {SCRIPT} apply --yes --to native", r["error_message"])
        self.assertEqual(self.log(), "")

    def test_apply_never_overwrites_a_same_named_jar(self):
        self.m.gfv_in_lib(text="new build\n")
        self.m.deps_in_lib()
        self.m.gfv_in_backup(text="older set\n")
        before = self.m.snapshot()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("the jars step is blocked: 4 jar(s) to move out already exist in the backup directory, moving would overwrite them", r["error_message"])
        self.assertEqual(self.m.snapshot(), before)
        # the other way round: lib already holds the complete (new) set, the older backup is left alone
        _, _, _, r = self.run_tool("--json", "--to", "gfv")
        jars = self.step(r, "jars")
        self.assertEqual((jars["status"], "moving" in jars, self.step(r, "stop")["status"]), ("skipped", False, "skipped"))  # cluster is down: only the start runs
        self.assertEqual(jars["detail"], "the GlutenFlink set is in lib already (4 component jar(s) + the 14 runtime dependencies)"
                         f"; 4 older jar(s) left in {self.m.backup}")
        self.assertEqual((self.m.lib / GFV_JARS[0]).read_text(encoding="utf-8"), "new build\n")

    def test_apply_to_native_moves_jars_and_restarts(self):
        self.m.gfv_in_lib()
        self.m.deps_in_lib()
        self.m.running(jobs=0)
        # a daemon of ANOTHER installation on the same host: never touched
        self.m.daemon(OTHER_PID, "taskmanager", dist=self.tmp / "other-flink" / "lib" / "flink-dist-1.18.1.jar")
        # the stop script leaves the TaskManager behind (lost pid file): the leftover path runs
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", env=self.m.env(FAKE_STOP_LEAVE="1"))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("jars", "done"), ("stop", "done"), ("start", "done")])
        self.assertEqual(sorted(p.name for p in self.m.backup.iterdir()), sorted(GFV_JARS))
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("*.jar")),
                         sorted(("flink-dist-1.19.2.jar",) + DEP_JARS))  # the runtime dependencies stay in lib
        self.assertEqual(self.step(r, "start")["detail"], "cluster back: Flink 1.19.2, 2 TaskManager(s), 0 job(s) running")
        actions = [(c["step"], c["action"]) for c in r["changes"]]
        self.assertEqual(actions[:4], [("jars", "moved out")] * 4)  # one entry per jar
        self.assertEqual(r["changes"][0]["detail"], f"{self.m.lib}/{sorted(GFV_JARS)[0]} -> {self.m.backup}/{sorted(GFV_JARS)[0]}")
        self.assertEqual(actions[4], ("stop", "stop-cluster.sh"))
        # the fake /proc is not the real one: the TERM/KILL intent is recorded and nothing is signalled
        self.assertEqual([a for a in actions if "TERM" in a[1] or "KILL" in a[1]], [("stop", SANDBOXED_TERM), ("stop", SANDBOXED_KILL)])
        term = next(c for c in r["changes"] if c["action"] == SANDBOXED_TERM)
        self.assertEqual(term["detail"], f"taskmanager pid {TM_PID}")
        self.assertFalse(any(str(OTHER_PID) in c["detail"] for c in r["changes"]))  # the other installation's daemon
        self.assertTrue((self.m.proc / str(OTHER_PID) / "cmdline").exists())
        self.assertIn(("start", "start-cluster.sh"), actions)
        self.assertEqual(r["next_step"], f"switched to the native build and restarted; run python3 {SCRIPT} verify --to native")
        self.assertIn(f"stop-cluster.sh FLINK_HOME={self.m.flink}\nstart-cluster.sh FLINK_HOME={self.m.flink}", self.log())
        report = load_module().render({KEY: r})
        self.assertIn("SWITCHED: lib holds the native build and the cluster was restarted", report)
        self.assertEqual(report.count("    - jars: moved out — "), 4)
        self.assertIn("    - jars: moved out — " + f"{self.m.lib}/{sorted(GFV_JARS)[0]} -> {self.m.backup}/{sorted(GFV_JARS)[0]}", report)
        # and back: --to gfv restores from the backup and removes the empty backup dir
        self.m.running(jobs=0)
        for pid in (JM_PID2, TM_PID2):
            shutil.rmtree(self.m.proc / str(pid), ignore_errors=True)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("gluten*")), sorted(j for j in GFV_JARS if j.startswith("gluten")))
        self.assertFalse(self.m.backup.exists())
        self.assertEqual(r["changes"][0]["action"], "moved in")

    def test_sandboxed_proc_root_records_signals_without_sending_any(self):
        self.m.running(jobs=0)
        module_env = self.m.env(FAKE_STOP_LEAVE="1")

        def no_kill(pid, sig):
            raise AssertionError(f"os.kill({pid}, {sig}) was called under a fake /proc")

        real_kill = os.kill
        os.kill = no_kill
        try:
            with patch.dict(os.environ, module_env, clear=True):
                module = load_module()
                self.assertFalse(module.REAL_PROC)
                steps = [{"name": "jars"}, {"name": "stop", "status": "todo", "rest_was_up": True}, {"name": "start"}]
                log = []
                self.assertTrue(module.apply_stop({"timeout": 5}, str(self.m.flink), self.m.rest_url, steps, log))
        finally:
            os.kill = real_kill
        self.assertEqual([(c["action"], c["detail"]) for c in log if "sent" in c["action"]],
                         [(SANDBOXED_TERM, f"taskmanager pid {TM_PID}"), (SANDBOXED_KILL, f"taskmanager pid {TM_PID}")])
        with patch.dict(os.environ, {"ENV_PREPARE_PROC_ROOT": "/proc"}, clear=True):
            self.assertTrue(load_module().REAL_PROC)  # on the real /proc the signals are real

    def test_apply_keeps_the_change_log_when_a_step_command_fails(self):
        self.m.gfv_in_lib()
        self.m.deps_in_lib()
        self.m.stopped()
        # the start command cannot be executed: the step fails and the jar moves made before it stay on record
        code, _, _, r = self.run_tool_with_faults("start-cluster.sh=missing", "--json", "apply", "--yes", "--to", "native")
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "start failed"))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("jars", "done"), ("stop", "skipped"), ("start", "failed")])
        self.assertIn("FileNotFoundError", self.step(r, "start")["detail"])
        self.assertEqual(r["changes"][-1], {"step": "start", "action": "failed", "detail": self.step(r, "start")["detail"]})
        self.assertEqual([c["action"] for c in r["changes"]], ["moved out"] * 4 + ["skipped", "failed"])
        self.assertEqual(sorted(p.name for p in self.m.backup.iterdir()), sorted(GFV_JARS))
        # a stop command that times out: the error is the step's failure, not a crash that erases the log
        self.m.running(jobs=0)
        code, _, _, r = self.run_tool_with_faults("stop-cluster.sh=timeout", "--json", "apply", "--yes", "--to", "gfv")
        self.assertEqual((code, r["error_message"], self.step(r, "stop")["status"]), (1, "stop failed", "failed"))
        self.assertIn("TimeoutExpired", self.step(r, "stop")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["moved in"] * 4 + ["failed"])
        self.assertFalse(self.m.backup.exists())
        # the failing commands never reached the scripts
        self.assertEqual(self.log(), "")

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
        self.assertEqual((r["error_message"], r["changes"][-1]["action"]), ("start failed", "failed"))
        self.assertEqual(next(c for c in r["changes"] if c["step"] == "stop")["action"], "skipped")
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
        self.m.running(jobs=0)
        frozen = self.m.root / "frozen"
        frozen.write_text("up\n", encoding="utf-8")
        server = RestServer(frozen)
        self.addCleanup(server.close)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--timeout", "2", env=self.m.env(FLINK_REST_URL=server.url))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("still answers 2s after the stop — a daemon of another installation may own the port", self.step(r, "stop")["detail"])
        self.assertEqual(r["error_message"], "stop failed")
        # the jar(s) moved before the failing stop are on record
        self.assertEqual([c["action"] for c in r["changes"]][:4], ["moved out"] * 4)

    # -- verify ---------------------------------------------------------------
    def test_verify_runs_cluster_and_lib_state_checks(self):
        self.m.fake_family()
        code, out, _, r = self.run_tool("--json", "verify", "--to", "native")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "verify"))
        self.assertEqual([(c["name"], c["status"]) for c in r["checks"]], [("cluster", "ok"), ("lib-state", "ok")])
        self.assertEqual((r["error_message"], r["next_step"]), ("", ""))
        self.assertIn("VERIFIED: the cluster runs the native build", self.run_tool("verify", "--to", "native")[1])
        code, _, _, r = self.run_tool("--json", "verify", "--to", "gfv", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-lib-state-check"))
        self.assertEqual((code, r["error_message"]), (1, "1 check(s) failing"))
        self.assertIn("NOT VERIFIED: 1 check(s) failing", self.run_tool("verify", "--to", "gfv", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-lib-state-check"))[1])

    def test_verify_reports_missing_siblings_as_not_verified(self):
        code, out, _, r = self.run_tool("--json", "verify", "--to", "native")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(c["name"], c["status"], c["skill"]) for c in r["checks"]],
                         [("cluster", "missing", "flink-velox-cluster-check"), ("lib-state", "missing", "flink-velox-lib-state-check")])
        self.assertEqual(r["error_message"], "2 check(s) not run (sibling skills not installed): flink-velox-cluster-check, flink-velox-lib-state-check")
        self.assertEqual(r["next_step"], f"install the missing check skill(s) (flink-velox-cluster-check, flink-velox-lib-state-check), then rerun python3 {SCRIPT} verify --to native")
        report = self.run_tool("verify", "--to", "native")[1]
        self.assertIn("  MISSING cluster: flink-velox-cluster-check is not installed next to this skill", report)
        self.assertIn("NOT VERIFIED: 2 check(s) not run (sibling skills not installed): flink-velox-cluster-check, flink-velox-lib-state-check", report)
        self.assertNotIn("\nVERIFIED", report)
    def test_verify_finds_the_siblings_in_the_flat_install_layout(self):
        # install.sh copies every skill to <config>/skills/<skill>/: no family dir, resolution goes by skill name
        skills = self.tmp / "config" / "skills"
        shutil.copytree(ROOT, skills / "flink-velox-cluster-switch", ignore=shutil.ignore_patterns("results", "__pycache__"))
        self.m.fake_family(skills)
        proc = subprocess.run([sys.executable, str(skills / "flink-velox-cluster-switch" / "scripts" / "cluster_switch.py"), "--json", "verify", "--to", "gfv"],
                              env=self.m.env(ENV_PREPARE_ENV_FAMILY_DIR=None), cwd=str(self.tmp), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        r = json.loads(proc.stdout)[KEY]
        self.assertEqual((proc.returncode, [(c["name"], c["status"]) for c in r["checks"]]), (0, [("cluster", "ok"), ("lib-state", "ok")]))

    # -- shape, --out, crash --------------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        self.m.gfv_in_lib()
        _, _, _, plan = self.run_tool("--json", "--to", "native")
        _, _, _, verified = self.run_tool("--json", "verify", "--to", "native")
        keys = {"success", "checked_at", "command", "to", "flink_home", "conf_file", "rest_url", "steps", "changes",
                "root_steps", "next_step", "error_message"}
        self.assertEqual(set(plan), keys)
        self.assertEqual(set(verified), keys | {"checks"})
        self.assertEqual(set(plan["steps"][0]), {"name", "needs_root", "actions", "in_lib", "in_backup", "backup", "status", "detail",
                                                "plan", "moving", "lib", "direction"})
        self.assertEqual(set(plan["steps"][1]), {"name", "needs_root", "actions", "status", "detail", "plan", "jobs_running", "rest_was_up"})
        module = load_module()

        def boom(explicit):
            raise RuntimeError("probe exploded")

        module.find_flink_home = boom
        buffer = io.StringIO()
        # in-process on purpose (to intercept the discovery call) — inside the fake environment, so
        # nothing the script could reach for points at the real machine
        with patch.dict(os.environ, self.m.env(), clear=True), redirect_stdout(buffer):
            code = module.main(["--json", "--to", "native"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["command"], a["error_message"][:14]), (2, False, "plan", "tool aborted: "))
        self.assertEqual(set(a), set(plan))
        self.assertEqual((a["steps"], a["changes"], a["flink_home"]), ([], [], None))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.m.gfv_in_lib()
        project = self.tmp / "project"
        project.mkdir()
        archive = project / "switch.json"
        _, stdout, _, _ = self.run_tool("--json", "--to", "native", "--out", str(archive))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), json.loads(stdout))
        first = archive.read_text(encoding="utf-8")
        code, out, err, r = self.run_tool("--json", "--to", "native", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        self.assertTrue(r["error_message"].startswith("refused: --out already exists"))
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

    def test_out_never_opens_a_fifo_and_is_checked_before_apply(self):
        self.m.gfv_in_lib()
        project = self.tmp / "project"
        project.mkdir()
        fifo = project / "pipe.json"
        os.mkfifo(str(fifo))
        # open() on a fifo blocks until a reader appears: the guard must refuse instead
        code, _, err, r = self.run_tool("--json", "--to", "native", "--out", str(fifo), "--overwrite", timeout=60)
        self.assertEqual((code, r["success"], stat.S_ISFIFO(fifo.lstat().st_mode)), (2, False, True))
        self.assertIn("exists and is not a regular file, refusing to overwrite it", err)
        # a bad --out is refused before anything runs: apply leaves the machine untouched
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        before = self.m.snapshot()
        code, _, err, r = self.run_tool("--json", "apply", "--yes", "--to", "native", "--out", str(self.tmp / "outside.json"), cwd=str(elsewhere))
        self.assertEqual((code, r["success"], r["steps"], r["changes"]), (2, False, [], []))
        self.assertTrue(r["error_message"].startswith("refused: --out must be a regular path inside the project directory"))
        self.assertEqual((self.m.snapshot(), self.log()), (before, ""))
        self.assertFalse((self.m.backup / GFV_JARS[0]).exists())

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
