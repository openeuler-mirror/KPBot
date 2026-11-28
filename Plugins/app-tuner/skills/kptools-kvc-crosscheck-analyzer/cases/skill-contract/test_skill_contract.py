#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tier-A contract tests for the kptools-kvc-crosscheck-analyzer skill (stdlib only).

覆盖：
  SKILL.md       frontmatter（name 与目录一致、description 含触发词、metadata 完整）
  资源完整性      evals/evals.json、evals/eval_queries.json
  内容卫生        无设备厂商/型号词、无工号、无个人绝对路径、LF 行尾
  自包含         不得引用未随技能交付的采集/解析/部署脚本
  触发面         description 覆盖四源对账场景的高频触发说法

注册说明（与其他技能不同的两处，均为有意为之）：
  1. 仓库 CHANGELOG.md 按 ITS 文首声明只记录 npm 包（`kpbot`）发布相关的变更，
     技能注册不走 CHANGELOG，因此本套件不含 CHANGELOG 条目检查。
  2. 技能的安装/发现注册面由 tests/test_codex_install.py 的计数回归守护
     （tests/fixtures/codex-skill-counts.json 三口径计数）。增删本技能时，
     该 fixture 与 docs/codex.md 计数表必须随同一个 PR 各 +1，由该回归断言。

运行：
  bash Plugins/app-tuner/skills/kptools-kvc-crosscheck-analyzer/cases/skill-contract/run.sh
  产物 results/result.json；退出码 0 = 全部通过
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

SCAN_DIRS = ("evals",)
SCAN_FILES = ("SKILL.md",)

VENDOR_WORDS = ["".join(p) for p in (
    ("鲲", "鹏"), ("昇", "腾"), ("kun", "peng"), ("Kun", "peng"),
    ("As", "cend"), ("as", "cend"), ("N", "PU"), ("910", "B"),
    ("At", "las"), ("H", "BM"),
)]
EMPLOYEE_ID = re.compile(r"\b[lw]\d{8}\b")
PERSONAL_PATH = re.compile(r"/home/[A-Za-z0-9_.-]+|C:\\Users\\")

TRIGGER_TOKENS = ("总量对不上", "放大几倍", "每传输段", "RoCE")

# 采集/解析/部署属于上游或相邻技能的职责，本技能不得引用这些未随包交付的脚本与工具
DANGLING_REFS = (
    "deploy_mooncake_so.sh", "analyze_ttft.py", "collect_env.sh",
    "deploy_ttft_full.sh", "probe_mooncake.sh", "hccn_tool",
    "kptools-ascend-", "kptools-mooncake-", "kptools-tpsplit-",
    "kptools-dplb-", "kptools-nmon", "kptools-network-collector",
)


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
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"]


def index_eol(path):
    """已入库文件以 git 索引的 eol 为准（i/lf、i/crlf），未入库返回 None。

    这样 Windows 上 autocrlf 检出产生的工作区 CRLF 不会误报；真正以 CRLF
    形态提交进仓库的文件（i/crlf）才会判失败。无 git 或独立拷贝时返回 None，
    由调用方回退到原始字节检查。
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "--eol", "--", str(path)],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"i/(lf|crlf|mixed|none)", r.stdout)
    return m.group(1) if m else None


class TestFrontmatter(unittest.TestCase):
    def test_name_matches_directory(self):
        self.assertEqual(frontmatter()["name"], SKILL_DIR.name)

    def test_description_covers_triggers(self):
        desc = frontmatter()["description"]
        for token in TRIGGER_TOKENS:
            self.assertIn(token, desc, "description 缺少触发词 %s" % token)

    def test_metadata_complete(self):
        fm = frontmatter()
        self.assertTrue(fm.get("license"))
        for key in ("skill_type", "category", "domain", "activation_mode", "version"):
            self.assertTrue(fm.get("metadata.%s" % key), "metadata.%s 缺失" % key)
        self.assertEqual(fm.get("metadata.side_effect_free"), "true")


class TestResources(unittest.TestCase):
    def test_evals_json(self):
        data = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        self.assertEqual(data["skill_name"], SKILL_DIR.name)
        ids = [e["id"] for e in data["evals"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in data["evals"]:
            self.assertTrue(e["prompt"])
            self.assertTrue(e["expected_output"])
            self.assertTrue(e["assertions"], "eval %s 缺 assertions" % e.get("id"))
            self.assertTrue(e.get("eval_name"), "eval %s 缺 eval_name" % e.get("id"))

    def test_eval_queries_json(self):
        data = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))
        self.assertEqual(data["skill_name"], SKILL_DIR.name)
        pos = [q for q in data["queries"] if q["should_trigger"]]
        neg = [q for q in data["queries"] if not q["should_trigger"]]
        self.assertGreaterEqual(len(pos), 8)
        self.assertGreaterEqual(len(neg), 8)
        for q in data["queries"]:
            self.assertTrue(q["prompt"])
            self.assertTrue(q.get("note"))


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
        bad = []
        for path in scanned_files():
            eol = index_eol(path)
            if eol is None:
                eol = "crlf" if b"\r" in path.read_bytes() else "lf"
            if eol in ("crlf", "mixed"):
                bad.append("%s (%s)" % (path.relative_to(SKILL_DIR), eol))
        self.assertEqual(bad, [], "存在 CRLF 行尾: %s" % bad)


class TestSelfContained(unittest.TestCase):
    def test_no_dangling_external_refs(self):
        """技能不得指向未随包交付的采集/解析/部署脚本或相邻技能内部资源。"""
        hits = []
        for path in scanned_files():
            text = read(path)
            for token in DANGLING_REFS:
                if token in text:
                    hits.append("%s: %s" % (path.relative_to(SKILL_DIR), token))
        self.assertEqual(hits, [], "引用了未随技能交付的资源: %s" % hits)

    def test_pure_analysis_no_scripts_dir(self):
        """本技能声明为纯 LLM 分析（无脚本、无副作用），不得携带 scripts/ 目录。"""
        self.assertFalse((SKILL_DIR / "scripts").is_dir(),
                         "纯分析技能不应有 scripts/；若有请同步更新 compatibility 与本套件")


class TestTriggerSurface(unittest.TestCase):
    def test_skill_discoverable_at_install_surface(self):
        """安装器按 skills/<name>/SKILL.md 枚举技能；本技能必须位于该表面。"""
        self.assertTrue(SKILL_DIR.is_dir())
        self.assertTrue((SKILL_DIR / "SKILL.md").is_file())
        self.assertEqual(SKILL_DIR.parent.name, "skills")

    def test_description_pushes_common_paraphrases(self):
        """对账场景的口语化说法也应覆盖（欠触发防护）。"""
        desc = frontmatter()["description"]
        for token in ("帮我核一下", "对不上", "放大几倍"):
            self.assertIn(token, desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
