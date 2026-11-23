#!/usr/bin/env python3
"""cluster-contract — tier-A case for flink-velox-cluster-check.

Drives scripts/cluster_check.py against a fake machine laid out in a scratch
directory — a sysroot with a Flink installation (lib/ jars with chosen mtimes,
conf), a fake /proc (btime, daemon cmdlines with a classpath naming this
installation's flink-dist, per-pid stat with a start time), a fake PATH with
nothing on it, and fake REST endpoints (an /overview server, a port that speaks
garbage, a closed port) — with nothing from the real machine in play; the
script's seams ENV_CHECK_SYSROOT and ENV_CHECK_PROC_ROOT carry the fake. Pins the
verdict contract (exit codes, success, status, missing, next_step), every probe
rule (daemon attribution by flink-dist path, loaded build per daemon and mixed,
lib vs loaded jars, jars replaced after the earliest start, REST overview incl.
version mismatch / fewer taskmanagers / running jobs / proxies ignored / garbage
port), the JSON shape (also for a crash), --out safety (inside the project, no
symlink, no silent overwrite) and that a check leaves the machine untouched.

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
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "cluster_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_cluster_check_result"
CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
JM_CLASS = "org.apache.flink.runtime.entrypoint.StandaloneSessionClusterEntrypoint"
TM_CLASS = "org.apache.flink.runtime.taskexecutor.TaskManagerRunner"
GFV_JARS = ["gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar",
            "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar"]
RUNTIME_DEP_JARS = ["guava-33.4.0-jre.jar", "failureaccess-1.0.2.jar", "arrow-c-data-18.1.0.jar",
                    "arrow-format-18.1.0.jar", "arrow-memory-core-18.1.0.jar", "arrow-memory-unsafe-18.1.0.jar",
                    "arrow-vector-18.1.0.jar", "commons-io-2.18.0.jar", "flatbuffers-java-24.3.25.jar",
                    "jackson-annotations-2.18.0.jar", "jackson-core-2.18.0.jar", "jackson-databind-2.18.0.jar",
                    "jackson-datatype-jdk8-2.18.0.jar", "jackson-datatype-jsr310-2.18.0.jar"]
OVERVIEW_OK = {"flink-version": "1.19.2", "taskmanagers": 2, "jobs-running": 0, "slots-total": 4, "slots-available": 4}
JAR_AGE = 7200        # every jar in lib/ is two hours old ...
DAEMON_AGE = 3600     # ... and the daemons started one hour ago
BTIME = 1700000000


def load_module():
    spec = importlib.util.spec_from_file_location("cluster_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]


class _OverviewHandler(BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        if self.path.split("?")[0] != "/overview":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RestServer:
    def __init__(self, overview):
        handler = type("Handler", (_OverviewHandler,), {"payload": overview})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


class GarbageServer:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.url = "http://127.0.0.1:%d" % self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.sendall(b"GARBAGE\r\n\r\n")
                except OSError:
                    pass

    def close(self):
        self.sock.close()
        self.thread.join(5)


class FakeMachine:
    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.proc = self.root / "proc"
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.flink = self.sys / "opt" / "flink"
        self.lib = self.flink / "lib"
        self.closed_url = closed_port_url()
        self.rest_url = None

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def age(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def flink_home(self, build="gfv"):
        self.write(self.flink / "bin" / "flink", "#!/bin/sh\nexit 0\n", 0o755)
        self.write(self.flink / "conf" / "config.yaml", "rest:\n  address: localhost\n  port: 8081\n")
        names = ["flink-dist-1.19.2.jar"] + (GFV_JARS + RUNTIME_DEP_JARS if build == "gfv" else [])
        for name in names:
            self.write(self.lib / name, "jar\n")
            self.age(self.lib / name, JAR_AGE)
        self.write(self.proc / "stat", f"btime {BTIME}\n")

    def daemon(self, pid, role, gfv=GFV_JARS, dist=None, age=DAEMON_AGE):
        entry = JM_CLASS if role == "jobmanager" else TM_CLASS
        dist = str(dist or (self.lib / "flink-dist-1.19.2.jar"))
        cp = ":".join([dist] + [str(self.lib / j) for j in gfv])
        argv = ["/usr/lib/jvm/java-17/bin/java", "-classpath", cp, entry]
        self.write(self.proc / str(pid) / "cmdline", "\0".join(argv) + "\0")
        ticks = int((time.time() - age - BTIME) * CLK_TCK)
        # /proc/<pid>/stat: fields after ") " start at field 3 (state); starttime is field 22
        self.write(self.proc / str(pid) / "stat", f"{pid} (java) S " + "0 " * 18 + f"{ticks} 0 0\n")

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "ENV_CHECK_SYSROOT": str(self.sys), "ENV_CHECK_PROC_ROOT": str(self.proc),
               "HOME": str(self.root), "LC_ALL": "C", "FLINK_HOME": str(self.flink),
               "FLINK_REST_URL": self.rest_url or self.closed_url}
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


class ClusterCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def rest(self, overview=None):
        server = RestServer(OVERVIEW_OK if overview is None else overview)
        self.addCleanup(server.close)
        self.m.rest_url = server.url
        return server

    def run_check(self, *args, env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd), stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_not_running_is_a_failure(self):
        self.m.flink_home()
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        c = r["cluster"]
        self.assertEqual((c["running"], c["jobmanagers"], c["taskmanagers"], c["consistent_with_lib"],
                          c["loaded_build"], c["rest_reachable"]), (False, [], [], None, None, False))
        self.assertEqual(self.item(r, "processes")["detail"], "not running: no JobManager and no TaskManager "
                         "of this installation")
        self.assertEqual(self.item(r, "processes")["fix"], "start the cluster (starting or restarting needs "
                         "the user consent)")
        self.assertEqual(self.item(r, "rest")["status"], "info")
        self.assertEqual(r["next_step"], "start the cluster (starting or restarting needs the user consent), then rerun")
        self.assertEqual(r["error_message"], "1 item(s) missing: processes")
        self.assertIn("NOT READY: 1 item(s) missing", self.run_check()[1])

    def test_running_gfv_cluster_consistent_with_lib(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        self.m.daemon(103, "taskmanager")
        code, _, _, r = self.run_check("--json")  # REST: closed port
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        c = r["cluster"]
        self.assertEqual((c["running"], c["loaded_build"], c["consistent_with_lib"]), (True, "gfv", True))
        self.assertEqual(c["loaded_gfv_jars"], sorted(GFV_JARS))
        self.assertEqual(c["lib_gfv_jars"], sorted(GFV_JARS))
        self.assertEqual([d["pid"] for d in c["jobmanagers"]], [101])
        self.assertEqual([d["pid"] for d in c["taskmanagers"]], [102, 103])
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - DAEMON_AGE))
        self.assertIn(c["jobmanagers"][0]["started"], (started, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - DAEMON_AGE - 1))))
        self.assertNotIn("started_epoch", c["jobmanagers"][0])
        self.assertEqual((c["jars_replaced_after_start"], c["other_flink_daemons"]), ([], 0))
        self.assertEqual((c["flink_home"], c["found_via"], c["lib_version"]), (str(self.m.flink), "FLINK_HOME", "1.19.2"))
        self.assertEqual(self.item(r, "processes")["detail"], "JobManager 101 and 2 TaskManager(s) of this installation")
        self.assertEqual(self.item(r, "loaded-build")["detail"], "every daemon carries " + ", ".join(sorted(GFV_JARS)))
        self.assertIn("lib/ is unchanged since the cluster started (", self.item(r, "lib-vs-cluster")["detail"])
        self.assertEqual(self.item(r, "rest")["status"], "warn")
        self.assertIn("does not answer although the daemons are up", self.item(r, "rest")["detail"])
        self.assertEqual(r["next_step"], "ready: the cluster runs the gfv build that is in lib (warnings above)")
        self.assertIn("cluster     OK       processes: JobManager 101 and 2 TaskManager(s)", self.run_check()[1])

    def test_running_native_cluster(self):
        self.m.flink_home("native")
        self.m.daemon(101, "jobmanager", gfv=[])
        self.m.daemon(102, "taskmanager", gfv=[])
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["cluster"]["loaded_build"], r["cluster"]["consistent_with_lib"]), (True, "native", True))
        self.assertEqual(self.item(r, "loaded-build")["detail"], "every daemon carries no gluten/velox jar on "
                         "its classpath (native build)")

    def test_rest_overview_ok(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        self.m.daemon(103, "taskmanager")
        url = self.rest().url
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "ok"))
        c = r["cluster"]
        self.assertEqual((c["rest_url"], c["rest_reachable"], c["rest_version"], c["rest_taskmanagers"], c["rest_jobs_running"]),
                         (url, True, "1.19.2", 2, 0))
        self.assertEqual(self.item(r, "rest")["detail"], f"REST {url} answers: Flink 1.19.2, 2 TaskManager(s), 0 job(s) running")
        self.assertEqual(r["next_step"], "ready: the cluster runs the gfv build that is in lib")

    def test_rest_version_mismatch_and_fewer_taskmanagers_and_jobs_warn(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        self.m.daemon(103, "taskmanager")
        self.rest({"flink-version": "1.18.1", "taskmanagers": 1, "jobs-running": 3})
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        rest = [i for i in r["items"] if i["name"] == "rest"]
        self.assertEqual([i["status"] for i in rest], ["ok", "warn", "warn"])
        self.assertEqual(rest[1]["detail"], "the REST endpoint reports Flink 1.18.1 while " + str(self.m.flink)
                         + " is 1.19.2 — it is not this installation")
        self.assertEqual(rest[2]["detail"], "2 TaskManager(s) of this installation run but REST lists 1 — still "
                         "registering, or registered with another JobManager")
        jobs = self.item(r, "jobs")
        self.assertEqual((jobs["status"], jobs["detail"]), ("warn", "3 job(s) are running — a restart would interrupt them"))
        self.assertIn("warnings above", r["next_step"])

    def test_rest_answers_while_nothing_runs_is_a_warning(self):
        self.m.flink_home("gfv")
        self.rest()
        _, _, _, r = self.run_check("--json")
        rest = self.item(r, "rest")
        self.assertEqual(rest["status"], "warn")
        self.assertIn("but no daemon of this installation runs here", rest["detail"])

    def test_rest_ignores_environment_proxies(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        self.rest()
        garbage = GarbageServer()
        self.addCleanup(garbage.close)
        code, _, _, r = self.run_check("--json", env=self.m.env(
            http_proxy=garbage.url, HTTP_PROXY=garbage.url, https_proxy=garbage.url, HTTPS_PROXY=garbage.url,
            all_proxy=garbage.url, ALL_PROXY=garbage.url))
        self.assertEqual((code, r["cluster"]["rest_reachable"], r["cluster"]["rest_version"]), (0, True, "1.19.2"))

    def test_rest_garbage_port_is_unreachable(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        garbage = GarbageServer()
        self.addCleanup(garbage.close)
        self.m.rest_url = garbage.url
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["cluster"]["rest_reachable"], self.item(r, "rest")["status"]), (False, "warn"))

    def test_rest_url_from_conf_when_no_env(self):
        self.m.flink_home("gfv")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_REST_URL=None))
        self.assertEqual(r["cluster"]["rest_url"], "http://localhost:8081")
        self.m.write(self.m.flink / "conf" / "config.yaml", "rest:\n  address: 10.0.0.9\n  port: 8159\n")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_REST_URL=None))
        self.assertEqual(r["cluster"]["rest_url"], "http://10.0.0.9:8159")

    def test_mixed_daemons_fail(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager", gfv=GFV_JARS)
        self.m.daemon(102, "taskmanager", gfv=[])
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["cluster"]["loaded_build"]), (1, False, "mixed"))
        lb = self.item(r, "loaded-build")
        self.assertEqual(lb["status"], "fail")
        self.assertIn("the daemons disagree", lb["detail"])
        self.assertEqual(r["next_step"], "restart the cluster as a whole so every daemon loads the same build "
                         "(a restart needs the user consent), then rerun")

    def test_jar_replaced_after_start_fails(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        self.m.age(self.m.lib / "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", 60)  # redeployed after the start
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["cluster"]["consistent_with_lib"]), (1, False, False))
        self.assertEqual(r["cluster"]["jars_replaced_after_start"], ["gluten-flink-runtime-1.9.0-SNAPSHOT.jar"])
        lc = self.item(r, "lib-vs-cluster")
        self.assertEqual(lc["detail"], "lib/ changed since the cluster started (replaced after the start: "
                         "gluten-flink-runtime-1.9.0-SNAPSHOT.jar) — the cluster still runs what it loaded")
        self.assertEqual(r["next_step"], "restart the cluster so it loads what is in lib now (a restart needs "
                         "the user consent), then rerun")
        self.assertIn("        fix: restart the cluster so it loads what is in lib now", self.run_check()[1])

    def test_loaded_jars_differ_from_disk_fails(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager", gfv=[])
        self.m.daemon(102, "taskmanager", gfv=[])
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["cluster"]["loaded_build"], r["cluster"]["consistent_with_lib"]), (1, "native", False))
        self.assertEqual(self.item(r, "lib-vs-cluster")["detail"], "lib/ changed since the cluster started (on disk "
                         + ", ".join(sorted(GFV_JARS)) + "; loaded none) — the cluster still runs what it loaded")

    def test_jobmanager_without_taskmanager_fails(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        proc = self.item(r, "processes")
        self.assertEqual(proc["detail"], "JobManager 101 runs but no TaskManager of this installation does — jobs cannot start")
        self.assertEqual(r["next_step"], proc["fix"] + ", then rerun")

    def test_taskmanagers_without_jobmanager_fail(self):
        self.m.flink_home("gfv")
        self.m.daemon(102, "taskmanager")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.item(r, "processes")["detail"], "1 TaskManager(s) of this installation run without "
                         "a JobManager — leftover processes")
        self.assertIn("stop the leftover TaskManagers", r["next_step"])

    def test_other_installations_are_counted_not_attributed(self):
        self.m.flink_home("gfv")
        other = self.tmp / "other-flink" / "lib" / "flink-dist-1.18.1.jar"
        self.m.write(other, "jar\n")
        self.m.daemon(201, "jobmanager", dist=other)
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["cluster"]["running"], r["cluster"]["other_flink_daemons"]), (False, 1))
        other_item = self.item(r, "other-clusters")
        self.assertEqual(other_item["status"], "info")
        self.assertIn("1 Flink daemon(s) of other installations", other_item["detail"])

    def test_no_flink_home(self):
        code, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.item(r, "home")["status"], "fail")
        self.assertEqual(r["cluster"]["rest_url"], "http://localhost:8081")
        self.assertTrue(r["next_step"].startswith("install the official binary distribution"))

    def test_json_contract_shape_and_crash_shape(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "cluster", "items", "missing", "next_step", "error_message"})
        self.assertEqual(set(r["cluster"]), {"flink_home", "found_via", "running", "jobmanagers", "taskmanagers",
                                             "other_flink_daemons", "loaded_build", "loaded_gfv_jars", "lib_gfv_jars",
                                             "lib_version", "earliest_start", "jars_replaced_after_start",
                                             "consistent_with_lib", "rest_url", "rest_reachable", "rest_version",
                                             "rest_taskmanagers", "rest_jobs_running"})
        self.assertEqual(set(r["cluster"]["jobmanagers"][0]), {"pid", "gfv_jars", "started"})
        module = load_module()

        def boom():
            raise RuntimeError("probe exploded")

        module.find_flink_home = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["status"], a["error_message"][:15]), (2, False, "fail", "check aborted: "))
        self.assertEqual(set(a), set(r))
        self.assertEqual(set(a["cluster"]), set(r["cluster"]))
        self.assertEqual((a["items"], a["missing"], a["cluster"]["running"]), ([], [], False))
        self.assertTrue(a["next_step"].startswith("fix the check error first"))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        self.m.daemon(102, "taskmanager")
        archive = self.tmp / "project" / "cluster-check.json"
        _, stdout, _, _ = self.run_check("--json", "--out", str(archive))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), json.loads(stdout))
        first = archive.read_text(encoding="utf-8")
        code, _, err, _ = self.run_check("--json", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        outside = self.tmp / "outside.json"
        code, _, err, _ = self.run_check("--json", "--out", str(outside), cwd=str(elsewhere))
        self.assertEqual(code, 2)
        self.assertIn("inside the project directory", err)
        target = self.tmp / "keep.json"
        target.write_text("keep me\n", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        code, _, err, _ = self.run_check("--json", "--out", str(link))
        self.assertEqual((code, target.read_text(encoding="utf-8")), (2, "keep me\n"))
        self.assertIn("not a symlink", err)
        self.assertEqual(self.run_check("--json", "--out", str(archive), "--overwrite")[0], 0)

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--bogus")
        self.assertEqual((code, r), (2, None))
        self.assertIn("usage", err.lower())

    def test_check_leaves_machine_untouched(self):
        self.m.flink_home("gfv")
        self.m.daemon(101, "jobmanager")
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"case": "cluster-contract", "skill": "flink-velox-cluster-check",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
