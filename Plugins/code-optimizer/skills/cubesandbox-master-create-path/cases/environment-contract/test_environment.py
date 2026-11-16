"""Check environment stripping with real child processes and synthetic secrets."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
from validation_environment import child_environment, require_executable

SENTINELS = {
    "KPBOT_ENV_SENTINEL_TOKEN": "test-only-token-54d1",
    "CI_JOB_TOKEN": "test-only-ci-token-54d1",
    "AWS_ACCESS_KEY_ID": "test-only-aws-id-54d1",
    "AWS_SECRET_ACCESS_KEY": "test-only-aws-secret-54d1",
    "DATABASE_URL": "test-only-dsn-54d1",
    "AN_UNLISTED_FUTURE_CREDENTIAL": "test-only-unlisted-54d1",
    "PYTHONPATH": "test-only-injected-path-54d1",
    "GOFLAGS": "test-only-injected-go-flags-54d1",
    "GIT_CONFIG_COUNT": "test-only-git-config-54d1",
}


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cube-env-contract-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git = require_executable("git", "Git")

    def test_real_child_does_not_receive_parent_secrets_or_configuration(self):
        with patch.dict(os.environ, SENTINELS):
            environment = child_environment(self.root / "runtime", executables=[self.git])
            result = subprocess.run([sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
                                    cwd=self.root, env=environment, capture_output=True, text=True, check=True)
        observed = json.loads(result.stdout)
        for key in SENTINELS:
            if key == "GOFLAGS":
                self.assertTrue(observed.get(key) == "", "GOFLAGS was not reset")
            else:
                self.assertFalse(key in observed, f"parent key {key} was inherited")
        self.assertFalse(any(value in str(observed) for value in SENTINELS.values()), "sentinel value reached child")

    def test_parent_path_home_and_caches_are_not_reused(self):
        contamination = {"PATH": "test-only-parent-path-54d1", "HOME": "test-only-home-54d1",
                         "USERPROFILE": "test-only-profile-54d1", "GOCACHE": "test-only-cache-54d1"}
        with patch.dict(os.environ, contamination):
            environment = child_environment(self.root / "runtime", executables=[sys.executable, self.git])
        for key in ["HOME", "USERPROFILE", "GOCACHE", "GOPATH", "TMP", "TEMP", "XDG_CONFIG_HOME"]:
            self.assertTrue(Path(environment[key]).is_relative_to(self.root), f"{key} is not task-local")
        self.assertFalse("test-only-parent-path" in environment["PATH"], "parent PATH was copied")
        self.assertEqual(environment["GOENV"], "off")
        self.assertEqual(environment["GOPROXY"], "off")
        self.assertEqual(environment["CGO_ENABLED"], "0")

    def test_static_mode_needs_no_go_and_does_not_execute_cases(self):
        environment = child_environment(self.root / "runtime", executables=[self.git])
        # Challenge the trusted entrypoint with synthetic parent credentials;
        # they must not be forwarded to its Git/case subprocesses or logs.
        environment.update(SENTINELS)
        result = subprocess.run([sys.executable, "-I", str(SKILL / "scripts/validate_package.py"),
                                 "--static-only", "--git", self.git, "--go", "missing-go-for-static-contract"],
                                cwd=self.root, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "static patch parsing failed")
        self.assertIn("STATIC PASS:", result.stdout)
        self.assertNotIn("RUN " + sys.executable, result.stdout)
        self.assertNotIn("PASS: raw patch format, Git parsing, comparison tests", result.stdout)
        self.assertFalse(any(value in result.stdout + result.stderr for value in SENTINELS.values()),
                         "static logs exposed synthetic credentials")

    def test_external_patch_argument_is_rejected_before_execution(self):
        environment = child_environment(self.root / "runtime", executables=[self.git])
        result = subprocess.run([sys.executable, str(SKILL / "cases/replica-schedulability/run.py"),
                                 "--patch", str(self.root / "outside.patch")],
                                cwd=self.root, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertFalse((self.root / "outside.patch").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
