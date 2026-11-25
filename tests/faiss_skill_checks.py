"""Shared contract checks for skills under the Faiss subdomain."""

import re
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "Plugins/code-optimizer/skills"
FAISS = SKILLS / "vectordb/faiss"
DOMAIN = FAISS / "DOMAIN.md"

REQUIRED_SECTIONS = ("## 输入", "## 执行步骤", "## 输出", "## 约束")
SKILL_REF_RE = re.compile(r"`(faiss-[a-z0-9-]+)`")


def skill_dir(name):
    return FAISS / name


def skill_text(name):
    return (skill_dir(name) / "SKILL.md").read_text(encoding="utf-8")


def frontmatter(name):
    text = skill_text(name)
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    data = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            data[key.strip()] = value.strip()
    return data


def domain_text():
    return DOMAIN.read_text(encoding="utf-8")


def domain_section(title):
    text = domain_text()
    marker = f"## {title}"
    start = text.find(marker)
    if start == -1:
        raise AssertionError(f"DOMAIN.md missing section: {marker}")
    start += len(marker)
    end = text.find("\n## ", start)
    return text[start : end if end != -1 else len(text)]


def domain_members():
    return set(re.findall(r"\| \[`(faiss-[a-z0-9-]+)`\]\(", domain_text()))


def domain_known_skills():
    return set(SKILL_REF_RE.findall(domain_text()))


def assert_skill_contract(case: unittest.TestCase, name):
    directory = skill_dir(name)
    case.assertTrue((directory / "SKILL.md").is_file(), f"missing SKILL.md: {name}")

    meta = frontmatter(name)
    case.assertEqual(name, meta.get("name"), f"frontmatter name mismatch: {name}")
    case.assertTrue(meta.get("description"), f"empty description: {name}")

    text = skill_text(name)
    for section in REQUIRED_SECTIONS:
        case.assertIn(section, text, f"{name} missing {section}")

    known = domain_known_skills()
    for ref in sorted(set(SKILL_REF_RE.findall(text))):
        case.assertIn(ref, known, f"{name} references unregistered skill: {ref}")

    case.assertIn(name, domain_members(), f"{name} not registered in DOMAIN members")
    case.assertIn(
        name,
        domain_section("路由"),
        f"{name} has no DOMAIN routing entry",
    )
