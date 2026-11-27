"""Install KPBot skills for Codex without changing user instructions or settings."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[2]
MANIFEST = "kpbot-codex-manifest.json"
OWNER = ".kpbot-codex-owner.json"
COMPATIBILITY = """
## Codex platform integration

Apply this platform mapping when the instructions below name another tool:

- Resolve scripts and references from this installed skill's directory. Skills are
  installed under `.agents/skills` in the project or the user's home directory.
- Treat `Skill`, slash commands, and named workflow stages as instructions to read
  the matching installed SKILL.md and carry out its steps with available tools.
- Map `TaskCreate`, `TaskUpdate`, and `TaskList` to the available planning tool or
  a local progress record; do not attempt to call tools absent from this session.
- Treat `AskUserQuestion` as a request for clarification only when the missing
  answer changes execution. Use an available input tool or ask in plain text.
  Silence and elapsed time do not grant approval.
- Run stages locally by default. Use subagents only when delegation is authorized
  and the current environment provides that capability. Files in `agents/` are
  role references within the skill, not registered Codex subagent configurations.
  If a stage requires independent review, keep that requirement pending until an
  actual independent reviewer is available; a local recheck is not independent.
- Keep Codex permissions and the user's confirmed scope in force. Other tools'
  allowlists, hooks, auto modes, and model names do not configure this session.

