#!/usr/bin/env python3
"""nexmark-contract — tier-A case for flink-velox-nexmark-check.

Drives scripts/nexmark_check.py against a fake machine laid out in a scratch
directory — a sysroot with a Flink installation whose lib/ may hold nexmark jars,
and a runnable nexmark tree (bin/run_query.sh) under a chosen location — with
nothing from the real machine in play; the script's seam ENV_CHECK_SYSROOT
carries the fake. Pins the verdict contract (exit codes, success, status,
missing, next_step), every probe rule (jar lookup in lib incl. several jars,
runnable tree via $NEXMARK_HOME / /usr/local/nexmark / /opt/nexmark in that
order, no Flink installation), the JSON shape (also for a crash), --out safety
(inside the project, no symlink, no silent overwrite) and that a check leaves
the machine untouched.

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
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "nexmark_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_nexmark_check_result"


def load_module():
    spec = importlib.util.spec_from_file_location("nexmark_check_under_test", str(SCRIPT))
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
        self.lib = self.flink / "lib"
        self.nexmark_local = self.sys / "usr" / "local" / "nexmark"
        self.nexmark_opt = self.sys / "opt" / "nexmark"

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body="exit 0\n"):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def flink_home(self):
        self.script(self.flink / "bin" / "flink")
        self.write(self.lib / "flink-dist-1.19.2.jar", "jar\n")

    def nexmark_jars(self, *names):
        for stale in self.lib.glob("*nexmark*.jar"):
            stale.unlink()
        for name in names:
            self.write(self.lib / name, "jar\n")

    def nexmark_tree(self, home, runnable=True):
        home = Path(home)
        if runnable:
            self.script(home / "bin" / "run_query.sh")
        self.write(home / "queries" / "q0.sql", "SELECT 1;\n")
        return home

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "ENV_CHECK_SYSROOT": str(self.sys), "HOME": str(self.root),
               "LC_ALL": "C", "FLINK_HOME": str(self.flink), "NEXMARK_HOME": str(self.nexmark_local)}
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


class NexmarkCase(unittest.TestCase):
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

    def ready(self):
        self.m.flink_home()
        self.m.nexmark_jars("nexmark-flink-0.3-SNAPSHOT.jar")
        self.m.nexmark_tree(self.m.nexmark_local)

    def test_jar_and_tree_present(self):
        self.ready()
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["missing"]), (0, True, "ok", []))
        n = r["nexmark"]
        self.assertEqual((n["jar"], n["jars"], n["home"], n["home_via"], n["found_via"]),
                         ("nexmark-flink-0.3-SNAPSHOT.jar", ["nexmark-flink-0.3-SNAPSHOT.jar"],
                          str(self.m.nexmark_local), "$NEXMARK_HOME", "FLINK_HOME"))
        self.assertEqual([(i["name"], i["status"]) for i in r["items"]], [("jar", "ok"), ("home", "ok")])
        self.assertEqual(self.item(r, "home")["detail"], f"runnable tree at {self.m.nexmark_local} (via $NEXMARK_HOME)")
        self.assertEqual(r["next_step"], "ready: nexmark jar and runnable tree are in place")
        _, out, _, _ = self.run_check()
        self.assertIn("nexmark     OK       jar: nexmark-flink-0.3-SNAPSHOT.jar in lib; home: runnable tree at", out)
        self.assertIn("READY: nexmark jar and runnable tree are in place", out)
        self.assertEqual(err, "")

    def test_tree_lookup_order(self):
        self.m.flink_home()
        self.m.nexmark_jars("nexmark-flink-0.3-SNAPSHOT.jar")
        # no NEXMARK_HOME: /usr/local/nexmark before /opt/nexmark
        self.m.nexmark_tree(self.m.nexmark_opt)
        _, _, _, r = self.run_check("--json", env=self.m.env(NEXMARK_HOME=None))
        self.assertEqual((r["nexmark"]["home"], r["nexmark"]["home_via"]), (str(self.m.nexmark_opt), "default"))
        self.m.nexmark_tree(self.m.nexmark_local)
        _, _, _, r = self.run_check("--json", env=self.m.env(NEXMARK_HOME=None))
        self.assertEqual(r["nexmark"]["home"], str(self.m.nexmark_local))
        # $NEXMARK_HOME wins when it holds a runnable tree
        other = self.tmp / "other-nexmark"
        self.m.nexmark_tree(other)
        _, _, _, r = self.run_check("--json", env=self.m.env(NEXMARK_HOME=str(other)))
        self.assertEqual((r["nexmark"]["home"], r["nexmark"]["home_via"]), (str(other), "$NEXMARK_HOME"))
        # $NEXMARK_HOME pointing at a tree without bin/run_query.sh falls through to the defaults
        self.m.write(other / "bin" / "run_query.sh", "not executable\n", 0o644)
        _, _, _, r = self.run_check("--json", env=self.m.env(NEXMARK_HOME=str(other)))
        self.assertEqual(r["nexmark"]["home"], str(self.m.nexmark_local))

    def test_missing_jar(self):
        self.m.flink_home()
        self.m.nexmark_tree(self.m.nexmark_local)
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        jar = self.item(r, "jar")
        self.assertEqual(jar["detail"], f"no nexmark jar in {self.m.lib}")
        self.assertEqual(jar["fix"], "build nexmark-flink from https://github.com/nexmark/nexmark "
                         "(mvn -pl nexmark-flink -am package -DskipTests) and copy the jar into lib")
        self.assertEqual(self.item(r, "home")["status"], "ok")
        self.assertEqual(r["nexmark"]["jar"], None)
        self.assertEqual(r["next_step"], "nexmark is not ready: build the nexmark-flink jar "
                         "(mvn -pl nexmark-flink -am package -DskipTests) and copy it into lib, then rerun")
        self.assertEqual(r["error_message"], "1 item(s) missing: jar")
        self.assertIn("        fix: build nexmark-flink from", self.run_check()[1])

    def test_missing_tree(self):
        self.m.flink_home()
        self.m.nexmark_jars("nexmark-flink-0.3-SNAPSHOT.jar")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        home = self.item(r, "home")
        self.assertEqual(home["detail"], "no runnable nexmark tree (bin/run_query.sh) under $NEXMARK_HOME, "
                         "/usr/local/nexmark or /opt/nexmark")
        self.assertEqual(home["fix"], "copy nexmark-flink/src/main/resources of the nexmark checkout as the "
                         "runnable tree and point NEXMARK_HOME at it")
        self.assertEqual(r["next_step"], "nexmark is not ready: lay out the runnable nexmark tree "
                         "(nexmark-flink/src/main/resources) and point NEXMARK_HOME at it, then rerun")

    def test_both_missing_names_both_actions(self):
        self.m.flink_home()
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(r["error_message"], "2 item(s) missing: jar, home")
        self.assertEqual(r["next_step"], "nexmark is not ready: build the nexmark-flink jar "
                         "(mvn -pl nexmark-flink -am package -DskipTests) and copy it into lib and lay out "
                         "the runnable nexmark tree (nexmark-flink/src/main/resources) and point NEXMARK_HOME "
                         "at it, then rerun")

    def test_several_jars_warn(self):
        self.ready()
        self.m.nexmark_jars("nexmark-flink-0.2-SNAPSHOT.jar", "nexmark-flink-0.3-SNAPSHOT.jar")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        self.assertEqual(r["nexmark"]["jar"], "nexmark-flink-0.2-SNAPSHOT.jar")  # first in name order
        self.assertEqual(r["items"][1]["status"], "warn")
        self.assertIn("several nexmark jars in lib", r["items"][1]["detail"])
        self.assertIn("warnings above", r["next_step"])

    def test_no_flink_installation(self):
        code, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.item(r, "flink")["status"], "fail")
        self.assertEqual(r["nexmark"], {"flink_home": None, "found_via": None, "jar": None, "jars": [],
                                        "home": None, "home_via": None})
        self.assertTrue(r["next_step"].startswith("install the official binary distribution"))

    def test_json_contract_shape_and_crash_shape(self):
        self.ready()
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "nexmark", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual(set(r["nexmark"]), {"flink_home", "found_via", "jar", "jars", "home", "home_via"})
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
        self.assertEqual(set(a["nexmark"]), set(r["nexmark"]))
        self.assertEqual((a["items"], a["missing"]), ([], []))
        self.assertTrue(a["next_step"].startswith("fix the check error first"))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.ready()
        archive = self.tmp / "project" / "nexmark-check.json"
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
        self.m.flink_home()
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"case": "nexmark-contract", "skill": "flink-velox-nexmark-check",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
