#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tier-A contract tests for model-precision-analysis (stdlib only).

Guards frontmatter/metadata, bundled resources, the standardized eval schema,
content hygiene, and the behavioural contract of the bundled scripts.
Run via run.sh, or directly:

    python3 cases/skill-contract/test_skill_contract.py
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]
REPO = SKILL_DIR.parents[3]

SCAN_FILES = ("SKILL.md", "scripts/analyze_precision.py",
              "evals/evals.json", "evals/trigger_eval.json")
PERSONAL_PATH = re.compile(r"/home/[A-Za-z0-9_.-]+|/data/l\d{8}")
EMPLOYEE_ID = re.compile(r"\b[lw]\d{8}\b")


def read(path):
    return Path(path).read_text(encoding="utf-8")


def frontmatter():
    text = read(SKILL_DIR / "SKILL.md")
    assert text.startswith("---\n"), "missing YAML frontmatter"
    end = text.find("\n---\n", 4)
    assert end != -1, "unterminated YAML frontmatter"
    block = text[4:end]
    flat, root = {}, None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  "):
            key, _, value = line.strip().partition(":")
            flat["%s.%s" % (root, key.strip())] = value.strip().strip('"')
        else:
            key, _, value = line.partition(":")
            root = key.strip()
            flat[root] = value.strip().strip('"')
    return flat


class TestFrontmatter(unittest.TestCase):
    def test_name_matches_directory(self):
        self.assertEqual(frontmatter()["name"], SKILL_DIR.name)

    def test_description_covers_triggers(self):
        desc = frontmatter()["description"]
        for token in ("精度", "回归", "准确率"):
            self.assertIn(token, desc, "description 缺少触发词 %s" % token)

    def test_metadata_complete(self):
        fm = frontmatter()
        self.assertEqual(fm.get("license"), "MulanPSL-2.0")
        for key in ("skill_type", "category", "domain", "activation_mode", "version"):
            self.assertTrue(fm.get("metadata.%s" % key), "metadata.%s 缺失" % key)


class TestResources(unittest.TestCase):
    def test_scripts_present(self):
        for name in ("scripts/analyze_precision.py",
                     "tests/test_extraction.py", "tests/test_pipeline.py",
                     "tests/test_json_output.py"):
            self.assertTrue((SKILL_DIR / name).is_file(), name)

    def test_evals_schema(self):
        data = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        self.assertEqual(data["skill_name"], SKILL_DIR.name)
        ids = [e["id"] for e in data["evals"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in data["evals"]:
            self.assertTrue(e.get("eval_name"), "eval %s 缺 eval_name" % e["id"])
            self.assertTrue(e.get("assertions"), "eval %s 缺 assertions" % e["id"])
            self.assertNotIn("expectations", e, "eval %s 仍用旧字段 expectations" % e["id"])

    def test_trigger_eval(self):
        data = json.loads(read(SKILL_DIR / "evals" / "trigger_eval.json"))
        self.assertIsInstance(data, list)
        pos = [q for q in data if q["should_trigger"]]
        neg = [q for q in data if not q["should_trigger"]]
        self.assertGreaterEqual(len(pos), 8)
        self.assertGreaterEqual(len(neg), 8)
        for q in data:
            self.assertTrue(q.get("query"))


class TestHygiene(unittest.TestCase):
    def test_no_personal_paths(self):
        hits = [name for name in SCAN_FILES if PERSONAL_PATH.search(read(SKILL_DIR / name))]
        self.assertEqual(hits, [], "发现个人绝对路径: %s" % hits)

    def test_no_employee_ids(self):
        hits = [name for name in SCAN_FILES if EMPLOYEE_ID.search(read(SKILL_DIR / name))]
        self.assertEqual(hits, [], "发现工号: %s" % hits)

    def test_lf_only(self):
        bad = [name for name in SCAN_FILES if b"\r" in (SKILL_DIR / name).read_bytes()]
        self.assertEqual(bad, [], "存在 CRLF: %s" % bad)


class TestScriptContract(unittest.TestCase):
    def _run(self, name):
        r = subprocess.run([sys.executable, str(SKILL_DIR / name)],
                           capture_output=True, text=True, cwd=str(SKILL_DIR))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_extraction(self):
        self._run("tests/test_extraction.py")

    def test_pipeline(self):
        self._run("tests/test_pipeline.py")

    def test_json_output(self):
        self._run("tests/test_json_output.py")


class TestRegistration(unittest.TestCase):
    def test_changelog_mentions_skill(self):
        if not (REPO / "CHANGELOG.md").is_file():
            self.skipTest("standalone copy")
        self.assertIn(SKILL_DIR.name, read(REPO / "CHANGELOG.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
