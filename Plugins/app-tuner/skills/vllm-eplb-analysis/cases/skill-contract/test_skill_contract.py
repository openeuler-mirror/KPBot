#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tier-A contract tests for the vllm-eplb-analysis skill (stdlib only)."""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]
APP_TUNER = SKILL_DIR.parents[1]
REPO = SKILL_DIR.parents[3]
SIBLING = SKILL_DIR.parent / "vllm-eplb-optimization"

SCAN_DIRS = ("references", "scripts", "evals/fixtures")
SCAN_FILES = ("SKILL.md", "evals/evals.json", "evals/eval_queries.json")

VENDOR_WORDS = ["".join(p) for p in (
    ("鲲", "鹏"), ("昇", "腾"), ("kun", "peng"), ("Kun", "peng"),
    ("As", "cend"), ("as", "cend"), ("N", "PU"), ("910", "B"),
    ("At", "las"), ("H", "BM"),
)]
EMPLOYEE_ID = re.compile(r"\b[lw]\d{8}\b")
PERSONAL_PATH = re.compile(r"/home/[A-Za-z0-9_.-]+|/data/l\d{8}")


def read(path):
    return Path(path).read_text(encoding="utf-8")


def frontmatter():
    text = read(SKILL_DIR / "SKILL.md")
    assert text.startswith("---\n"), "missing YAML frontmatter"
    end = text.find("\n---\n", 4)
    assert end != -1, "unterminated YAML frontmatter"
    flat = {}
    root = None
    for line in text[4:end].splitlines():
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


def scanned_files():
    files = [SKILL_DIR / name for name in SCAN_FILES]
    for d in SCAN_DIRS:
        files.extend(sorted((SKILL_DIR / d).rglob("*")))
    return [f for f in files
            if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc",)]


class TestFrontmatter(unittest.TestCase):
    def test_name_matches_directory(self):
        self.assertEqual(frontmatter()["name"], SKILL_DIR.name)

    def test_description_covers_triggers(self):
        desc = frontmatter()["description"]
        for token in ("不均衡", "balancedness", "EPLB"):
            self.assertIn(token, desc, "description 缺少触发词 %s" % token)

    def test_metadata_complete(self):
        fm = frontmatter()
        self.assertEqual(fm.get("license"), "MulanPSL-2.0")
        for key in ("skill_type", "category", "domain", "activation_mode", "version"):
            self.assertTrue(fm.get("metadata.%s" % key), "metadata.%s 缺失" % key)
        self.assertEqual(fm.get("metadata.side_effect_free"), "true")


class TestResources(unittest.TestCase):
    def test_analyzer_present(self):
        self.assertTrue((SKILL_DIR / "scripts" / "analyze_eplb_metrics.py").is_file())

    def test_reference_present(self):
        ref = SKILL_DIR / "references" / "eplb-metrics.md"
        self.assertTrue(ref.is_file())
        self.assertGreater(len(read(ref)), 3000)

    def test_evals_json(self):
        data = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        self.assertEqual(data["skill_name"], SKILL_DIR.name)
        ids = [e["id"] for e in data["evals"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in data["evals"]:
            self.assertTrue(e["assertions"])
            self.assertTrue(e["prompt"])

    def test_eval_queries_json(self):
        data = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))
        pos = [q for q in data["queries"] if q["should_trigger"]]
        neg = [q for q in data["queries"] if not q["should_trigger"]]
        self.assertGreaterEqual(len(pos), 8)
        self.assertGreaterEqual(len(neg), 8)


class TestHygiene(unittest.TestCase):
    def test_no_vendor_or_chip_words(self):
        hits = []
        for path in scanned_files():
            text = read(path)
            for word in VENDOR_WORDS:
                if word in text:
                    hits.append("%s: %s" % (path.relative_to(SKILL_DIR), word))
        self.assertEqual(hits, [], "发现厂商/型号词: %s" % hits)

    def test_no_employee_ids_or_personal_paths(self):
        hits = []
        for path in scanned_files():
            text = read(path)
            if EMPLOYEE_ID.search(text):
                hits.append("%s: employee-id" % path.relative_to(SKILL_DIR))
            if PERSONAL_PATH.search(text):
                hits.append("%s: personal-path" % path.relative_to(SKILL_DIR))
        self.assertEqual(hits, [], "发现工号/个人路径: %s" % hits)

    def test_lf_only(self):
        bad = [str(p.relative_to(SKILL_DIR)) for p in scanned_files() if b"\r" in p.read_bytes()]
        self.assertEqual(bad, [], "存在 CRLF 文件: %s" % bad)


class TestScriptContract(unittest.TestCase):
    def test_run_checks_all_green(self):
        r = subprocess.run([sys.executable, str(SKILL_DIR / "evals" / "run_checks.py")],
                           capture_output=True, text=True, cwd=str(SKILL_DIR))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks passed", r.stdout)


class TestLinkage(unittest.TestCase):
    def test_analysis_points_to_optimization(self):
        text = read(SKILL_DIR / "SKILL.md")
        self.assertIn("vllm-eplb-optimization", text)
        self.assertIn("apply_eplb.py", text)

    def test_optimization_points_back(self):
        if not SIBLING.is_dir():
            self.skipTest("sibling skill not present")
        text = read(SIBLING / "SKILL.md")
        self.assertIn("vllm-eplb-analysis", text)


class TestRegistration(unittest.TestCase):
    def _in_repo(self):
        return (REPO / "CHANGELOG.md").is_file()


    def test_changelog_mentions_skill(self):
        if not self._in_repo():
            self.skipTest("standalone copy")
        self.assertIn(SKILL_DIR.name, read(REPO / "CHANGELOG.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