"""


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("Invalid skill name: " + repr(name))
    return name


def is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def check_directory(path):
    if is_link(path) or (path.exists() and not path.is_dir()):
        raise ValueError("Expected a directory without links: " + str(path))


def local_npm_artifacts(directory, names):
    if "SKILL.md" not in names or "package-lock.json" not in names:
        return set()
    candidates = (Path(directory) / name for name in ("node_modules", "test-output") if name in names)
    return {path.name for path in candidates if not is_link(path) and path.is_dir()}


def skill_resources(root):
    yield root
    if root.is_dir() and not is_link(root):
        names = os.listdir(root)
        ignored = local_npm_artifacts(root, names)
        for name in names:
            if name not in ignored:
                yield from skill_resources(root / name)


def check_tree(root):
    for path in skill_resources(root):
        if is_link(path) or not (path.is_file() or path.is_dir()):
            raise ValueError("Skill resources must be regular files or directories: " + str(path))


def read_json(path):
    if is_link(path) or not path.is_file():
        raise ValueError("Expected a regular metadata file: " + str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Metadata must be a JSON object: " + str(path))
    return data


def plugin_config(path):
    data = read_json(path)
    platforms = data.get("platforms", [])
    if not isinstance(platforms, list) or any(not isinstance(value, str) for value in platforms):
        raise ValueError("Plugin platforms must be a list of strings: " + str(path))
    if "codex" not in platforms:
        return None
    config = data.get("codex", {})
    if not isinstance(config, dict):
        raise ValueError("Plugin codex settings must be a JSON object: " + str(path))
    excludes = config.get("exclude", [])
    if not isinstance(excludes, list):
        raise ValueError("Plugin codex.exclude must be a list of skill names: " + str(path))
    return {validate_name(name) for name in excludes}


def config_root(level, install_path=None):
    if level not in ("project", "global"):
        raise ValueError("Unknown installation level: " + str(level))
    default_base = (os.environ.get("HOME") or Path.home()) if level == "global" else Path.cwd()
    base = str(install_path or default_base)
    if os.name == "nt":
        base = re.sub(r"\A/(?:cygdrive/)?([A-Za-z])(?:/|\Z)", lambda match: match.group(1) + ":/", base)
    return Path(base).expanduser().resolve() / ".agents"


def managed_state(root):
    check_directory(root)
    check_directory(root / "skills")
    path = root / MANIFEST
    if not path.exists() and not is_link(path):
        return []
    state = read_json(path)
    if not isinstance(state, dict) or state.get("brand") != "KPBot" or state.get("tool") != "codex":
        raise ValueError("Unrecognized Codex installation manifest")
    names = state.get("installed_skills")
    if not isinstance(names, list):
        raise ValueError("Invalid installed_skills in Codex manifest")
    for name in names:
        validate_name(name)
    if len(names) != len(set(names)):
        raise ValueError("Duplicate installed skills in Codex manifest")
    return names


def verify_owned(target):
    check_directory(target)
    if target.exists():
        owner = read_json(target / OWNER)
        if owner != {"brand": "KPBot", "tool": "codex", "name": target.name}:
            raise ValueError("Skill is not owned by KPBot: " + str(target))


def skill_document(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        raise ValueError("Missing skill frontmatter: " + str(path))
    header = match.group(1)
    name = re.search(r"^name:[ \t]*([^\r\n]+)", header, re.MULTILINE)
    if not name or not re.search(r"^description:[ \t]*\S", header, re.MULTILINE):
        raise ValueError("Skill requires a name and description: " + str(path))
    return validate_name(name.group(1).strip().strip("\"'")), text, match.end()


def plugin_manifests(directory):
    check_directory(directory)
    if not directory.exists():
        return
    metadata_directory = directory / ".claude-plugin"
    check_directory(metadata_directory)
    metadata = metadata_directory / "plugin.json"
    if metadata.exists() or is_link(metadata):
        yield metadata
        return
    for child in sorted(directory.iterdir()):
        if is_link(child):
            raise ValueError("Plugin paths must be regular files or directories without links: " + str(child))
        if child.is_dir():
            yield from plugin_manifests(child)


def discover_skills(repository):
    catalog = {}
    for metadata in plugin_manifests(repository / "Plugins"):
        plugin = metadata.parent.parent
        excludes = plugin_config(metadata)
        if excludes is None:
            continue
        check_directory(plugin / "skills")
        for package in sorted((plugin / "skills").glob("*")):
            if package.name in excludes:
                continue
            if is_link(package):
                raise ValueError("Skill resources must be regular files or directories: " + str(package))
            if not package.is_dir():
                continue
            check_tree(package)
            documents = []
            if (package / "SKILL.md").is_file():
                documents.append(package / "SKILL.md")
            else:
                documents.extend(sorted(package.glob("*/SKILL.md")))
                documents.extend(sorted(package.glob("skills/*/SKILL.md")))
            if not documents:
                if sorted(child.name for child in package.iterdir()) == ["DOMAIN.md"] and (package / "DOMAIN.md").is_file():
                    continue
                raise ValueError("Missing SKILL.md or grouped skill entrypoints: " + str(package))
            for document in documents:
                name, _, _ = skill_document(document)
                if name in excludes or document.parent.name in excludes:
                    continue
                if name in catalog:
                    raise ValueError("Duplicate Codex skill name: " + name)
                catalog[name] = (document.parent, package, plugin)
    if not catalog:
        raise ValueError("No plugins declare Codex skills")
    return catalog


def prepare_skill(repository, destination, source, package, plugin):
    ignore_caches = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")

    def ignore(directory, names):
        return ignore_caches(directory, names) | local_npm_artifacts(directory, names)

    check_tree(source)
    shutil.copytree(source, destination, ignore=ignore)
    if source != package:
        for resource in ("references", "scripts", "subskills", "agents"):
            fallback = package / resource
            if fallback.is_dir() and not (destination / resource).exists():
                check_tree(fallback)
                shutil.copytree(fallback, destination / resource, ignore=ignore)
    check_directory(plugin / "codex")
    overlay = plugin / "codex" / source.name
    if overlay.is_dir():
        check_tree(overlay)
        shutil.copytree(overlay, destination, dirs_exist_ok=True, ignore=ignore)
    excludes = plugin_config(plugin / ".claude-plugin/plugin.json")
    if excludes is None:
        raise ValueError("Plugin no longer declares Codex support: " + str(plugin))
    for document in destination.rglob("SKILL.md"):
        name, text, body_start = skill_document(document)
        if name in excludes or document.parent.name in excludes:
            document.unlink()
            continue
        if document == destination / "SKILL.md" and name != destination.name:
            raise ValueError("Codex overlay changed the skill name: " + str(document))
        body = text[body_start:]
        for old, new in (
            ("$HOME/.config/opencode/skills/", "$HOME/.agents/skills/"),
            ("$HOME/.claude/skills/", "$HOME/.agents/skills/"),
            (".opencode/skills/", ".agents/skills/"),
            (".claude/skills/", ".agents/skills/"),
        ):
            body = body.replace(old, new)
        document.write_text(text[:body_start] + COMPATIBILITY + body, encoding="utf-8")
    text = (destination / "SKILL.md").read_text(encoding="utf-8")
    for shared in (repository / "Plugins/common/scripts").glob("*.sh"):
        target = destination / "scripts" / shared.name
        if shared.name in text and not target.exists():
            check_directory(shared.parent)
            check_tree(shared)
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(shared, target)
    for script in destination.rglob("*.sh"):
        script.chmod(script.stat().st_mode | 0o111)
    (destination / OWNER).write_text(json.dumps({
        "brand": "KPBot", "tool": "codex", "name": destination.name,
    }) + "\n", encoding="utf-8")


def install(repository, root, level):
    previous = managed_state(root)
    manifest_path = root / MANIFEST
    previous_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    catalog = discover_skills(repository)
    destination = root.resolve()
    resource_roots = {plugin / name for _, _, plugin in catalog.values() for name in ("skills", "codex")}
    # Shared scripts and undeclared plugins are also source resources.
    resource_roots.add(repository / "Plugins")
    for resource in resource_roots:
        source_root = resource.resolve()
        if destination == source_root or source_root in destination.parents or destination in source_root.parents:
            raise ValueError("Installation directory overlaps skill resources: " + str(root))
    version = read_json(repository / "package.json").get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Package version must be a nonempty string")
    names = sorted(set(previous) | set(catalog))
    for name in names:
        verify_owned(root / "skills" / name)
    existing = {name for name in names if (root / "skills" / name).exists()}
    root.mkdir(parents=True, exist_ok=True)
    skills = root / "skills"
    skills.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".kpbot-codex-", dir=root))
    preserve_stage = False
    try:
        fresh = stage / "new"
        backup = stage / "old"
        fresh.mkdir()
        backup.mkdir()
        for name, (source, package, plugin) in catalog.items():
            prepare_skill(repository, fresh / name, source, package, plugin)
        discovered_names = set()
        for document in fresh.rglob("SKILL.md"):
            name, _, _ = skill_document(document)
            if name in discovered_names:
                raise ValueError("Duplicate nested Codex skill name: " + name)
            discovered_names.add(name)
        manifest = stage / MANIFEST
        manifest.write_text(json.dumps({
            "brand": "KPBot", "tool": "codex", "level": level, "version": version,
            "installed_skills": sorted(catalog),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        current = managed_state(root)
        current_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
        if current != previous or current_manifest != previous_manifest:
            raise ValueError("Codex installation changed during preparation")
        for name in names:
            target = skills / name
            if target.exists() and name not in existing:
                raise ValueError("Skill directory conflict during preparation: " + str(target))
            verify_owned(target)
        saved = []
        published = []
        try:
            for name in names:
                if (skills / name).exists():
                    (skills / name).rename(backup / name)
                    saved.append(name)
            for name in sorted(catalog):
                (fresh / name).rename(skills / name)
                published.append(name)
            os.replace(manifest, root / MANIFEST)
        except Exception:
            failures = []
            for name in published:
                try:
                    shutil.rmtree(skills / name)
                except OSError as exc:
                    failures.append(exc)
            for name in saved:
                try:
                    (backup / name).rename(skills / name)
                except OSError as exc:
                    failures.append(exc)
            if failures:
                preserve_stage = True
                raise OSError("Codex rollback incomplete; recovery files preserved in " + str(stage)) from failures[0]
            raise
    finally:
        if not preserve_stage:
            shutil.rmtree(stage)
    return len(catalog)


def uninstall(root):
    names = managed_state(root)
    for name in names:
        verify_owned(root / "skills" / name)
    for name in names:
        target = root / "skills" / name
        if target.exists():
            shutil.rmtree(target)
    (root / MANIFEST).unlink(missing_ok=True)
    return len(names)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install or uninstall KPBot Codex skills")
    parser.add_argument("command", choices=("install", "uninstall"))
    parser.add_argument("arguments", nargs="*", metavar="[project|global] codex [path]")
    args = parser.parse_args(argv)
    tokens = list(args.arguments)
    level = tokens.pop(0) if tokens and tokens[0] in ("project", "global") else "project"
    if not tokens or tokens.pop(0) != "codex" or len(tokens) > 1:
        parser.error("expected [project|global] codex [path]")
    try:
        root = config_root(level, tokens[0] if tokens else None)
        if args.command == "install":
            install(REPOSITORY, root, level)
        else:
            uninstall(root)
    except (OSError, ValueError) as exc:
        print("KPBot Codex: " + str(exc), file=sys.stderr)
        return 1
    print("KPBot Codex: {} completed in {}".format(args.command, root / "skills"))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
