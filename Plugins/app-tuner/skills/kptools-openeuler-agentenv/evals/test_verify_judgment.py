"""Exercise verify.sh judgment paths outside the container.

Run:  python3 -m unittest discover -s evals -v   (from the skill package root)

verify.sh reads WORKSPACE_DIR (defaults to /workspace in the container), so the
same production logic can be regression-tested on any host without docker.

Judgment is bound to real build artifacts (review P2): binaries must live in
repo_dir/target/release/ (realpath containment), be ELF and executable; the
agent-declared paths alone can never satisfy build_ok.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CASE = Path(__file__).resolve().parents[1] / "cases" / "agent-build-agentenv"
VERIFY = CASE / "verify.sh"

ELF = b"\x7fELF" + b"\x00" * 64


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

    def write_report(self, binaries=None, **overrides):
        report = {
            "repo_dir": str(self.repo),
            "binaries": binaries or {},
            "cargo_version": "cargo 1.80.0",
            "rustc_version": "rustc 1.80.0",
            "build_command": "cargo build --release",
        }
        report.update(overrides)
        (self.root / "opt" / "build_report.json").write_text(
            json.dumps(report), encoding="utf-8")

    def make_elf(self, name):
        path = self.repo / "target" / "release" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ELF)
        path.chmod(0o755)
        return path

    def test_all_binaries_present(self):
        (self.repo / "Cargo.toml").write_text("[workspace.package]\n", encoding="utf-8")
        declared = {key: str(self.make_elf(name)) for key, name in
                    (("server", "server"), ("uvm_ublk", "uvm-ublk"),
                     ("uvm_ublk_daemon", "uvm-ublk-daemon"))}
        self.write_report(binaries=declared)
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["success"])
        self.assertEqual(result["build_ok"], 1)
        self.assertEqual(result["repo_present"], 1)
        self.assertEqual(result["binaries_found"], 3)

    def test_target_release_probing_without_declared_paths(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        for name in ("server", "uvm-ublk", "uvm-ublk-daemon"):
            self.make_elf(name)
        self.write_report()
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["binaries_found"], 3)
        self.assertEqual(result["build_ok"], 1)

    def test_declared_system_executable_rejected(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        decoy = self.root / "true"
        decoy.write_bytes(ELF)
        decoy.chmod(0o755)
        self.write_report(binaries={"server": str(decoy),
                                    "uvm_ublk": str(decoy),
                                    "uvm_ublk_daemon": str(decoy)})
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["build_ok"], 0,
                         "declared paths outside target/release must never pass")
        self.assertIn("binaries_missing", result["reason"])

    def test_symlink_escape_rejected(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        decoy = self.root / "true"
        decoy.write_bytes(ELF)
        decoy.chmod(0o755)
        link_dir = self.repo / "target" / "release"
        link_dir.mkdir(parents=True, exist_ok=True)
        for name in ("server", "uvm-ublk", "uvm-ublk-daemon"):
            (link_dir / name).symlink_to(decoy)
        self.write_report()
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["build_ok"], 0,
                         "symlinks resolving outside target/release must fail")

    def test_non_elf_artifact_rejected(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        for name in ("server", "uvm-ublk", "uvm-ublk-daemon"):
            path = self.repo / "target" / "release" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
            path.chmod(0o755)
        self.write_report()
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["build_ok"], 0,
                         "non-ELF executables in target/release must fail")

    def test_report_fields_missing_fails(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        for name in ("server", "uvm-ublk", "uvm-ublk-daemon"):
            self.make_elf(name)
        self.write_report(build_command="", cargo_version="")
        proc, result = self.run_verify()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("report_fields_missing", result["reason"])

    def test_missing_binary_fails(self):
        (self.repo / "Cargo.toml").write_text("", encoding="utf-8")
        self.make_elf("server")
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
