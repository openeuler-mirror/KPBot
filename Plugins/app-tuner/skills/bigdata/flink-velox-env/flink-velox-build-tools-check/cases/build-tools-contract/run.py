#!/usr/bin/env python3
"""build-tools-contract — tier-A case for flink-velox-build-tools-check.

Drives scripts/build_tools_check.py against a fake PATH whose tools are sh
stubs printing canned --version lines (or nothing, or garbage); nothing from the
real machine is in play. Pins the verdict contract (exit codes, success, status,
missing, next_step), every probe rule (version from the first x.y.z, whole-line
fallback, stderr counts, exit code ignored, silent tool = present with unknown
version), the openEuler package name (ninja -> ninja-build), the JSON shape,
--out, "check aborted:" and that a check leaves the machine untouched.

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
SCRIPT = ROOT / "scripts" / "build_tools_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_build_tools_check_result"

# canned --version lines and the version the check must extract, pinned
# independently of the script
LINES = {"ninja": "1.11.1", "ccache": "ccache version 4.8.3",
         "autoconf": "autoconf (GNU Autoconf) 2.71", "automake": "automake (GNU automake) 1.16.5",
         "libtool": "libtool (GNU libtool) 2.4.7", "flex": "flex 2.6.4",
         "bison": "bison (GNU Bison) 3.8.2", "python3": "Python 3.11.6", "patchelf": "patchelf 0.15.0"}
VERSIONS = {"ninja": "1.11.1", "ccache": "4.8.3", "autoconf": "2.71", "automake": "1.16.5",
            "libtool": "2.4.7", "flex": "2.6.4", "bison": "3.8.2", "python3": "3.11.6",
            "patchelf": "0.15.0"}
ORDER = ("ninja", "ccache", "autoconf", "automake", "libtool", "flex", "bison", "python3", "patchelf")


def load_module():
    """build_tools_check.py as an in-process module (for the crash path a
    subprocess cannot be driven into hermetically)."""
    spec = importlib.util.spec_from_file_location("build_tools_check_under_test", str(SCRIPT))
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


class BuildToolsCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakePath(self.tmp / "machine")

    def run_check(self, *args, env=None):
        """Run build_tools_check.py; returns (exit code, stdout, stderr, result or None)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env, cwd=str(self.tmp),
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
        self.assertEqual(r["next_step"], "ready: the build tools are complete")
        # the human report: one compact line, READY, next
        _, out, _, _ = self.run_check()
        self.assertIn("build-tools OK       all 9 present: ninja 1.11.1, ccache 4.8.3, autoconf 2.71, "
                      "automake 1.16.5, libtool 2.4.7, flex 2.6.4, bison 3.8.2, python3 3.11.6, "
                      "patchelf 0.15.0", out)
        self.assertIn("READY: the build tools are complete", out)
        self.assertIn("next: ready", out)
        self.assertEqual(err, "")

    def test_missing_ninja_names_its_package(self):
        self.m.tools(without=("ninja",))
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        self.assertEqual(r["missing"], ["ninja: not found on PATH"])
        item = self.item(r, "ninja")
        self.assertEqual((item["status"], item["detail"], item["fix"]),
                         ("fail", "not found on PATH", "system package (openEuler name): ninja-build"))
        self.assertEqual((r["tools"]["ninja"]["path"], r["tools"]["ninja"]["version"]), (None, None))
        self.assertEqual(r["next_step"], "install the missing build tools (dnf install ninja-build), then rerun")
        self.assertEqual(r["error_message"], "1 tool(s) missing: ninja")
        _, out, _, _ = self.run_check()
        self.assertIn("build-tools FAIL     1 of 9 missing", out)
        self.assertIn("  FAIL  ninja: not found on PATH\n        fix: system package (openEuler name): "
                      "ninja-build", out)
        self.assertIn("NOT READY: 1 tool(s) missing", out)

    def test_several_missing_one_dnf_line_in_table_order(self):
        self.m.tools(without=("patchelf", "flex", "ninja"))  # removed out of order on purpose
        code, _, _, r = self.run_check("--json")
        self.assertEqual(code, 1)
        self.assertEqual(r["missing"], ["ninja: not found on PATH", "flex: not found on PATH",
                                        "patchelf: not found on PATH"])
        self.assertEqual(r["next_step"],
                         "install the missing build tools (dnf install ninja-build flex patchelf), then rerun")
        self.assertEqual(r["error_message"], "3 tool(s) missing: ninja, flex, patchelf")

    def test_version_falls_back_to_whole_line(self):
        self.m.tools()
        self.m.stub("libtool", 'echo "Weird 2 (no dotted version)"\n')
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.item(r, "libtool")["version"], "Weird 2 (no dotted version)")
        self.assertIn("libtool Weird 2 (no dotted version)", self.run_check()[1])

    def test_silent_tool_is_present_with_unknown_version(self):
        self.m.tools()
        self.m.stub("patchelf", "exit 0\n")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        item = self.item(r, "patchelf")
        self.assertEqual((item["status"], item["version"], item["detail"]),
                         ("ok", None, f"version unknown ({self.m.bin / 'patchelf'})"))
        self.assertIn("patchelf version unknown", self.run_check()[1])

    def test_version_on_stderr_and_nonzero_exit_count(self):
        self.m.tools()
        self.m.stub("bison", 'echo "bison (GNU Bison) 3.9.1" >&2; exit 3\n')
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, self.item(r, "bison")["version"]), (0, "3.9.1"))

    def test_json_contract_shape(self):
        self.m.tools(without=("flex",))
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "tools", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual(set(r["tools"]), set(ORDER))
        self.assertEqual(set(r["tools"]["ninja"]), {"path", "version", "package"})
        self.assertEqual(set(self.item(r, "ccache")), {"name", "status", "detail", "version", "package"})
        self.assertEqual(set(self.item(r, "flex")), {"name", "status", "detail", "fix", "package"})
        self.assertRegex(r["checked_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_out_file_matches_stdout(self):
        self.m.tools()
        out_file = self.tmp / "project" / "build-tools-check.json"
        _, stdout, _, _ = self.run_check("--json", "--out", str(out_file))
        self.assertEqual(json.loads(out_file.read_text(encoding="utf-8")),
                         json.loads(stdout))

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
        self.assertIn("probe exploded", r["error_message"])
        # the human report names it a check error, never "READY"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            module.main([])
        self.assertTrue(buffer.getvalue().startswith("CHECK ERROR: check aborted:"))

    def test_check_leaves_machine_untouched(self):
        self.m.tools(without=("patchelf",))
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "build-tools-contract",
        "skill": "flink-velox-build-tools-check",
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
