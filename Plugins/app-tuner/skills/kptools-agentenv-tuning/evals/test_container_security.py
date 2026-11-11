"""Opt-in Docker tests with synthetic inputs; no provider credentials are needed."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

IMAGE = os.environ.get("AGENTENV_TEST_IMAGE")
FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

if os.geteuid() == 0:
    sys.exit(20)
paths = [Path('/workspace/verify.sh'), Path('/workspace/entrypoint.sh'),
         Path('/workspace/.pristine.sha256'), Path('/workspace/opencode.json'),
         Path('/workspace/results/trace.jsonl'),
         Path('/staging/pkg/scripts/agent_case_supervisor.py')]
paths.extend(p for p in Path('/workspace/evidence').iterdir() if p.is_file())
for path in paths:
    try:
        with path.open('w'):
            pass
    except (PermissionError, FileNotFoundError):
        pass
    else:
        sys.exit(21)
    try:
        path.unlink()
    except (PermissionError, FileNotFoundError):
        pass
    else:
        sys.exit(22)

# The unprivileged agent must not be able to read the staged skill in either
# mode; the supervisor installs a separate readable copy only in with mode.
try:
    Path('/staging/pkg/skills/' + os.environ['EXPECTED_SKILL'] + '/SKILL.md').read_text()
except PermissionError:
    pass
else:
    sys.exit(23)
for secret in (Path('/workspace/verify.sh'), Path('/workspace/.pristine.sha256')):
    try:
        secret.read_text()
    except PermissionError:
        pass
    else:
        sys.exit(24)
if Path('/workspace/.opencode/skills/kptools-agentenv-tuning/evals').exists():
    sys.exit(25)

model = sys.argv[sys.argv.index('-m') + 1]
mode = os.environ['SKILL_MODE']
expected = os.environ['EXPECTED_SKILL']
if model == 'fake/exit-fail':
    sys.exit(7)
report = Path('/workspace/opt/report.json')
if model == 'fake/report-symlink':
    report.symlink_to('/etc/os-release')
else:
    report.write_text(json.dumps({'status': 'blocked', 'validation_level': 'environment',
        'findings': ['unknown host container module ioctl guest benchmark cpuset dvmbm'],
        'evidence_paths': [], 'candidate_actions': [], 'missing_inputs': [], 'gain_pct': None}))
if model == 'fake/no-skill':
    print(json.dumps({'type': 'text', 'text': 'skills/' + expected + '/SKILL.md'}))
elif model == 'fake/skill-error':
    print(json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'skill',
        'state': {'status': 'error', 'input': {'name': expected}}}}))
elif model == 'fake/read-skill':
    print(json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'read',
        'state': {'status': 'completed', 'input': {'filePath': '/workspace/.opencode/skills/' + expected + '/SKILL.md'}}}}))
elif model == 'fake/read-staging':
    print(json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'read',
        'state': {'status': 'completed', 'input': {'filePath': '/staging/pkg/skills/' + expected + '/SKILL.md'}}}}))
elif mode == 'with' or model == 'fake/without-leak':
    print(json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'skill',
        'state': {'status': 'completed', 'input': {'name': expected}}}}))
'''


@unittest.skipUnless(IMAGE and shutil.which("docker"), "set AGENTENV_TEST_IMAGE to run Docker integration tests")
class ContainerSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agentenv-docker-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.agent = root / "opencode"
        cls.agent.write_text(FAKE_AGENT, encoding="utf-8")
        cls.agent.chmod(0o755)
        cls.auth = root / "auth.json"
        cls.auth.write_text("{}\n", encoding="utf-8")
        cls.auth.chmod(0o644)
        root.chmod(0o755)

    def run_case(self, mode, model, tamper=False):
        command = ["docker", "create", "--network", "none", "-e", "SKILL_MODE=" + mode,
                   "-e", "MODEL=" + model, "-e", "AGENTENV_CASE_TIMEOUT=30",
                   "-v", str(self.agent) + ":/usr/local/bin/opencode:ro",
                   "-v", str(self.auth) + ":/run/secrets/opencode-auth.json:ro"]
        if tamper:
            command += ["--entrypoint", "/bin/sh"]
        command.append(IMAGE)
        if tamper:
            command += ["-c", "chmod u+w /workspace/verify.sh; printf tamper >> /workspace/verify.sh; exec /workspace/entrypoint.sh"]
        container = subprocess.check_output(command, text=True).strip()
        try:
            proc = subprocess.run(["docker", "start", "-a", container], capture_output=True, text=True, timeout=40)
            archive = subprocess.check_output(["docker", "cp", container + ":/workspace/results/result.json", "-"])
            with tarfile.open(fileobj=io.BytesIO(archive)) as result:
                member = next(item for item in result if item.isfile())
                data = json.load(result.extractfile(member))
            return proc.returncode, data
        finally:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_unprivileged_agent_and_ab_modes(self):
        for mode, loaded in (("with", True), ("without", False)):
            with self.subTest(mode=mode):
                code, result = self.run_case(mode, "fake/success")
                self.assertEqual(code, 0)
                self.assertTrue(result["success"])
                self.assertIs(result["skill_loaded"], loaded)

    def test_only_completed_skill_or_read_counts(self):
        for model, success in (("fake/no-skill", False), ("fake/skill-error", False),
                               ("fake/read-staging", False), ("fake/read-skill", True)):
            with self.subTest(model=model):
                code, result = self.run_case("with", model)
                self.assertEqual(code == 0, success)
                self.assertIs(result["success"], success)

    def test_symlink_and_agent_failure_are_rejected(self):
        for model in ("fake/report-symlink", "fake/exit-fail"):
            with self.subTest(model=model):
                code, result = self.run_case("with", model)
                self.assertNotEqual(code, 0)
                self.assertFalse(result["success"])

    def test_without_mode_rejects_skill_call(self):
        code, result = self.run_case("without", "fake/without-leak")
        self.assertNotEqual(code, 0)
        self.assertFalse(result["success"])

    def test_changed_grader_fails_integrity_check(self):
        code, result = self.run_case("with", "fake/success", tamper=True)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "case integrity check failed")


if __name__ == "__main__":
    unittest.main()
