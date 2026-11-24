"""Agent frontmatter must stay loadable on both claude and opencode platforms.

Background: claude accepts a comma-separated scalar ``tools`` field in agent
frontmatter, but opencode rejects it at config load time ("Expected object |
undefined") and the whole session fails to start. The repo-wide convention is
therefore to not restrict tools via frontmatter at all (read-only behavior is
enforced by the agent's prompt), and every agent must keep required fields
(``name``/``description``) loadable on both platforms.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def agent_files():
    """Agent files on install.sh's registration surface.

    install.sh registers ``<skill>/agents/*.md`` for every installed skill,
    i.e. the immediate ``agents/`` directory of each directory holding a
    SKILL.md. Nested fixtures (e.g. ``cases/**/.opencode/agents/``) are
    skill-internal and never registered, so they are out of scope.
    """
    out = []
    for skill_md in (REPO / "Plugins").rglob("SKILL.md"):
        if "skills" not in skill_md.parts:
            continue
        agents_dir = skill_md.parent / "agents"
        if agents_dir.is_dir():
            out.extend(agents_dir.glob("*.md"))
    return sorted(set(out))


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    data = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            data[key.strip()] = value.strip()
    return data


class AgentFrontmatterCompatTests(unittest.TestCase):
    def test_agents_are_discovered(self):
        found = agent_files()
        self.assertGreaterEqual(
            len(found),
            15,
            "expected to find the repo's agent files; glob pattern broken?",
        )

    def test_required_fields_and_no_tools_key(self):
        problems = []
        for path in agent_files():
            meta = frontmatter(path)
            rel = path.relative_to(REPO).as_posix()
            if not meta:
                problems.append(f"{rel}: no YAML frontmatter")
                continue
            if not meta.get("name"):
                problems.append(f"{rel}: missing name")
            if not meta.get("description"):
                problems.append(f"{rel}: missing description")
            if "tools" in meta:
                problems.append(
                    f"{rel}: tools key breaks opencode sessions "
                    "(claude-only format; use prompt constraints instead)"
                )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
