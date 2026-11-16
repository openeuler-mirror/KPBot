"""Case contract checks for the kptools-openeuler-agentenv skill package.

Run:  python3 -m unittest discover -s evals -v   (from the skill package root)
"""

import re
import shlex
import stat
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]      # .../skills/kptools-openeuler-agentenv
PLUGIN = PACKAGE.parents[1]                        # .../Plugins/app-tuner
CASE = PACKAGE / "cases" / "agent-build-agentenv"


class CaseContractTests(unittest.TestCase):
    def test_case_builds_from_plugin_root(self):
        text = (CASE / "case.yaml").read_text(encoding="utf-8")
        context_match = re.search(r"(?m)^  context:\s*(\S+)", text)
        dockerfile_match = re.search(r"(?m)^  dockerfile:\s*(\S+)", text)
        self.assertIsNotNone(context_match)
        self.assertIsNotNone(dockerfile_match)
        context = (CASE / context_match.group(1)).resolve()
        self.assertEqual(context, PLUGIN)
        dockerfile = context / dockerfile_match.group(1)
        self.assertTrue(dockerfile.is_file(), dockerfile)
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not line.startswith("COPY "):
                continue
            parts = shlex.split(line)
            self.assertTrue((context / parts[1]).exists(), parts[1])

    def test_case_yaml_core_fields(self):
        text = (CASE / "case.yaml").read_text(encoding="utf-8")
        for pattern in (r"(?m)^name:\s*agent-build-agentenv\b",
                        r"(?m)^skill:\s*kptools-openeuler-agentenv\b",
                        r"(?m)^tier:\s*agent\b"):
            self.assertRegex(text, pattern)
        for field in ("success", "repo_present", "build_ok"):
            self.assertIn(field, text)

    def test_image_name_matches_run_ab_default(self):
        yaml_image = re.search(r"(?m)^image:\s*(\S+)",
                               (CASE / "case.yaml").read_text(encoding="utf-8")).group(1)
        run_ab_image = re.search(r'DEFAULT_IMAGE="([^"]+)"',
                                 (CASE / "run_ab.sh").read_text(encoding="utf-8")).group(1)
        self.assertEqual(yaml_image, run_ab_image)

    def test_prompt_ab_isolation(self):
        with_text = (CASE / "prompts" / "with-skill.md").read_text(encoding="utf-8")
        without_text = (CASE / "prompts" / "without-skill.md").read_text(encoding="utf-8")
        prefix = "请使用 kptools-openeuler-agentenv skill，"
        self.assertTrue(with_text.startswith(prefix),
                        "with-skill prompt must explicitly mention the skill")
        self.assertEqual(with_text[len(prefix):], without_text,
                         "with/without prompts may differ only by the skill mention")

    def test_case_scripts_executable(self):
        for name in ("entrypoint.sh", "verify.sh", "run_ab.sh"):
            mode = (CASE / name).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, name)

    def test_skill_frontmatter(self):
        text = (PACKAGE / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^---\s*\nname:\s*(\S+)\s*\ndescription:\s*(.+?)\n---",
                          text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), PACKAGE.name)
        self.assertGreater(len(match.group(2)), 20)


if __name__ == "__main__":
    unittest.main()
