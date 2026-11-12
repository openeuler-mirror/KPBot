"""Regression checks for independent cases and their trusted evidence boundary."""

import importlib.util
import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
PLUGIN = PACKAGE.parents[1]
SPEC = importlib.util.spec_from_file_location("agent_case_supervisor", PACKAGE / "scripts/agent_case_supervisor.py")
SUPERVISOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPERVISOR)


class CaseContractTests(unittest.TestCase):
    def test_each_case_builds_from_plugin_root(self):
        cases = list(PACKAGE.rglob("case.yaml"))
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case):
                text = case.read_text(encoding="utf-8")
                context_match = re.search(r"(?m)^  context:\s*(\S+)", text)
                dockerfile_match = re.search(r"(?m)^  dockerfile:\s*(\S+)", text)
                self.assertIsNotNone(context_match)
                self.assertIsNotNone(dockerfile_match)
                context = (case.parent / context_match.group(1)).resolve()
                self.assertEqual(context, PLUGIN)
                dockerfile = context / dockerfile_match.group(1)
                self.assertTrue(dockerfile.is_file(), dockerfile)
                for line in dockerfile.read_text(encoding="utf-8").splitlines():
                    if not line.startswith("COPY "):
                        continue
                    parts = shlex.split(line)
                    self.assertTrue((context / parts[1]).exists(), parts[1])

    def test_entry_documents_exist(self):
        text = (PACKAGE / "SKILL.md").read_text(encoding="utf-8")
        for path in re.findall(r"`((?:references|scripts)/[^`\s]+\.(?:md|sh|py))`", text):
            with self.subTest(path=path):
                self.assertTrue((PACKAGE / path).is_file(), path)


    def test_specialist_documents_exist(self):
        skills = list((PACKAGE / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for skill in skills:
            text = skill.read_text(encoding="utf-8")
            for path in re.findall(r"`((?:references|scripts)/[^`\s]+\.(?:md|sh|py))`", text):
                with self.subTest(skill=skill, path=path):
                    self.assertTrue((PACKAGE / path).is_file(), path)

    def test_eval_input_files_exist(self):
        for path in (PACKAGE / "evals").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for case in data["evals"]:
                for resource in case.get("files", []):
                    with self.subTest(eval=path, case=case["id"], resource=resource):
                        self.assertTrue((PACKAGE / resource).is_file(), resource)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentenv-case-test-")
        self.root = Path(self.temp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.original_results = SUPERVISOR.RESULTS
        self.original_workspace = SUPERVISOR.WORKSPACE
        self.original_staging = SUPERVISOR.STAGING
        SUPERVISOR.RESULTS = self.results
        SUPERVISOR.WORKSPACE = self.root

    def tearDown(self):
        SUPERVISOR.RESULTS = self.original_results
        SUPERVISOR.WORKSPACE = self.original_workspace
        SUPERVISOR.STAGING = self.original_staging
        self.temp.cleanup()

    def trace(self, events):
        (self.results / "trace.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    def test_plain_text_and_failed_calls_do_not_prove_skill_loading(self):
        self.trace([{"type": "text", "text": "skills/example-agent-skill/SKILL.md"},
                    {"type": "tool_use", "part": {"type": "tool", "tool": "skill",
                     "state": {"status": "error", "input": {"name": "example-agent-skill"}}}},
                    ["invalid event"]])
        summary, loaded = SUPERVISOR.summarize_trace("example-agent-skill", "example-package")
        self.assertFalse(loaded)
        self.assertEqual(summary["skills_invoked"], [])

    def test_completed_skill_call_and_actual_read_are_recognized(self):
        skill = {"type": "tool_use", "part": {"type": "tool", "tool": "skill",
                 "state": {"status": "completed", "input": {"name": "example-agent-skill"}}}}
        read = {"type": "tool_use", "part": {"type": "tool", "tool": "read",
                "state": {"status": "completed", "input": {"filePath": str(self.root / ".opencode/skills/example-agent-skill/SKILL.md")}}}}
        for event in (skill, read):
            with self.subTest(tool=event["part"]["tool"]):
                self.trace([event])
                self.assertTrue(SUPERVISOR.summarize_trace("example-agent-skill", "example-package")[1])
                self.assertFalse(SUPERVISOR.summarize_trace("different-agent-skill", "example-package")[1])

    def test_read_requires_installed_skill_path(self):
        paths = {
            ".opencode/skills/example-agent-skill/SKILL.md": True,
            ".opencode/skills/example-package/skills/example-agent-skill/SKILL.md": True,
            "/staging/pkg/skills/example-agent-skill/SKILL.md": False,
            "other/skills/example-agent-skill/SKILL.md": False,
            ".opencode/skills/../skills/example-agent-skill/SKILL.md": False,
        }
        for path, expected in paths.items():
            with self.subTest(path=path):
                self.trace([{"type": "tool_use", "part": {"type": "tool", "tool": "read",
                            "state": {"status": "completed", "input": {"filePath": path}}}}])
                self.assertIs(SUPERVISOR.summarize_trace("example-agent-skill", "example-package")[1], expected)

    def test_install_excludes_eval_answers(self):
        staging = self.root / "staging"
        (staging / "skills/example-agent-skill").mkdir(parents=True)
        (staging / "skills/example-agent-skill/SKILL.md").write_text("skill", encoding="utf-8")
        (staging / "evals").mkdir()
        (staging / "evals/answers.json").write_text("answers", encoding="utf-8")
        SUPERVISOR.STAGING = staging
        SUPERVISOR.prepare_skill("with", "example-package", "example-agent-skill")
        installed = self.root / ".opencode/skills"
        self.assertTrue((installed / "example-agent-skill/SKILL.md").is_file())
        self.assertFalse((installed / "example-package/evals").exists())

    def test_report_snapshot_rejects_symlinks_and_hardlinks(self):
        opt = self.root / "opt"
        opt.mkdir()
        source = opt / "report.json"
        source.write_text('{"status":"blocked"}', encoding="utf-8")
        SUPERVISOR.snapshot_report(os.getuid())
        self.assertEqual((self.results / "report.json").read_text(), source.read_text())
        alias = opt / "alias"
        os.link(source, alias)
        with self.assertRaises(ValueError):
            SUPERVISOR.snapshot_report(os.getuid())
        alias.unlink()
        source.unlink()
        source.symlink_to(self.results / "report.json")
        with self.assertRaises(OSError):
            SUPERVISOR.snapshot_report(os.getuid())


if __name__ == "__main__":
    unittest.main()
