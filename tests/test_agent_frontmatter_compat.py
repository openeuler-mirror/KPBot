"""Agent frontmatter must stay loadable on both claude and opencode platforms.

Background: claude accepts a comma-separated scalar ``tools`` whitelist in
agent frontmatter, but opencode rejects it at config load time ("Expected
object | undefined") and the whole session fails to start. The repo-wide
policy is therefore: agents may carry a claude-side ``tools`` whitelist only
when an opencode overlay (``<plugin>/opencode/<skill>/agents/<same file>``)
provides a counterpart without the ``tools`` key. Overlay skill directories
must also contain a SKILL.md, because both installers enumerate overlays by
SKILL.md and silently skip a directory that lacks one.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def surface_agents():
    """(skill_md, agent_file) pairs on the installers' registration surface.

    install.sh / install.ps1 register ``<skill>/agents/*.md`` for every
    installed skill, i.e. the immediate ``agents/`` directory of each
    directory holding a SKILL.md. Nested fixtures (e.g.
    ``cases/**/.opencode/agents/``) are skill-internal and out of scope.
    """
    out = []
    for skill_md in (REPO / "Plugins").rglob("SKILL.md"):
        if "skills" not in skill_md.parts:
            continue
        agents_dir = skill_md.parent / "agents"
        if agents_dir.is_dir():
            for agent in agents_dir.glob("*.md"):
                out.append((skill_md, agent))
    return sorted(set(out))


def overlay_root(skill_md):
    """opencode overlay skill dir for a skill: <plugin>/opencode/<skill-name>."""
    parts = skill_md.relative_to(REPO).parts
    idx = parts.index("skills")
    plugin = REPO.joinpath(*parts[:idx])
    return plugin / "opencode" / skill_md.parent.name


def overlay_agents():
    out = []
    for ov_root in (REPO / "Plugins").rglob("opencode"):
        for agents_dir in ov_root.glob("*/agents"):
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


def rel(path):
    return path.relative_to(REPO).as_posix()


class AgentFrontmatterCompatTests(unittest.TestCase):
    def test_agents_are_discovered(self):
        found = surface_agents()
        self.assertGreaterEqual(
            len(found),
            15,
            "expected to find the repo's agent files; glob pattern broken?",
        )

    def test_surface_agents_have_required_fields(self):
        problems = []
        for skill_md, path in surface_agents():
            meta = frontmatter(path)
            if not meta:
                problems.append(f"{rel(path)}: no YAML frontmatter")
                continue
            if not meta.get("name"):
                problems.append(f"{rel(path)}: missing name")
            if not meta.get("description"):
                problems.append(f"{rel(path)}: missing description")
        self.assertEqual(problems, [])

    def test_tools_whitelist_requires_opencode_overlay(self):
        problems = []
        for skill_md, path in surface_agents():
            meta = frontmatter(path)
            if "tools" not in meta:
                continue
            counterpart = overlay_root(skill_md) / "agents" / path.name
            if not counterpart.is_file():
                problems.append(
                    f"{rel(path)}: claude-only tools whitelist needs an "
                    f"opencode overlay counterpart at {rel(counterpart)}"
                )
                continue
            ometa = frontmatter(counterpart)
            if "tools" in ometa:
                problems.append(f"{rel(counterpart)}: overlay keeps tools key")
            if not ometa.get("name") or not ometa.get("description"):
                problems.append(
                    f"{rel(counterpart)}: overlay agent missing name/description"
                )
        self.assertEqual(problems, [])

    def test_overlay_skill_dirs_are_enumerable(self):
        problems = []
        for agent in overlay_agents():
            skill_dir = agent.parents[1]
            if not (skill_dir / "SKILL.md").is_file():
                problems.append(
                    f"{rel(skill_dir)}: overlay without SKILL.md is skipped "
                    "by install.sh and install.ps1"
                )
            meta = frontmatter(agent)
            if "tools" in meta:
                problems.append(f"{rel(agent)}: opencode agent carries tools key")
        self.assertEqual(problems, [])

    def test_overlay_skill_md_stays_in_sync(self):
        main_skills = {}
        for skill_md in (REPO / "Plugins").rglob("SKILL.md"):
            if "skills" in skill_md.parts:
                main_skills[skill_md.parent.name] = skill_md
        problems = []
        for agent in overlay_agents():
            overlay_dir = agent.parents[1]
            main = main_skills.get(overlay_dir.name)
            if main is None:
                problems.append(f"{rel(overlay_dir)}: no main skill to sync with")
                continue
            overlay_md = overlay_dir / "SKILL.md"
            if overlay_md.read_bytes() != main.read_bytes():
                problems.append(
                    f"{rel(overlay_md)}: overlay SKILL.md must stay a "
                    f"byte-identical copy of {rel(main)} (agents-carrying "
                    "overlays exist only to swap platform-specific files)"
                )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
