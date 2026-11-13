"""Exercise verify.sh judgment paths outside the container.

Run:  python3 -m unittest discover -s evals -v   (from the skill package root)

verify.sh reads WORKSPACE_DIR (defaults to /workspace in the container), so the
same production logic can be regression-tested on any host without docker.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CASE = Path(__file__).resolve().parents[1] / "cases" / "agent-build-agentenv"
VERIFY = CASE / "verify.sh"


class VerifyJudgmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentenv-verify-test-")
        self.root = Path(self.temp.name)
        (self.root / "opt").mkdir()
        (self.root / "results").mkdir()
        self.repo = self.root / "AgentENV"
        self.repo.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_verify(self):
        env = dict(os.environ, WORKSPACE_DIR=str(self.root), SKILL_MODE="with")
        proc = subprocess.run(["bash", str(VERIFY)], cwd=self.root, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, check=False)
        result = json.loads(
            (self.root / "results" / "result.json").read_text(encoding="utf-8"))
        return proc, result

    def write_report(self, binaries=None):
        report = {
            "repo_dir": str(self.repo),
            "binaries": binaries or {},
            "cargo_version": "cargo 1.80.0",
            "rustc_version": "rustc 1.80.0",
            "build_command": "cargo build --release",
        }
        (self.root / "opt" / "build_report.json").write_text(
            json.dumps(report), encoding="utf-8")

    def make_binary(self, name):
        path = self.repo / "target" / "release" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_all_binaries_present(self):
        (self.repo / "Cargo.toml").write_text("[workspace.package]\n", encoding="utf-8")
        declared = {key: str(self.make_binary(name)) for key, name in
                    (("server", "server"), ("uvm_ublk", "uvm-ublk"),
                     ("uvm_ublk_daemon", "uvm-ublk-daemon"))}
        self.write_report(binaries=declared)
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["success"])
        self.assertEqual(result["build_ok"], 1)
        self.assertEqual(result["repo_present"], 1)
        self.assertEqual(result["binaries_found"], 3)

    def test_fallback_probing_without_declared_paths(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        for name in ("server", "uvm-ublk", "uvm-ublk-daemon"):
            self.make_binary(name)
        self.write_report()
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["binaries_found"], 3)
        self.assertEqual(result["build_ok"], 1)

    def test_missing_binary_fails(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        self.make_binary("server")
        self.write_report()
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["build_ok"], 0)
        self.assertIn("binaries_missing", result["reason"])

    def test_missing_report_fails(self):
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(result["success"])
        self.assertIn("build_report.json", result["reason"])

    def test_repo_absent_fails(self):
        (self.root / "opt" / "build_report.json").write_text(
            json.dumps({"repo_dir": "/nope", "binaries": {}}), encoding="utf-8")
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["repo_present"], 0)


if __name__ == "__main__":
    unittest.main()
