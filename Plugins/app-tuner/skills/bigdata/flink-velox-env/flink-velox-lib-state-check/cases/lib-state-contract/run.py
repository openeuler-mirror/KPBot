#!/usr/bin/env python3
"""lib-state-contract — tier-A case for flink-velox-lib-state-check.

Drives scripts/lib_state_check.py against a fake machine laid out in a scratch
directory — a sysroot with a Flink installation whose lib/ holds a chosen set of
jars (native, the complete GlutenFlink set, a partial set, duplicates), a fake PATH with
nothing on it or a flink symlink — with nothing from the real machine in play; the
script's seam ENV_CHECK_SYSROOT carries the fake. Pins the verdict contract (exit
codes, success, status, missing, next_step for every --expect), every probe rule
(native / gfv / partial classification, the 14 runtime dependencies, missing
parts, duplicates, installation lookup), the JSON shape (also for a crash), --out
safety (inside the project, no symlink, no silent overwrite) and that a check
leaves the machine untouched.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "lib_state_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_lib_state_check_result"

# the GlutenFlink set as the build script deploys it (planner/runtime/loader + velox4j)
GFV_JARS = [
    "gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar",
    "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar",
]
RUNTIME_DEP_JARS = [
    "guava-33.4.0-jre.jar", "failureaccess-1.0.2.jar", "arrow-c-data-18.1.0.jar",
    "arrow-format-18.1.0.jar", "arrow-memory-core-18.1.0.jar", "arrow-memory-unsafe-18.1.0.jar",
    "arrow-vector-18.1.0.jar", "commons-io-2.18.0.jar", "flatbuffers-java-24.3.25.jar",
    "jackson-annotations-2.18.0.jar", "jackson-core-2.18.0.jar", "jackson-databind-2.18.0.jar",
    "jackson-datatype-jdk8-2.18.0.jar", "jackson-datatype-jsr310-2.18.0.jar",
]
# ... and how the scan names a missing one (artifactId, the version stripped)
RUNTIME_DEP_IDS = ("guava", "failureaccess", "arrow-c-data", "arrow-format", "arrow-memory-core",
                   "arrow-memory-unsafe", "arrow-vector", "commons-io", "flatbuffers-java",
                   "jackson-annotations", "jackson-core", "jackson-databind", "jackson-datatype-jdk8",
                   "jackson-datatype-jsr310")


def load_module():
    spec = importlib.util.spec_from_file_location("lib_state_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.flink = self.sys / "opt" / "flink"
        self.local = self.sys / "usr" / "local" / "flink"
        self.lib = self.flink / "lib"

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def flink_home(self, home=None):
        home = Path(home) if home else self.flink
        self.write(home / "bin" / "flink", "#!/bin/sh\nexit 0\n", 0o755)
        return home

    def lib_jars(self, build):
        self.lib.mkdir(parents=True, exist_ok=True)
        for stale in self.lib.glob("*.jar"):
            stale.unlink()
        names = {"native": ["flink-dist-1.19.2.jar", "flink-table-1.19.2.jar"],
                 "gfv": GFV_JARS + RUNTIME_DEP_JARS,
                 "partial": GFV_JARS + RUNTIME_DEP_JARS[:10],
                 "partial-runtime-only": ["gluten-flink-runtime-1.9.0-SNAPSHOT.jar"]}[build]
        for name in names:
            self.write(self.lib / name, "jar\n")

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "ENV_CHECK_SYSROOT": str(self.sys), "HOME": str(self.root),
               "LC_ALL": "C", "FLINK_HOME": str(self.flink)}
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


class LibStateCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def run_check(self, *args, env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd), stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_native_lib_is_ok(self):
        self.m.flink_home()
        self.m.lib_jars("native")
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["expect"]), (0, True, "ok", "any"))
        s = r["lib_state"]
        self.assertEqual((s["build"], s["lib"], s["gfv_jars"], s["missing_parts"], s["duplicates"]),
                         ("native", str(self.m.lib), [], [], {}))
        self.assertEqual(s["runtime_deps"], {"expected": 14, "missing": list(RUNTIME_DEP_IDS)})
        self.assertEqual((s["flink_home"], s["found_via"]), (str(self.m.flink), "FLINK_HOME"))
        self.assertEqual(self.item(r, "build")["detail"], "no gluten/velox jar in lib — the native build "
                         "(same criterion as the lib gate of flink-velox-e2e-verify)")
        self.assertEqual(r["next_step"], "ready: lib holds the native build")
        _, out, _, _ = self.run_check()
        self.assertIn(f"lib-state   OK       native build in {self.m.lib}: no gluten/velox jar in lib", out)
        self.assertIn("READY: lib holds the native build", out)
        self.assertEqual(err, "")

    def test_complete_gfv_set_is_ok(self):
        self.m.flink_home()
        self.m.lib_jars("gfv")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        s = r["lib_state"]
        self.assertEqual(s["build"], "gfv")
        self.assertEqual(s["gfv_jars"], sorted(GFV_JARS))
        self.assertEqual(s["gluten_jars"], {"gluten-flink-planner": ["gluten-flink-planner-1.9.0-SNAPSHOT.jar"],
                                            "gluten-flink-runtime": ["gluten-flink-runtime-1.9.0-SNAPSHOT.jar"],
                                            "gluten-flink-loader": ["gluten-flink-loader-1.9.0-SNAPSHOT.jar"]})
        self.assertEqual(s["velox4j_jars"], ["velox4j-0.1.0-SNAPSHOT.jar"])
        self.assertEqual(s["runtime_deps"]["missing"], [])
        self.assertEqual(self.item(r, "build")["detail"],
                         "complete GlutenFlink set: planner runtime loader + velox4j + 14 runtime deps")
        self.assertEqual(r["next_step"], "ready: lib holds the gfv build")

    def test_partial_set_fails_and_names_the_missing_parts(self):
        self.m.flink_home()
        self.m.lib_jars("partial")
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        s = r["lib_state"]
        self.assertEqual(s["build"], "partial")
        self.assertEqual(s["missing_parts"], list(RUNTIME_DEP_IDS[10:]))
        build = self.item(r, "build")
        self.assertEqual(build["detail"], "an incomplete GlutenFlink set is in lib: missing jackson-core, "
                         "jackson-databind, jackson-datatype-jdk8, jackson-datatype-jsr310 "
                         "(4 gluten/velox jar(s): gluten-flink-loader-1.9.0-SNAPSHOT.jar, "
                         "gluten-flink-planner-1.9.0-SNAPSHOT.jar, gluten-flink-runtime-1.9.0-SNAPSHOT.jar, "
                         "velox4j-0.1.0-SNAPSHOT.jar) — half a stack fails at class-loading")
        self.assertEqual(build["fix"], "deploy the complete GlutenFlink build into lib, or remove every gluten/velox "
                         "jar, then restart the cluster (a restart needs the user consent)")
        self.assertEqual(r["next_step"], "deploy the complete GlutenFlink build into lib (missing jackson-core, "
                         "jackson-databind, jackson-datatype-jdk8, jackson-datatype-jsr310) or remove every "
                         "gluten/velox jar, then restart the cluster (a restart needs the user consent), then rerun")
        self.assertEqual(r["error_message"], "1 item(s) missing: build")
        _, out, _, _ = self.run_check()
        self.assertIn("lib-state   FAIL     1 item(s) missing", out)
        self.assertIn("        fix: deploy the complete GlutenFlink build into lib", out)

    def test_partial_with_only_the_runtime_jar(self):
        self.m.flink_home()
        self.m.lib_jars("partial-runtime-only")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(r["lib_state"]["build"], "partial")
        self.assertEqual(r["lib_state"]["missing_parts"][:4],
                         ["velox4j", "gluten-flink-planner", "gluten-flink-loader", "guava"])

    def test_expect_native_rejects_gfv_and_partial(self):
        self.m.flink_home()
        self.m.lib_jars("gfv")
        code, _, _, r = self.run_check("--json", "--expect", "native")
        self.assertEqual((code, r["success"], r["expect"]), (1, False, "native"))
        build = self.item(r, "build")
        self.assertEqual(build["status"], "fail")
        self.assertEqual(build["fix"], "move every gluten/velox jar out of lib and restart the cluster "
                         "(a restart needs the user consent)")
        self.assertEqual(r["next_step"], "move every gluten/velox jar out of lib and restart the cluster "
                         "(a restart needs the user consent), then rerun")
        self.m.lib_jars("partial")
        _, _, _, r = self.run_check("--json", "--expect", "native")
        self.assertEqual(r["lib_state"]["build"], "partial")  # partial wins over the expectation
        self.assertIn("deploy the complete GlutenFlink build", r["next_step"])

    def test_expect_gfv_rejects_native(self):
        self.m.flink_home()
        self.m.lib_jars("native")
        code, _, _, r = self.run_check("--json", "--expect", "gfv")
        self.assertEqual((code, r["success"]), (1, False))
        build = self.item(r, "build")
        self.assertEqual(build["status"], "fail")
        self.assertEqual(build["fix"], "build velox4j and gluten-flink and deploy the complete set into lib, "
                         "then restart the cluster (a restart needs the user consent)")
        self.assertEqual(r["next_step"], "build velox4j and gluten-flink and deploy the complete set into lib, "
                         "then restart the cluster (a restart needs the user consent), then rerun")
        # and accepts the complete set, saying so
        self.m.lib_jars("gfv")
        _, _, _, r = self.run_check("--json", "--expect", "gfv")
        self.assertEqual((r["success"], r["next_step"]), (True, "ready: lib holds the gfv build as expected"))

    def test_duplicate_versions_warn_without_failing(self):
        self.m.flink_home()
        self.m.lib_jars("gfv")
        self.m.write(self.m.lib / "gluten-flink-runtime-1.8.0-SNAPSHOT.jar", "old\n")
        self.m.write(self.m.lib / "velox4j-0.0.9-SNAPSHOT.jar", "old\n")
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        self.assertEqual(r["lib_state"]["duplicates"], {
            "gluten-flink-runtime": ["gluten-flink-runtime-1.8.0-SNAPSHOT.jar", "gluten-flink-runtime-1.9.0-SNAPSHOT.jar"],
            "velox4j": ["velox4j-0.0.9-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar"]})
        dups = [i for i in r["items"] if i["name"] == "duplicates"]
        self.assertEqual([d["detail"].split(" in lib:")[0] for d in dups],
                         ["several versions of gluten-flink-runtime", "several versions of velox4j"])
        self.assertEqual(r["next_step"], "remove the stale duplicate jars (gluten-flink-runtime: "
                         "gluten-flink-runtime-1.8.0-SNAPSHOT.jar, gluten-flink-runtime-1.9.0-SNAPSHOT.jar; "
                         "velox4j: velox4j-0.0.9-SNAPSHOT.jar, velox4j-0.1.0-SNAPSHOT.jar) and restart the "
                         "cluster (a restart needs the user consent), then rerun")
        self.assertIn("  WARN  duplicates: several versions of velox4j", self.run_check()[1])

    def test_two_versions_of_a_runtime_dependency_warn_too(self):
        # an upgrade that left the old guava next to the new one: which of them loads is decided
        # by classpath order — the same reason duplicates of the gluten jars warn
        self.m.flink_home()
        self.m.lib_jars("gfv")
        self.m.write(self.m.lib / "guava-31.1-jre.jar", "old\n")
        self.m.write(self.m.lib / "jackson-core-2.15.0.jar", "old\n")
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        self.assertEqual(r["lib_state"]["duplicates"], {
            "guava": ["guava-31.1-jre.jar", "guava-33.4.0-jre.jar"],
            "jackson-core": ["jackson-core-2.15.0.jar", "jackson-core-2.18.0.jar"]})
        dups = [i for i in r["items"] if i["name"] == "duplicates"]
        self.assertEqual([d["detail"] for d in dups], [
            "several versions of guava in lib: guava-31.1-jre.jar, guava-33.4.0-jre.jar — remove the stale ones "
            "and restart the cluster (a restart needs the user consent)",
            "several versions of jackson-core in lib: jackson-core-2.15.0.jar, jackson-core-2.18.0.jar — remove "
            "the stale ones and restart the cluster (a restart needs the user consent)"])
        self.assertIn("remove the stale duplicate jars (guava: guava-31.1-jre.jar, guava-33.4.0-jre.jar; "
                      "jackson-core: jackson-core-2.15.0.jar, jackson-core-2.18.0.jar)", r["next_step"])
        self.assertIn("  WARN  duplicates: several versions of guava", self.run_check()[1])
        # the build itself is still complete: a warning, not a failure
        self.assertEqual((r["lib_state"]["build"], r["lib_state"]["missing_parts"]), ("gfv", []))

    def test_no_flink_home(self):
        code, out, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((code, r["success"]), (1, False))
        home = self.item(r, "home")
        self.assertEqual(home["status"], "fail")
        self.assertIn("Flink not found", home["detail"])
        self.assertEqual(r["lib_state"]["build"], None)
        self.assertEqual(r["next_step"], home["fix"] + ", then rerun")

    def test_installation_lookup_default_and_path(self):
        self.m.flink_home()
        self.m.lib_jars("native")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["lib_state"]["found_via"], r["lib_state"]["flink_home"]), ("default", f"{self.m.sys}/opt/flink"))
        self.m.flink_home(home=self.m.local)
        self.m.lib = self.m.local / "lib"  # the PATH-found installation has its own lib/
        self.m.lib_jars("native")
        (self.m.bin / "flink").symlink_to(self.m.local / "bin" / "flink")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["lib_state"]["found_via"], r["lib_state"]["lib"]), ("PATH", str(self.m.local / "lib")))

    def test_home_without_lib_dir(self):
        self.m.flink_home()
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.item(r, "lib")["status"], "fail")
        self.assertIn("has no lib/ directory", self.item(r, "lib")["detail"])

    def test_json_contract_shape_and_crash_shape(self):
        self.m.flink_home()
        self.m.lib_jars("gfv")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "expect", "status", "lib_state", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual(set(r["lib_state"]), {"build", "lib", "flink_home", "found_via", "gfv_jars", "gluten_jars",
                                               "velox4j_jars", "runtime_deps", "duplicates", "missing_parts"})
        module = load_module()

        def boom(expect):
            raise RuntimeError("probe exploded")

        module.find_flink_home = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["status"], a["error_message"][:15]), (2, False, "fail", "check aborted: "))
        self.assertEqual(set(a), set(r))
        self.assertEqual(set(a["lib_state"]), set(r["lib_state"]))
        self.assertEqual((a["items"], a["missing"]), ([], []))
        self.assertTrue(a["next_step"].startswith("fix the check error first"))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.m.flink_home()
        self.m.lib_jars("gfv")
        archive = self.tmp / "project" / "lib-state-check.json"
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

    def test_out_never_opens_a_fifo(self):
        self.m.flink_home()
        self.m.lib_jars("native")
        fifo = self.tmp / "project" / "pipe.json"
        fifo.parent.mkdir()
        os.mkfifo(str(fifo))
        # open() on a fifo blocks until a reader appears: the guard must refuse instead
        # (a blocked check would run into the subprocess timeout of run_check and error out)
        code, _, err, _ = self.run_check("--json", "--out", str(fifo), "--overwrite")
        self.assertEqual((code, stat.S_ISFIFO(fifo.lstat().st_mode)), (2, True))
        self.assertIn("--out exists and is not a regular file, refusing to overwrite it", err)
        self.assertIn("READY", self.run_check()[1])  # the verdict itself is unaffected

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--expect", "whatever")
        self.assertEqual((code, r), (2, None))
        self.assertIn("usage", err.lower())

    def test_check_leaves_machine_untouched(self):
        self.m.flink_home()
        self.m.lib_jars("partial")
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"case": "lib-state-contract", "skill": "flink-velox-lib-state-check",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
