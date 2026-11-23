#!/usr/bin/env python3
"""toolchain-contract — tier-A case for flink-velox-toolchain-check.

Drives scripts/toolchain_check.py against a fake PATH whose tools are sh stubs
printing canned --version lines (or nothing, or garbage); nothing from the real
machine is in play. Pins the verdict contract (exit codes, success, status,
missing, next_step), every probe rule (version from the first x.y.z, whole-line
fallback, stderr counts, exit code ignored, silent tool = present with unknown
version), the openEuler package names (g++ -> gcc-c++, mvn -> maven), the JSON
shape, --out safety (inside the project, no symlink, no silent overwrite), the full
contract shape of a crash result and that a check leaves the machine untouched.

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
SCRIPT = ROOT / "scripts" / "toolchain_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_toolchain_check_result"

# canned --version lines and the version the check must extract, pinned
# independently of the script
LINES = {"git": "git version 2.43.0", "cmake": "cmake version 3.30.4",
         "gcc": "gcc (GCC) 12.3.1 (openEuler 24.03)", "g++": "g++ (GCC) 12.3.1",
         "mvn": "Apache Maven 3.6.3"}
VERSIONS = {"git": "2.43.0", "cmake": "3.30.4", "gcc": "12.3.1", "g++": "12.3.1", "mvn": "3.6.3"}
ORDER = ("git", "cmake", "gcc", "g++", "mvn")


def load_module():
    """toolchain_check.py as an in-process module (for the crash path a
    subprocess cannot be driven into hermetically)."""
    spec = importlib.util.spec_from_file_location("toolchain_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePath:
    """A directory of tool stubs that is the whole PATH."""

    def __init__(self, root):
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def tools(self, without=(), lines=LINES):
        for name, line in lines.items():
            if name not in without:
                self.stub(name, f'echo "{line}"\n')

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "HOME": str(self.root), "LC_ALL": "C"}
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


class ToolchainCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakePath(self.tmp / "machine")

    def run_check(self, *args, env=None, cwd=None):
        """Run toolchain_check.py; returns (exit code, stdout, stderr, result or None)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_all_present(self):
        self.m.tools()
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["missing"], r["error_message"]),
                         (0, True, "ok", [], ""))
        self.assertEqual([i["name"] for i in r["items"]], list(ORDER))
        for name in ORDER:
            self.assertEqual((self.item(r, name)["status"], self.item(r, name)["version"]),
                             ("ok", VERSIONS[name]))
            self.assertEqual(r["tools"][name]["path"], str(self.m.bin / name))
        self.assertEqual(r["next_step"], "ready: the compile toolchain is complete")
        # the human report: one compact line, READY, next
        _, out, _, _ = self.run_check()
        self.assertIn("toolchain   OK       all 5 present: git 2.43.0, cmake 3.30.4, gcc 12.3.1, "
                      "g++ 12.3.1, mvn 3.6.3", out)
        self.assertIn("READY: the compile toolchain is complete", out)
        self.assertIn("next: ready", out)
        self.assertEqual(err, "")

    def test_missing_gpp_names_its_package(self):
        self.m.tools(without=("g++",))
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        self.assertEqual(r["missing"], ["g++: not found on PATH"])
        item = self.item(r, "g++")
        self.assertEqual((item["status"], item["detail"], item["fix"]),
                         ("fail", "not found on PATH", "system package (openEuler name): gcc-c++"))
        self.assertEqual((r["tools"]["g++"]["path"], r["tools"]["g++"]["version"]), (None, None))
        self.assertEqual(r["next_step"], "install the missing tools (dnf install gcc-c++), then rerun")
        self.assertEqual(r["error_message"], "1 tool(s) missing: g++")
        _, out, _, _ = self.run_check()
        self.assertIn("toolchain   FAIL     1 of 5 missing", out)
        self.assertIn("  FAIL  g++: not found on PATH\n        fix: system package (openEuler name): gcc-c++", out)
        self.assertIn("NOT READY: 1 tool(s) missing", out)

    def test_several_missing_one_dnf_line_in_table_order(self):
        self.m.tools(without=("mvn", "g++"))  # removed in reverse order on purpose
        code, _, _, r = self.run_check("--json")
        self.assertEqual(code, 1)
        self.assertEqual(r["missing"], ["g++: not found on PATH", "mvn: not found on PATH"])
        self.assertEqual(r["next_step"], "install the missing tools (dnf install gcc-c++ maven), then rerun")
        self.assertEqual(self.item(r, "mvn")["fix"], "system package (openEuler name): maven")
        self.assertEqual(r["error_message"], "2 tool(s) missing: g++, mvn")

    def test_version_falls_back_to_whole_line(self):
        self.m.tools()
        self.m.stub("cmake", 'echo "Weird 1 (no dotted version)"\n')
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.item(r, "cmake")["version"], "Weird 1 (no dotted version)")
        self.assertIn("cmake Weird 1 (no dotted version)", self.run_check()[1])

    def test_silent_tool_is_present_with_unknown_version(self):
        self.m.tools()
        self.m.stub("mvn", "exit 0\n")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        item = self.item(r, "mvn")
        self.assertEqual((item["status"], item["version"], item["detail"]),
                         ("ok", None, f"version unknown ({self.m.bin / 'mvn'})"))
        self.assertIn("mvn version unknown", self.run_check()[1])

    def test_version_on_stderr_and_nonzero_exit_count(self):
        self.m.tools()
        self.m.stub("gcc", 'echo "gcc (GCC) 13.2.0" >&2; exit 3\n')
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, self.item(r, "gcc")["version"]), (0, "13.2.0"))

    def test_json_contract_shape(self):
        self.m.tools(without=("gcc",))
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "tools", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual(set(r["tools"]), set(ORDER))
        self.assertEqual(set(r["tools"]["g++"]), {"path", "version", "package"})
        self.assertEqual(set(self.item(r, "git")), {"name", "status", "detail", "version", "package"})
        self.assertEqual(set(self.item(r, "gcc")), {"name", "status", "detail", "fix", "package"})
        self.assertRegex(r["checked_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_out_file_matches_stdout(self):
        self.m.tools()
        out_file = self.tmp / "project" / "toolchain-check.json"
        _, stdout, _, _ = self.run_check("--json", "--out", str(out_file))
        self.assertEqual(json.loads(out_file.read_text(encoding="utf-8")),
                         json.loads(stdout))

    def test_out_is_confined_to_the_project(self):
        self.m.tools()
        # outside the project (the working directory): refused, exit 2, nothing written
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        out_file = self.tmp / "elsewhere_outside.json"
        code, stdout, err, r = self.run_check("--json", "--out", str(out_file), cwd=str(elsewhere))
        self.assertEqual((code, r["success"]), (2, True))  # the verdict still prints
        self.assertIn("inside the project directory", err)
        self.assertFalse(out_file.exists())
        # a symlink, even one pointing inside the project: refused, its target untouched
        target = self.tmp / "keep.json"
        target.write_text("keep me\n", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        code, _, err, _ = self.run_check("--json", "--out", str(link))
        self.assertEqual(code, 2)
        self.assertIn("not a symlink", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        # an existing archive is kept unless --overwrite is given
        archive = self.tmp / "project" / "check.json"
        self.assertEqual(self.run_check("--json", "--out", str(archive))[0], 0)
        first = archive.read_text(encoding="utf-8")
        code, _, err, _ = self.run_check("--json", "--out", str(archive))
        self.assertEqual(code, 2)
        self.assertIn("--overwrite", err)
        self.assertEqual(archive.read_text(encoding="utf-8"), first)
        self.m.stub("cmake", "exit 0\n")  # present, version unknown: a different result to archive
        code, _, _, _ = self.run_check("--json", "--out", str(archive), "--overwrite")
        self.assertEqual(code, 0)
        self.assertIn("version unknown", archive.read_text(encoding="utf-8"))
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--bogus")
        self.assertEqual((code, r), (2, None))
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_scan_aborted_is_2(self):
        module = load_module()

        def boom(name):
            raise RuntimeError("probe exploded")

        module.probe_tool = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        r = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, r["success"]), (2, False))
        self.assertTrue(r["error_message"].startswith("check aborted:"))
        # the crash result is in the full contract shape: no missing key for a consumer
        self.assertEqual(set(r), {"success", "checked_at", "status", "tools", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual((r["status"], r["tools"], r["items"], r["missing"]), ("fail", {}, [], []))
        self.assertTrue(r["next_step"].startswith("fix the check error first"))
        self.assertIn("probe exploded", r["error_message"])
        # the human report names it a check error, never "READY"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            module.main([])
        self.assertTrue(buffer.getvalue().startswith("CHECK ERROR: check aborted:"))

    def test_check_leaves_machine_untouched(self):
        self.m.tools(without=("mvn",))
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "toolchain-contract",
        "skill": "flink-velox-toolchain-check",
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
