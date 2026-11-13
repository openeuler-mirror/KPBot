"""Regression for the RUN_ARGS whitelist (see review P1 on PR #102).

Run:  python3 -m unittest discover -s evals -v   (from the skill package root)

The default value must pass the whitelist and reach `docker run` in equals
form; space-form or non-whitelisted arguments must fail closed before docker
is ever invoked. Uses a stub docker binary, so no docker daemon is required.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CASE = Path(__file__).resolve().parents[1] / "cases" / "agent-build-agentenv"
RUN_AB = CASE / "run_ab.sh"

STUB_DOCKER = """#!/usr/bin/env bash
case "$1" in
    run)   echo "docker $*" >> "${STUB_LOG:-/dev/null}"; echo "stub-cid" ;;
    wait)  exit 0 ;;
    inspect) echo "0" ;;
    *)     exit 0 ;;
esac
"""


class RunAbFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentenv-runab-test-")
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "docker").write_text(STUB_DOCKER, encoding="utf-8")
        (self.bin_dir / "docker").chmod(0o755)
        (self.bin_dir / "opencode").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.bin_dir / "opencode").chmod(0o755)
        self.auth = self.root / "auth.json"
        self.auth.write_text("{}", encoding="utf-8")
        self.log = self.root / "docker.log"

    def tearDown(self):
        self.temp.cleanup()

    def run_ab(self, extra_env=None):
        env = dict(os.environ,
                   PATH=f"{self.bin_dir}:{os.environ['PATH']}",
                   OPENCODE_BIN=str(self.bin_dir / "opencode"),
                   OPENCODE_AUTH=str(self.auth),
                   STUB_LOG=str(self.log))
        env.pop("DOCKER_RUN_ARGS", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", str(RUN_AB)], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, check=False)

    def test_default_run_args_pass_whitelist_and_reach_docker(self):
        proc = self.run_ab()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("--network=host", log)
        self.assertEqual(log.count("docker run"), 2,
                         "both A/B conditions must reach docker run")

    def test_space_form_network_rejected(self):
        proc = self.run_ab({"DOCKER_RUN_ARGS": "--network host"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--network", proc.stderr)

    def test_illegal_argument_rejected(self):
        proc = self.run_ab({"DOCKER_RUN_ARGS": "--privileged"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--privileged", proc.stderr)

    def test_custom_whitelisted_args_forwarded(self):
        proc = self.run_ab({"DOCKER_RUN_ARGS": "--cpus=4 --rm --network=bridge"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("--cpus=4", log)
        self.assertIn("--network=bridge", log)


if __name__ == "__main__":
    unittest.main()
