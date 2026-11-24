#!/usr/bin/env python3
"""jdk-contract — tier-A case for flink-velox-jdk-check.

Drives scripts/jdk_check.py against a fake machine laid out in a scratch
directory — a sysroot (two JDKs, a Flink installation with conf/config.yaml or
conf/flink-conf.yaml), a fake PATH with nothing on it (or a java symlink), a fake
/proc with Flink daemon cmdlines — with nothing from the real machine in play; the
script's seams ENV_CHECK_SYSROOT and ENV_CHECK_PROC_ROOT carry the fake. Pins the
verdict contract (exit codes, success, status, missing, next_step for both goals),
every probe rule (JDK 17 via JAVA_HOME / PATH / /usr/lib/jvm, JAVA_HOME verdicts,
conf env.java.home > JAVA_HOME > PATH, daemon attribution by flink-dist path, the
merged single verdict when running == startup, Java 17 required only for goal=build,
no Flink installation = warning), the JSON shape (also for a crash), --out safety
(inside the project, no symlink, no silent overwrite) and that a check leaves the
machine untouched.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import stat
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "jdk_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_jdk_check_result"

JM_CLASS = "org.apache.flink.runtime.entrypoint.StandaloneSessionClusterEntrypoint"
TM_CLASS = "org.apache.flink.runtime.taskexecutor.TaskManagerRunner"


def load_module():
    """jdk_check.py as an in-process module (for the crash path a subprocess
    cannot be driven into hermetically)."""
    spec = importlib.util.spec_from_file_location("jdk_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    """A whole machine in a directory: sysroot, fake PATH, fake /proc."""

    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.bin = self.root / "bin"
        self.proc = self.root / "proc"
        self.bin.mkdir(parents=True)
        self.jdk17 = self.sys / "usr" / "lib" / "jvm" / "java-17-fake"
        self.jdk8 = self.sys / "usr" / "lib" / "jvm" / "java-1.8.0-openjdk-fake"
        self.flink = self.sys / "opt" / "flink"
        self.lib = self.flink / "lib"

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def jdk(self, home, version, release=True):
        self.script(home / "bin" / "java",
                    f'if [ "$1" = "-version" ]; then echo \'openjdk version "{version}"\' >&2; fi\n'
                    "exit 0\n")
        self.script(home / "bin" / "javac", "exit 0\n")
        if release:
            self.write(home / "release", f'JAVA_VERSION="{version}"\nIMPLEMENTOR="fake"\n')

    def flink_home(self, conf="none", env_java_home=None, also=None):
        """bin/flink plus one conf layout: none, config.yaml (nested rest block) or
        flink-conf.yaml (flat keys); also = (name, text) of a second conf file next
        to it."""
        self.script(self.flink / "bin" / "flink", "exit 0\n")
        self.write(self.lib / "flink-dist-1.19.2.jar", "not a real jar\n")
        for name in ("config.yaml", "flink-conf.yaml"):
            (self.flink / "conf" / name).unlink(missing_ok=True)
        text = ""
        if conf == "config.yaml":
            text, name = "rest:\n  address: localhost\n  port: 8081\n", "config.yaml"
        elif conf == "flink-conf.yaml":
            text, name = "rest.address: localhost\nrest.port: 8081\n", "flink-conf.yaml"
        else:
            name = None
        if env_java_home:
            text += f"env.java.home: {env_java_home}\n"
            name = name or "config.yaml"
        if name:
            self.write(self.flink / "conf" / name, text)
        if also:
            self.write(self.flink / "conf" / also[0], also[1])

    def daemon(self, pid, java_home, role="jobmanager", dist=None):
        """A Flink daemon in the fake /proc: <java>/bin/java -classpath <dist> <entry>."""
        entry = JM_CLASS if role == "jobmanager" else TM_CLASS
        dist = dist or (self.lib / "flink-dist-1.19.2.jar")
        argv = [str(Path(java_home) / "bin" / "java"), "-classpath", str(dist), entry]
        self.write(self.proc / str(pid) / "cmdline", "\0".join(argv) + "\0")

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "ENV_CHECK_SYSROOT": str(self.sys),
               "ENV_CHECK_PROC_ROOT": str(self.proc), "HOME": str(self.root), "LC_ALL": "C",
               "FLINK_HOME": str(self.flink), "JAVA_HOME": str(self.jdk17)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def snapshot(self):
        """(path -> kind, mode, size, mtime_ns) of every entry under root/."""
        seen = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            base = Path(dirpath)
            for name in dirnames:
                seen[str(base / name)] = ("d", (base / name).lstat().st_mode)
            for name in filenames:
                st = (base / name).lstat()
                seen[str(base / name)] = ("f", st.st_mode, st.st_size, st.st_mtime_ns)
        return seen


class JdkCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def ready(self, daemons=(), conf="config.yaml"):
        """JDK 17 + JDK 8 installed, JAVA_HOME = JDK 17, a Flink home, optional daemons."""
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.jdk(self.m.jdk8, "1.8.0_412")
        self.m.flink_home(conf=conf)
        for pid, java_home, role in daemons:
            self.m.daemon(pid, java_home, role)

    def run_check(self, *args, env=None, cwd=None):
        """Run jdk_check.py; returns (exit code, stdout, stderr, result or None)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_all_present_build(self):
        self.ready()
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["missing"], r["error_message"], r["goal"]),
                         (0, True, "ok", [], "", "build"))
        self.assertEqual((r["jdk17"]["path"], r["jdk17"]["version"], r["jdk17"]["found_via"]),
                         (str(self.m.jdk17), "17.0.18", "JAVA_HOME"))
        self.assertEqual(r["jdk8"], str(self.m.jdk8))
        self.assertEqual(r["flink_home"], {"path": str(self.m.flink), "found_via": "FLINK_HOME",
                                           "conf_file": str(self.m.flink / "conf" / "config.yaml")})
        self.assertEqual(r["running_javas"], [])
        self.assertEqual((r["cluster_java"]["path"], r["cluster_java"]["major"], r["cluster_java"]["found_via"]),
                         (str(self.m.jdk17), 17, "JAVA_HOME"))
        self.assertEqual([(i["name"], i["status"]) for i in r["items"]],
                         [("jdk17", "ok"), ("JAVA_HOME", "ok"), ("startup", "ok")])
        self.assertEqual(r["next_step"], "ready: the Java for the Flink daemons is in order")
        _, out, _, _ = self.run_check()
        self.assertIn(f"jdk         OK       JDK 17.0.18 at {self.m.jdk17} (via JAVA_HOME); JAVA_HOME ok; "
                      f"startup: Java 17.0.18 ({self.m.jdk17}, via JAVA_HOME)", out)
        self.assertIn("READY: the Java for the Flink daemons is in order", out)
        self.assertEqual(err, "")

    def test_running_daemon_on_the_same_java_merges_into_one_item(self):
        self.ready(daemons=[(101, self.m.jdk17, "jobmanager")])
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(r["running_javas"], [{"path": str(self.m.jdk17), "found_via": "running process (pid 101, jobmanager)",
                                               "version": "17.0.18", "major": 17, "pid": 101, "role": "jobmanager"}])
        self.assertEqual([i["name"] for i in r["items"]], ["jdk17", "JAVA_HOME", "java"])
        self.assertTrue(r["cluster_java"]["found_via"].endswith("; the 1 running daemon(s) use it"))

    def test_running_daemon_on_java8_fails_build(self):
        self.ready(daemons=[(102, self.m.jdk8, "taskmanager")])
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        running = self.item(r, "running")
        self.assertEqual(running["status"], "fail")
        self.assertEqual(running["detail"], "not every running Flink daemon runs a suitable Java: taskmanager pid 102: "
                         f"Java 1.8.0_412 ({self.m.jdk8}) — needs Java 17: the gluten jars carry Java 17 bytecode")
        self.assertEqual(running["fix"], "set env.java.home in the Flink conf to a JDK 17 and restart the cluster "
                         "(a restart needs the user consent)")
        self.assertEqual(self.item(r, "startup")["status"], "ok")
        self.assertEqual(r["missing"], [f"running: {running['detail']}"])
        self.assertEqual(r["error_message"], "1 item(s) missing: running")
        self.assertEqual(r["next_step"], "set env.java.home in the Flink conf to a JDK 17 and restart the "
                         "cluster (a restart needs the user consent), then rerun")
        _, out, _, _ = self.run_check()
        self.assertIn("jdk         FAIL     1 item(s) missing", out)
        self.assertIn("  FAIL  running: not every running Flink daemon runs a suitable Java: taskmanager pid 102: "
                      "Java 1.8.0_412", out)
        self.assertIn("NOT READY for goal 'build': 1 item(s) missing", out)

    def test_every_daemon_is_looked_at_on_its_own(self):
        # the JobManager already moved to Java 17, one TaskManager is still on Java 8:
        # stopping at the first daemon found would call the installation clean
        self.ready(daemons=[(101, self.m.jdk17, "jobmanager"), (102, self.m.jdk8, "taskmanager"),
                            (103, self.m.jdk17, "taskmanager")])
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "1 item(s) missing: running"))
        running = self.item(r, "running")
        self.assertEqual(running["detail"], "not every running Flink daemon runs a suitable Java: "
                         f"jobmanager pid 101: Java 17.0.18 ({self.m.jdk17}); "
                         f"taskmanager pid 102: Java 1.8.0_412 ({self.m.jdk8}) — needs Java 17: the gluten jars carry "
                         f"Java 17 bytecode; taskmanager pid 103: Java 17.0.18 ({self.m.jdk17})")
        self.assertEqual(running["fix"], "set env.java.home in the Flink conf to a JDK 17 and restart the cluster "
                         "(a restart needs the user consent)")
        self.assertEqual([(e["pid"], e["role"], e["major"]) for e in r["running_javas"]],
                         [(101, "jobmanager", 17), (102, "taskmanager", 8), (103, "taskmanager", 17)])
        self.assertEqual((self.item(r, "startup")["status"], r["next_step"]), ("ok", running["fix"] + ", then rerun"))
        report = self.run_check()[1]
        self.assertIn("  FAIL  running: not every running Flink daemon runs a suitable Java: jobmanager pid 101: Java 17.0.18", report)
        self.assertIn("taskmanager pid 102: Java 1.8.0_412", report)
        # all daemons on one and the same Java as the restart would pick: a single verdict, all of them named
        shutil.rmtree(self.m.proc)
        self.ready(daemons=[(101, self.m.jdk17, "jobmanager"), (102, self.m.jdk17, "taskmanager")])
        _, _, _, r = self.run_check("--json")
        self.assertEqual(([i["name"] for i in r["items"]], r["success"]), (["jdk17", "JAVA_HOME", "java"], True))
        self.assertTrue(r["cluster_java"]["found_via"].endswith("; the 2 running daemon(s) use it"))
        self.assertEqual(len(r["running_javas"]), 2)
        # under the native goal both Javas are fine, but they differ: said so, as a warning
        shutil.rmtree(self.m.proc)
        self.ready(daemons=[(101, self.m.jdk17, "jobmanager"), (102, self.m.jdk8, "taskmanager")])
        _, _, _, r = self.run_check("--json", "--goal", "native")
        running = self.item(r, "running")
        self.assertEqual((r["success"], running["status"]), (True, "warn"))
        self.assertEqual(running["detail"], "the running daemons run different Java versions (a restart puts them all "
                         f"on the startup Java): jobmanager pid 101: Java 17.0.18 ({self.m.jdk17}); "
                         f"taskmanager pid 102: Java 1.8.0_412 ({self.m.jdk8})")

    def test_a_java_17_directory_holding_another_jdk_is_not_taken(self):
        decoy = self.m.sys / "usr" / "lib" / "jvm" / "java-17-aaa-decoy"  # sorts first, is not a JDK 17
        self.m.jdk(decoy, "21.0.3")
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.flink_home()
        _, _, _, r = self.run_check("--json", env=self.m.env(JAVA_HOME=None))
        self.assertEqual((r["jdk17"]["path"], r["jdk17"]["version"], r["jdk17"]["found_via"]), (str(self.m.jdk17), "17.0.18", "/usr/lib/jvm"))
        # with nothing else on the machine the decoy is not passed off as a JDK 17 either
        shutil.rmtree(self.m.jdk17)
        code, _, _, r = self.run_check("--json", env=self.m.env(JAVA_HOME=None))
        self.assertEqual((code, r["jdk17"], self.item(r, "jdk17")["status"]), (1, {"path": None, "version": None, "found_via": None}, "fail"))

    def test_native_goal_accepts_java_8_11_17_only(self):
        jdk21 = self.m.sys / "usr" / "lib" / "jvm" / "java-21-fake"
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.jdk(jdk21, "21.0.3")
        self.m.flink_home(env_java_home=self.m.jdk17)  # a restart would pick a fine Java …
        self.m.daemon(105, jdk21, "taskmanager")  # … but one daemon runs Java 21
        code, _, _, r = self.run_check("--json", "--goal", "native")
        self.assertEqual((code, r["success"]), (1, False))
        running, startup = self.item(r, "running"), self.item(r, "startup")
        self.assertEqual(running["detail"], "not every running Flink daemon runs a suitable Java: taskmanager pid 105: "
                         f"Java 21.0.3 ({jdk21}) — Flink 1.19 itself runs on Java 8, 11 or 17")
        self.assertEqual(startup["status"], "ok")
        report = self.run_check("--goal", "native")[1]
        self.assertIn("NOT READY for goal 'native': 1 item(s) missing", report)
        self.assertIn("  FAIL  running: not every running Flink daemon runs a suitable Java: taskmanager pid 105: Java 21.0.3", report)
        shutil.rmtree(self.m.proc)  # no daemon left: only what a restart would pick is judged …
        self.m.flink_home(env_java_home=jdk21)  # … and when that is Java 21 too
        _, _, _, r = self.run_check("--json", "--goal", "native")
        startup = self.item(r, "startup")
        self.assertEqual(startup["detail"], f"Java 21.0.3 ({jdk21}, via flink conf env.java.home): Flink 1.19 itself "
                         "runs on Java 8, 11 or 17, not on it")
        self.assertEqual(r["next_step"], startup["fix"] + ", then rerun")
        self.assertEqual(running["fix"], "set env.java.home in the Flink conf (or JAVA_HOME) to a JDK 8, 11 or 17 "
                         "and restart the cluster (a restart needs the user consent)")
        self.assertEqual(r["next_step"], running["fix"] + ", then rerun")
        self.assertIn("NOT READY for goal 'native': 1 item(s) missing", self.run_check("--goal", "native")[1])
        # the build goal says what it always did
        _, _, _, r = self.run_check("--json")
        self.assertIn("Java 17 bytecode", self.item(r, "startup")["detail"])

    def test_only_the_conf_file_flink_reads_is_used(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.jdk(self.m.jdk8, "1.8.0_412")
        # env.java.home only in config.yaml while flink-conf.yaml exists: Flink never sees it
        self.m.flink_home(conf="flink-conf.yaml", also=("config.yaml", f"rest:\n  port: 8081\nenv.java.home: {self.m.jdk8}\n"))
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["flink_home"]["conf_file"], r["cluster_java"]["found_via"]),
                         (True, str(self.m.flink / "conf" / "flink-conf.yaml"), "JAVA_HOME"))
        # in the file Flink reads: it wins over JAVA_HOME
        self.m.flink_home(conf="config.yaml", env_java_home=self.m.jdk17,
                          also=("flink-conf.yaml", f"rest.port: 8081\nenv.java.home: {self.m.jdk8}\n"))
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["flink_home"]["conf_file"], r["cluster_java"]["found_via"], r["cluster_java"]["major"]),
                         (False, str(self.m.flink / "conf" / "flink-conf.yaml"), "flink conf env.java.home", 8))
        # only config.yaml (what a 1.19 distribution ships): that one is read
        self.m.flink_home(conf="config.yaml", env_java_home=self.m.jdk17)
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["flink_home"]["conf_file"], r["cluster_java"]["found_via"]),
                         (True, str(self.m.flink / "conf" / "config.yaml"), "flink conf env.java.home"))

    def test_native_goal_tolerates_a_java8_daemon(self):
        self.ready(daemons=[(102, self.m.jdk8, "taskmanager")])
        code, _, _, r = self.run_check("--json", "--goal", "native")
        self.assertEqual((code, r["success"], r["goal"]), (0, True, "native"))
        self.assertEqual(self.item(r, "running")["status"], "ok")
        self.assertEqual(self.item(r, "running")["detail"],
                         f"all 1 running daemon(s) run taskmanager pid 102: Java 1.8.0_412 ({self.m.jdk8})")

    def test_conf_env_java_home_beats_java_home(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.jdk(self.m.jdk8, "1.8.0_412")
        self.m.flink_home(conf="flink-conf.yaml", env_java_home=self.m.jdk8)
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual((r["cluster_java"]["path"], r["cluster_java"]["found_via"], r["cluster_java"]["major"]),
                         (str(self.m.jdk8), "flink conf env.java.home", 8))
        self.assertEqual(self.item(r, "startup")["status"], "fail")
        self.assertEqual(r["next_step"], "set env.java.home in the Flink conf to a JDK 17 and restart the "
                         "cluster (a restart needs the user consent), then rerun")
        # nested config.yaml is read the same way: env.java.home as a scalar leaf
        self.m.flink_home(conf="config.yaml", env_java_home=self.m.jdk17)
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["cluster_java"]["found_via"]), (True, "flink conf env.java.home"))

    def test_no_jdk17_anywhere_fails(self):
        self.m.flink_home()
        code, out, _, r = self.run_check("--json", env=self.m.env(JAVA_HOME=None))
        self.assertEqual((code, r["success"]), (1, False))
        jdk = self.item(r, "jdk17")
        self.assertEqual(jdk["status"], "fail")
        self.assertEqual(jdk["fix"], "install a JDK 17 (java-17-openjdk-devel on openEuler/CentOS)")
        self.assertEqual(r["jdk17"], {"path": None, "version": None, "found_via": None})
        self.assertEqual(r["next_step"], "install a JDK 17 (java-17-openjdk-devel on openEuler) "
                         "and point JAVA_HOME at it, then rerun")
        # without JAVA_HOME, PATH java or env.java.home a restart has no java either: a fail
        self.assertEqual(self.item(r, "startup")["status"], "fail")
        _, out, _, _ = self.run_check(env=self.m.env(JAVA_HOME=None))
        self.assertIn("next: install a JDK 17", out)

    def test_java_home_at_jdk8_fails_and_names_the_jdk17(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.jdk(self.m.jdk8, "1.8.0_412")
        self.m.flink_home()
        code, _, _, r = self.run_check("--json", env=self.m.env(JAVA_HOME=self.m.jdk8))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(r["jdk17"]["found_via"], "/usr/lib/jvm")
        jh = self.item(r, "JAVA_HOME")
        self.assertEqual(jh["status"], "fail")
        self.assertIn("JAVA_HOME is JDK 8", jh["detail"])
        self.assertEqual(jh["fix"], f"point JAVA_HOME at the JDK 17 at {self.m.jdk17}")
        self.assertEqual(r["next_step"], f"point JAVA_HOME at the JDK 17 at {self.m.jdk17}, then rerun")

    def test_java_home_pointing_at_a_jre_fails(self):
        self.ready()
        (self.m.jdk17 / "bin" / "javac").unlink()
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("has no bin/javac", self.item(r, "JAVA_HOME")["detail"])

    def test_java_home_unset_is_a_warning_when_the_daemons_still_find_a_java(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.flink_home()
        (self.m.bin / "java").symlink_to(self.m.jdk17 / "bin" / "java")  # the restart falls back to PATH
        env = self.m.env(JAVA_HOME=None)
        code, _, _, r = self.run_check("--json", env=env)
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        jh = self.item(r, "JAVA_HOME")
        self.assertEqual(jh["status"], "warn")
        self.assertEqual(jh["detail"], f"not set; the build falls back to the JDK 17 found at {self.m.jdk17}")
        self.assertEqual((self.item(r, "startup")["status"], r["cluster_java"]["found_via"]), ("ok", "PATH"))
        _, out, _, _ = self.run_check(env=env)
        self.assertIn("JAVA_HOME not set", out)
        self.assertIn("  WARN  JAVA_HOME: not set", out)
        # but with no java on PATH either, a restart has no java: a fail, not a warning
        (self.m.bin / "java").unlink()
        code, _, _, r = self.run_check("--json", env=env)
        self.assertEqual((code, self.item(r, "startup")["status"]), (1, "fail"))

    def test_jdk17_found_via_the_java_on_path(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.flink_home()
        (self.m.bin / "java").symlink_to(self.m.jdk17 / "bin" / "java")
        code, _, _, r = self.run_check("--json", env=self.m.env(JAVA_HOME=None))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual((r["jdk17"]["found_via"], r["jdk17"]["path"]), ("PATH", str(self.m.jdk17)))
        self.assertEqual((r["cluster_java"]["found_via"], r["cluster_java"]["major"]), ("PATH", 17))

    def test_no_flink_home_is_a_warning_not_a_failure(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        code, out, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(r["flink_home"], {"path": None, "found_via": None, "conf_file": None})
        self.assertEqual(r["running_javas"], [])
        flink = self.item(r, "flink")
        self.assertEqual(flink["status"], "warn")
        self.assertIn("conf env.java.home not read", flink["detail"])
        self.assertEqual([i["name"] for i in r["items"]], ["jdk17", "JAVA_HOME", "flink", "startup"])
        _, out, _, _ = self.run_check(env=self.m.env(FLINK_HOME=None))
        self.assertIn("  WARN  flink: Flink installation not found", out)

    def test_flink_home_via_default_location_and_path_symlink(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.flink_home()
        # /opt/flink under the sysroot is a default location
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["flink_home"]["path"], r["flink_home"]["found_via"]), (str(self.m.flink), "default"))
        # a flink on PATH resolves through its symlink to the installation
        (self.m.bin / "flink").symlink_to(self.m.flink / "bin" / "flink")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["flink_home"]["found_via"], r["flink_home"]["path"]), ("PATH", str(self.m.flink)))

    def test_daemon_of_another_installation_is_ignored(self):
        self.ready()
        self.m.daemon(103, self.m.jdk8, "jobmanager",
                      dist=self.tmp / "other-flink" / "lib" / "flink-dist-1.19.2.jar")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(r["running_javas"], [])
        self.assertEqual([i["name"] for i in r["items"]], ["jdk17", "JAVA_HOME", "startup"])

    def test_conf_env_java_home_without_bin_java_fails(self):
        self.m.jdk(self.m.jdk17, "17.0.18")
        self.m.flink_home(env_java_home=self.tmp / "nowhere")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("has no bin/java", self.item(r, "startup")["detail"])
        self.assertEqual(r["next_step"], "set env.java.home in the Flink conf (or JAVA_HOME) to a JDK, then rerun")

    def test_json_contract_shape_and_crash_shape(self):
        self.ready()
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "goal", "status", "jdk17", "jdk8", "java_home",
                                  "flink_home", "cluster_java", "running_javas", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual(set(r["cluster_java"]), {"path", "version", "major", "found_via"})
        self.assertEqual(set(self.item(r, "startup")), {"name", "status", "detail"})
        module = load_module()

        def boom(home, source):
            raise RuntimeError("probe exploded")

        module.describe_java = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["status"], a["error_message"][:15]), (2, False, "fail", "check aborted: "))
        self.assertEqual(set(a), set(r))
        self.assertEqual((a["items"], a["missing"], a["flink_home"], a["running_javas"]),
                         ([], [], {"path": None, "found_via": None, "conf_file": None}, []))
        self.assertTrue(a["next_step"].startswith("fix the check error first"))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.ready()
        archive = self.tmp / "project" / "jdk-check.json"
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
        self.assertFalse(outside.exists())
        target = self.tmp / "keep.json"
        target.write_text("keep me\n", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        code, _, err, _ = self.run_check("--json", "--out", str(link))
        self.assertEqual(code, 2)
        self.assertIn("not a symlink", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(self.run_check("--json", "--out", str(archive), "--overwrite")[0], 0)

    def test_out_never_opens_a_fifo(self):
        self.ready()
        fifo = self.tmp / "project" / "pipe.json"
        fifo.parent.mkdir()
        os.mkfifo(str(fifo))
        # open() on a fifo blocks until a reader appears: the guard must refuse instead
        # (a blocked check would run into the subprocess timeout of run_check and error out)
        code, _, err, _ = self.run_check("--json", "--out", str(fifo), "--overwrite")
        self.assertEqual((code, stat.S_ISFIFO(fifo.lstat().st_mode)), (2, True))
        self.assertIn("--out exists and is not a regular file, refusing to overwrite it", err)
        self.assertIn("READY: the Java for the Flink daemons is in order", self.run_check()[1])  # the verdict itself is unaffected

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--bogus")
        self.assertEqual((code, r), (2, None))
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())
        code, out, err, r = self.run_check("--goal", "whatever")
        self.assertEqual((code, r), (2, None))

    def test_check_leaves_machine_untouched(self):
        self.ready(daemons=[(104, self.m.jdk8, "taskmanager")])
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "jdk-contract",
        "skill": "flink-velox-jdk-check",
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
