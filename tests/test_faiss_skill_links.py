"""Guard Faiss skill references against cross-skill relative file paths.

Skills are installed flat at ``<tool>/skills/<skill-name>/`` (see
``docs/STANDARDS.md`` and ``install.sh``), while the source tree groups them
under ``skills/vectordb/faiss/``. A relative link that escapes its own skill
directory therefore resolves differently in the two layouts -- or breaks after
installation. Cross-skill references must use the skill name instead of a
file path.

The parsers below cover the Markdown link forms used by the skills: inline
links with optional titles, angle-bracket destinations, and reference-style
links with their definitions. Titles are stripped before validation so that
``[text](path "title")`` is checked against ``path``.
"""

import re
import tempfile
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "Plugins/code-optimizer/skills"
FAISS_SUBDOMAIN = SKILLS / "vectordb/faiss"

# [text](<dest with space> "title") / [text](dest 'title') / [text](dest (title))
INLINE_LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(<[^>\n]*>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
# [id]: <dest> "title"
REF_DEF_RE = re.compile(
    r"^[ \t]{0,3}\[([^\]]+)\]:[ \t]*(<[^>\n]*>|\S+)"
    r"(?:[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?[ \t]*$",
    re.MULTILINE,
)
# [text][id]
REF_USE_RE = re.compile(r"!?\[[^\]]*\]\[([^\]]*)\]")

ABSOLUTE_PREFIXES = ("http://", "https://", "mailto:", "#", "/")


def normalize_target(target):
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return target.split("#", 1)[0]


def link_targets(text):
    targets = []
    definitions = {}
    for match in REF_DEF_RE.finditer(text):
        definitions[match.group(1).strip().lower()] = normalize_target(match.group(2))
    for match in INLINE_LINK_RE.finditer(text):
        targets.append(normalize_target(match.group(1)))
    for match in REF_USE_RE.finditer(text):
        key = match.group(1).strip().lower()
        if key and key in definitions:
            targets.append(definitions[key])
    return targets


def relative_targets(doc):
    for target in link_targets(doc.read_text(encoding="utf-8")):
        if not target or target.startswith(ABSOLUTE_PREFIXES):
            continue
        if "://" in target:
            continue
        yield target


def escaping_links(skill_root):
    problems = []
    root = skill_root.resolve()
    for doc in skill_root.rglob("*.md"):
        for target in relative_targets(doc):
            try:
                (doc.parent / target).resolve().relative_to(root)
            except ValueError:
                problems.append(f"{doc} -> {target}")
    return problems


def missing_links(skill_root):
    problems = []
    for doc in skill_root.rglob("*.md"):
        for target in relative_targets(doc):
            if not (doc.parent / target).exists():
                problems.append(f"{doc} -> {target}")
    return problems


def skill_dirs():
    return sorted(d for d in FAISS_SUBDOMAIN.iterdir() if (d / "SKILL.md").is_file())


class FaissSkillLinkTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FAISS_SUBDOMAIN.is_dir(), f"missing subdomain: {FAISS_SUBDOMAIN}")
        self.assertGreater(len(skill_dirs()), 0, "no Faiss skills found")

    def test_relative_link_does_not_escape_skill_dir(self):
        problems = []
        for skill in skill_dirs():
            problems += escaping_links(skill)
        self.assertEqual(
            [],
            problems,
            "cross-skill references must use the skill name, not a relative file path",
        )

    def test_intra_skill_relative_link_resolves_in_source_tree(self):
        problems = []
        for skill in skill_dirs():
            problems += missing_links(skill)
        self.assertEqual([], problems)

    def test_legacy_flat_faiss_dirs_are_removed(self):
        self.assertEqual(
            [],
            sorted(SKILLS.glob("faiss-*")),
            "old flat Faiss skill paths must not reappear",
        )


class LinkParserRegressionTests(unittest.TestCase):
    def _skill(self, tmp):
        root = Path(tmp) / "skill"
        root.mkdir()
        (root / "target.md").write_text("x", encoding="utf-8")
        (root / "with space.md").write_text("x", encoding="utf-8")
        return root

    def test_parses_angle_title_and_reference_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._skill(tmp)
            doc = root / "SKILL.md"
            doc.write_text(
                "[a](target.md)\n"
                "[b](<with space.md>)\n"
                "[c](target.md \"title\")\n"
                "[d](target.md 'title')\n"
                "[e](target.md (title))\n"
                "[ref][id]\n"
                "[id]: target.md \"title\"\n",
                encoding="utf-8",
            )
            self.assertEqual([], escaping_links(root))
            self.assertEqual([], missing_links(root))
            self.assertCountEqual(
                link_targets(doc.read_text(encoding="utf-8")),
                [
                    "target.md",
                    "with space.md",
                    "target.md",
                    "target.md",
                    "target.md",
                    "target.md",
                ],
            )

    def test_titled_existing_link_is_not_reported_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._skill(tmp)
            (root / "SKILL.md").write_text('[c](target.md "title")\n', encoding="utf-8")
            self.assertEqual([], missing_links(root))

    def test_escape_is_detected_in_titled_and_reference_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._skill(tmp)
            (root / "SKILL.md").write_text(
                '[x](<../outside.md> "title")\n'
                "[ref][out]\n"
                '[out]: ../outside.md "title"\n',
                encoding="utf-8",
            )
            self.assertEqual(2, len(escaping_links(root)), escaping_links(root))


if __name__ == "__main__":
    unittest.main()
