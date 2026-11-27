"""Contract tests for Codex discovery, installation, and CLI dispatch."""

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_install", REPOSITORY / "adapters/codex/install.py")
codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex)
BASH = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else Path(shutil.which("bash") or "/bin/bash")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path, document):
    write(path, json.dumps(document))


def shell_path(path):
    path = Path(path).resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:] if os.name == "nt" else str(path)


@contextlib.contextmanager
def directory_link(target, link):
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, check=True, timeout=10)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        yield
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


class CodexInstallTests(unittest.TestCase):
    def setUp(self):
        temp_root = REPOSITORY / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="codex-install-", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repository = self.directory / "package"
        self.project = self.directory / "project with spaces"
        self.root = self.project / ".agents"
        write_json(self.repository / "package.json", {"version": "test"})
        self.plugin = self.add_plugin("optimizer")
        self.add_skill(self.plugin, "example")

    def add_plugin(self, name, platforms=None, exclude=None):
        plugin = self.repository / "Plugins" / name
        write_json(plugin / ".claude-plugin/plugin.json", {
            "platforms": ["codex"] if platforms is None else platforms,
            "codex": {"exclude": exclude or []},
        })
        return plugin

    def add_skill(self, plugin, relative, name=None, body="Run the example."):
        source = plugin / "skills" / relative
        write(source / "SKILL.md", "---\nname: " + (name or source.name)
              + "\ndescription: Example contract skill\n---\n\n" + body + "\n")
        return source

    def install(self):
        return codex.install(self.repository, self.root, "project")

    def installed(self, name="example"):
        return self.root / "skills" / name

    def assert_overlap_rejected(self, target):
        before = {path.relative_to(self.repository): path.read_bytes()
                  for path in self.repository.rglob("*") if path.is_file()}
        with mock.patch.object(codex.shutil, "copytree", side_effect=AssertionError("Unexpected recursive copy")) as copy:
            with self.assertRaisesRegex(ValueError, "overlaps skill resources"):
                codex.install(self.repository, target, "project")
            copy.assert_not_called()
        after = {path.relative_to(self.repository): path.read_bytes()
                 for path in self.repository.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((target / "skills").exists())

    def test_destination_inside_skill_resources_is_rejected_before_copying(self):
        target = self.plugin / "skills/example/.agents"
        self.assert_overlap_rejected(target)
        self.assertFalse(target.exists())

    def test_destinations_inside_any_plugin_resources_are_rejected_before_copying(self):
        for relative in ("Plugins/common/scripts", "Plugins/unsupported/resources", "Plugins/new-resource"):
            with self.subTest(relative=relative):
                source = self.repository / relative
                write(source / "preserve.txt", "Source resource")
                target = codex.config_root("project", source)
                self.assert_overlap_rejected(target)
                self.assertFalse(target.exists())

    def test_global_destination_inside_shared_scripts_is_rejected_before_copying(self):
        source = self.repository / "Plugins/common/scripts"
        write(source / "preserve.txt", "Shared script resource")
        target = codex.config_root("global", source)
        self.assert_overlap_rejected(target)
        self.assertFalse(target.exists())

    def test_destination_inside_group_fallback_is_rejected_before_copying(self):
        self.add_skill(self.plugin, "group/child")
        resource = self.plugin / "skills/group/references"
        write(resource / "guide.md", "Fallback resource")
        target = resource / ".agents"
        self.assert_overlap_rejected(target)
        self.assertFalse(target.exists())

    def test_destination_inside_overlay_is_rejected_before_copying(self):
        target = self.plugin / "codex/example/.agents"
        write(target.parent / "SKILL.md", "---\nname: example\ndescription: Overlay\n---\nBody")
        self.assert_overlap_rejected(target)
        self.assertFalse(target.exists())

    def test_destination_containing_the_source_is_rejected_before_copying(self):
        moved = self.root / "source"
        moved.parent.mkdir(parents=True)
        self.repository.rename(moved)
        self.repository = moved
        self.assert_overlap_rejected(self.root)

    def test_project_root_may_contain_the_repository(self):
        target = codex.config_root("project", self.directory)
        self.assertEqual(codex.install(self.repository, target, "project"), 1)
        self.assertTrue((target / "skills/example/SKILL.md").is_file())

    def test_project_and_global_directories(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.directory / "home")}):
            self.assertEqual(codex.config_root("global"), self.directory / "home/.agents")
            self.assertEqual(codex.config_root("project", self.project), self.root)
            self.assertEqual(codex.config_root("global", self.project), self.root)
        with mock.patch.object(codex.Path, "cwd", return_value=self.project):
            self.assertEqual(codex.config_root("project"), self.root)

    def test_filters_plugins_and_skills_using_codex_metadata(self):
        self.add_skill(self.add_plugin("foreign", platforms=["opencode"]), "foreign")
        self.add_skill(self.add_plugin("legacy", platforms=[]), "legacy")
        blocked = self.add_plugin("filtered", exclude=["blocked"])
        self.add_skill(blocked, "blocked")
        self.add_skill(blocked, "kept")
        self.assertEqual(self.install(), 2)
        self.assertEqual(codex.managed_state(self.root), ["example", "kept"])
        self.assertFalse(self.installed("blocked").exists())
        self.assertFalse(self.installed("foreign").exists())
        self.assertFalse(self.installed("legacy").exists())

    def test_embedded_plugin_manifest_does_not_create_a_second_installation_root(self):
        vendor = self.add_plugin("optimizer/skills/example/references/vendor")
        self.add_skill(vendor, "vendored")
        self.assertEqual(self.install(), 1)
        self.assertEqual(codex.managed_state(self.root), ["example"])
        self.assertFalse(self.installed("vendored").exists())
        resource = self.installed() / "references/vendor/skills/vendored/SKILL.md"
        self.assertTrue(resource.is_file())
        self.assertEqual(codex.skill_document(resource)[0], "vendored")
        self.assertIn("Codex platform integration", resource.read_text(encoding="utf-8"))

    def test_embedded_invalid_plugin_metadata_is_preserved_as_a_resource(self):
        resource = Path("references/vendor/.claude-plugin/plugin.json")
        write(self.plugin / "skills/example" / resource, "Not plugin metadata")
        self.assertEqual(self.install(), 1)
        self.assertEqual((self.installed() / resource).read_text(), "Not plugin metadata")

    def test_unsupported_plugin_cannot_opt_in_through_nested_vendor_metadata(self):
        self.add_plugin("foreign", platforms=["opencode"])
        vendor = self.add_plugin("foreign/vendor/package")
        self.add_skill(vendor, "foreign-skill")
        self.assertEqual(self.install(), 1)
        self.assertFalse(self.installed("foreign-skill").exists())

    def test_grouped_plugin_discovery_survives_moves_at_different_depths(self):
        plugin = self.add_plugin("domains/deep/package")
        self.add_skill(plugin, "deep-skill")
        self.assertEqual(self.install(), 2)
        moved = self.repository / "Plugins/another/deeper/domain/moved"
        moved.parent.mkdir(parents=True)
        plugin.rename(moved)
        self.assertEqual(self.install(), 2)
        self.assertEqual(codex.managed_state(self.root), ["deep-skill", "example"])

    def test_linked_source_ancestors_cannot_replace_an_existing_installation(self):
        self.install()
        original = {path.relative_to(self.root): path.read_bytes()
                    for path in self.root.rglob("*") if path.is_file()}
        for index, relative in enumerate(("optimizer/.claude-plugin", "optimizer/skills", "optimizer", ".")):
            with self.subTest(relative=relative):
                source = self.repository / "Plugins" / relative
                outside = self.directory / ("outside-source-" + str(index))
                source.rename(outside)
                try:
                    with directory_link(outside, source):
                        with self.assertRaisesRegex(ValueError, "links|regular files"):
                            self.install()
                finally:
                    outside.rename(source)
                current = {path.relative_to(self.root): path.read_bytes()
                           for path in self.root.rglob("*") if path.is_file()}
                self.assertEqual(current, original)

    def test_linked_group_container_cannot_supply_external_skills(self):
        external = self.directory / "outside-group"
        self.add_skill(external, "child")
        with directory_link(external, self.plugin / "skills/group"):
            with self.assertRaisesRegex(ValueError, "links|regular files"):
                self.install()
        self.assertFalse(self.root.exists())

    def test_linked_overlay_parent_cannot_supply_external_instructions(self):
        external = self.directory / "outside-overlay"
        write(external / "example/SKILL.md", "---\nname: example\ndescription: External\n---\nExternal body")
        with directory_link(external, self.plugin / "codex"):
            with self.assertRaisesRegex(ValueError, "links|regular files"):
                self.install()
        self.assertEqual((external / "example/SKILL.md").read_text(),
                         "---\nname: example\ndescription: External\n---\nExternal body")
        self.assertFalse(self.installed().exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_linked_common_parent_cannot_supply_external_shared_scripts(self):
        self.add_skill(self.plugin, "example", body="bash scripts/print_logo.sh")
        external = self.directory / "outside-common"
        write(external / "scripts/print_logo.sh", "#!/bin/sh\necho external\n")
        with directory_link(external, self.repository / "Plugins/common"):
            with self.assertRaisesRegex(ValueError, "links|regular files"):
                self.install()
        self.assertFalse(self.installed().exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))
        self.assertEqual((external / "scripts/print_logo.sh").read_text(), "#!/bin/sh\necho external\n")

    def test_copies_resources_and_adapts_only_installed_instructions(self):
        source = self.plugin / "skills/example"
        self.add_skill(self.plugin, "example", body="bash .claude/skills/example/scripts/print_logo.sh\n"
                       'bash "$HOME/.config/opencode/skills/example/scripts/print_logo.sh"')
        original = (source / "SKILL.md").read_text(encoding="utf-8")
        write(source / "references/data.json", '{"fixture": true}')
        write(self.repository / "Plugins/common/scripts/print_logo.sh", "#!/usr/bin/env bash\necho KPBot\n")
        self.install()
        installed = self.installed()
        text = (installed / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(".agents/skills/example/", text)
        self.assertIn("$HOME/.agents/skills/example/", text)
        self.assertNotIn(".claude/skills/", text)
        self.assertIn("Codex platform integration", text)
        self.assertEqual((source / "SKILL.md").read_text(encoding="utf-8"), original)
        self.assertTrue((installed / "references/data.json").is_file())
        self.assertTrue((installed / "scripts/print_logo.sh").is_file())

    def test_nested_skills_keep_package_resources_and_stable_names(self):
        package = self.add_skill(self.plugin, "bundle")
        write(package / "references/contract.json", "{}")
        self.add_skill(self.plugin, "bundle/skills/inner_dir", name="inner-skill")
        self.add_skill(self.plugin, "infra/cube")
        self.assertEqual(self.install(), 3)
        self.assertFalse(self.installed("inner-skill").exists())
        self.assertTrue((self.installed("bundle") / "references/contract.json").is_file())
        self.assertTrue((self.installed("cube") / "SKILL.md").is_file())
        self.assertTrue((self.installed("bundle") / "skills/inner_dir/SKILL.md").is_file())
        names = [codex.skill_document(document)[0] for document in (self.root / "skills").rglob("SKILL.md")]
        self.assertCountEqual(names, ["example", "bundle", "inner-skill", "cube"])

    def test_nested_exclusions_hide_only_the_unsupported_entrypoint(self):
        plugin = self.add_plugin("bundle-plugin", exclude=["blocked"])
        self.add_skill(plugin, "bundle")
        blocked = self.add_skill(plugin, "bundle/skills/blocked")
        write(blocked / "references/retained.txt", "resource")
        self.install()
        nested = self.installed("bundle") / "skills/blocked"
        self.assertFalse((nested / "SKILL.md").exists())
        self.assertEqual((nested / "references/retained.txt").read_text(encoding="utf-8"), "resource")

    def test_codex_overlay_replaces_instructions_and_keeps_resources(self):
        write(self.plugin / "skills/example/references/canonical.txt", "canonical")
        write(self.plugin / "codex/example/SKILL.md",
              "---\nname: example\ndescription: Codex overlay\n---\nNative Codex instructions.\n")
        self.install()
        self.assertIn("Native Codex instructions", (self.installed() / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((self.installed() / "references/canonical.txt").is_file())

    def test_invalid_overlay_leaves_existing_installation_intact(self):
        self.install()
        before = (self.installed() / "SKILL.md").read_bytes()
        write(self.plugin / "codex/example/SKILL.md",
              "---\nname: renamed\ndescription: Invalid overlay\n---\nNew content.\n")
        with self.assertRaisesRegex(ValueError, "changed the skill name"):
            self.install()
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), before)

    def test_preserves_user_configuration_and_unrelated_skills(self):
        user_skill = self.installed("user-skill") / "SKILL.md"
        settings = self.project / ".codex/config.toml"
        guidance = self.project / "AGENTS.md"
        for path in (user_skill, settings, guidance):
            write(path, "user content")
        self.install()
        self.assertEqual(codex.uninstall(self.root), 1)
        for path in (user_skill, settings, guidance):
            self.assertEqual(path.read_text(encoding="utf-8"), "user content")
        self.assertFalse(self.installed().exists())
        self.assertEqual(codex.uninstall(self.root), 0)

    def test_rejects_unmanaged_collisions_before_changing_other_skills(self):
        user_file = self.installed() / "SKILL.md"
        write(user_file, "user skill")
        self.add_skill(self.plugin, "another")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(user_file.read_text(encoding="utf-8"), "user skill")
        self.assertFalse(self.installed("another").exists())
        self.assertFalse((self.root / codex.MANIFEST).exists())

    def test_updates_are_idempotent_and_remove_only_retired_managed_skills(self):
        self.add_skill(self.plugin, "retired")
        self.install()
        self.assertEqual(self.install(), 2)
        text = (self.installed() / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Codex platform integration"), 1)
        shutil.rmtree(self.plugin / "skills/retired")
        write(self.installed("user-skill") / "SKILL.md", "user content")
        self.assertEqual(self.install(), 1)
        self.assertFalse(self.installed("retired").exists())
        self.assertTrue(self.installed("user-skill").exists())

    def test_failed_publication_restores_previous_installation(self):
        self.install()
        before = (self.installed() / "SKILL.md").read_bytes()
        manifest = (self.root / codex.MANIFEST).read_bytes()
        self.add_skill(self.plugin, "example", body="new instructions")
        self.add_skill(self.plugin, "another")
        with mock.patch.object(codex.os, "replace", side_effect=OSError("fixture publication failure")):
            with self.assertRaisesRegex(OSError, "fixture publication failure"):
                self.install()
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), before)
        self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)
        self.assertFalse(self.installed("another").exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_rejects_duplicate_names_and_invalid_frontmatter_before_installation(self):
        self.add_skill(self.add_plugin("duplicate"), "different-dir", name="example")
        with self.assertRaisesRegex(ValueError, "Duplicate Codex skill"):
            self.install()
        self.assertFalse(self.root.exists())
        shutil.rmtree(self.repository / "Plugins/duplicate")
        write(self.plugin / "skills/example/SKILL.md", "No frontmatter")
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            self.install()
        self.assertFalse(self.root.exists())

    def test_manifest_cannot_remove_paths_outside_the_skills_directory(self):
        protected = self.project / "protected.txt"
        write(protected, "preserve")
        write_json(self.root / codex.MANIFEST, {
            "brand": "KPBot", "tool": "codex", "installed_skills": ["../../protected.txt"],
        })
        with self.assertRaisesRegex(ValueError, "Invalid skill name"):
            codex.uninstall(self.root)
        self.assertEqual(protected.read_text(encoding="utf-8"), "preserve")

    def test_invalid_manifest_is_rejected_with_an_actionable_error(self):
        write_json(self.root / codex.MANIFEST, [])
        with self.assertRaisesRegex(ValueError, "JSON object"):
            codex.uninstall(self.root)
        self.assertTrue((self.root / codex.MANIFEST).is_file())

    def test_malformed_plugin_settings_report_errors_without_changing_an_install(self):
        self.install()
        original = (self.installed() / "SKILL.md").read_bytes()
        manifest = (self.root / codex.MANIFEST).read_bytes()
        cases = (
            {"platforms": "codex"},
            {"platforms": ["codex", 7]},
            {"platforms": ["codex"], "codex": None},
            {"platforms": ["codex"], "codex": []},
            {"platforms": ["codex"], "codex": {"exclude": "example"}},
            {"platforms": ["codex"], "codex": {"exclude": [None]}},
            {"platforms": ["codex"], "codex": {"exclude": ["../example"]}},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata):
                write_json(self.plugin / ".claude-plugin/plugin.json", metadata)
                stderr = io.StringIO()
                with mock.patch.object(codex, "REPOSITORY", self.repository), contextlib.redirect_stderr(stderr):
                    self.assertEqual(codex.main(["install", "codex", str(self.project)]), 1)
                self.assertIn("KPBot Codex:", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
                self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)

    def test_invalid_package_version_is_rejected_before_creating_installation(self):
        for metadata in ({}, {"version": None}, {"version": []}, {"version": " "}):
            with self.subTest(metadata=metadata):
                write_json(self.repository / "package.json", metadata)
                with self.assertRaisesRegex(ValueError, "version"):
                    self.install()
                self.assertFalse(self.root.exists())

    def test_empty_catalog_cannot_remove_previous_skills(self):
        self.install()
        original = (self.installed() / "SKILL.md").read_bytes()
        write_json(self.plugin / ".claude-plugin/plugin.json", {"platforms": ["claude"]})
        with self.assertRaisesRegex(ValueError, "No plugins"):
            self.install()
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertTrue((self.root / codex.MANIFEST).is_file())

    def test_nested_duplicate_names_leave_previous_installation_intact(self):
        self.install()
        original = (self.installed() / "SKILL.md").read_bytes()
        self.add_skill(self.plugin, "example/nested", name="another")
        self.add_skill(self.plugin, "another")
        with self.assertRaisesRegex(ValueError, "Duplicate nested"):
            self.install()
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertFalse(self.installed("another").exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_missing_skill_entrypoint_is_rejected_before_creating_installation(self):
        write(self.plugin / "skills/incomplete/references/guide.md", "Incomplete package")
        with self.assertRaisesRegex(ValueError, "Missing SKILL.md"):
            self.install()
        self.assertFalse(self.root.exists())

    def test_documentation_only_domain_is_not_installed_as_a_skill(self):
        write(self.plugin / "skills/bigdata/DOMAIN.md", "Domain without members")
        self.assertEqual(self.install(), 1)
        self.assertFalse(self.installed("bigdata").exists())

    def test_domain_documentation_does_not_hide_missing_skill_entrypoints(self):
        write(self.plugin / "skills/incomplete/DOMAIN.md", "Domain description")
        write(self.plugin / "skills/incomplete/references/guide.md", "Remaining resources")
        with self.assertRaisesRegex(ValueError, "Missing SKILL.md"):
            self.install()
        self.assertFalse(self.root.exists())

    def test_lost_skill_entrypoint_cannot_silently_retire_an_installed_skill(self):
        self.add_skill(self.plugin, "another")
        self.install()
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        (self.plugin / "skills/example/SKILL.md").unlink()
        write(self.plugin / "skills/example/references/guide.md", "Remaining resources")
        with self.assertRaisesRegex(ValueError, "Missing SKILL.md"):
            self.install()
        after = {path.relative_to(self.root): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_excluded_incomplete_package_does_not_block_supported_skills(self):
        write(self.plugin / "skills/incomplete/references/guide.md", "Excluded resources")
        write_json(self.plugin / ".claude-plugin/plugin.json", {
            "platforms": ["codex"], "codex": {"exclude": ["incomplete"]},
        })
        self.assertEqual(self.install(), 1)
        self.assertTrue((self.installed() / "SKILL.md").is_file())
        self.assertFalse(self.installed("incomplete").exists())

    def test_group_with_all_child_skills_excluded_does_not_block_supported_skills(self):
        self.add_skill(self.plugin, "group/blocked")
        write_json(self.plugin / ".claude-plugin/plugin.json", {
            "platforms": ["codex"], "codex": {"exclude": ["blocked"]},
        })
        self.assertEqual(self.install(), 1)
        self.assertFalse(self.installed("blocked").exists())
        self.assertFalse(self.installed("group").exists())

    def test_failed_preparation_does_not_change_existing_installation(self):
        self.install()
        original = (self.installed() / "SKILL.md").read_bytes()
        manifest = (self.root / codex.MANIFEST).read_bytes()
        self.add_skill(self.plugin, "another")
        prepare = codex.prepare_skill

        def fail_after_copy(repository, destination, *arguments):
            prepare(repository, destination, *arguments)
            if destination.name == "example":
                raise OSError("fixture copy failure")

        with mock.patch.object(codex, "prepare_skill", side_effect=fail_after_copy):
            with self.assertRaisesRegex(OSError, "fixture copy failure"):
                self.install()
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)
        self.assertFalse(self.installed("another").exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_new_directory_during_preparation_is_preserved(self):
        self.install()
        self.add_skill(self.plugin, "another")
        original = (self.installed() / "SKILL.md").read_bytes()
        manifest = (self.root / codex.MANIFEST).read_bytes()
        prepare = codex.prepare_skill

        def create_conflict(*arguments):
            prepare(*arguments)
            write(self.installed("another") / "user.txt", "Created during preparation")

        with mock.patch.object(codex, "prepare_skill", side_effect=create_conflict):
            with self.assertRaisesRegex(ValueError, "conflict"):
                self.install()
        self.assertEqual((self.installed("another") / "user.txt").read_text(), "Created during preparation")
        self.assertFalse((self.installed("another") / "SKILL.md").exists())
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_changed_manifest_during_preparation_aborts_publication(self):
        self.install()
        manifest = self.root / codex.MANIFEST
        updated = json.loads(manifest.read_text())
        updated["version"] = "external-update"
        original = (self.installed() / "SKILL.md").read_bytes()
        prepare = codex.prepare_skill

        def change_manifest(*arguments):
            prepare(*arguments)
            write_json(manifest, updated)

        with mock.patch.object(codex, "prepare_skill", side_effect=change_manifest):
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                self.install()
        self.assertEqual(json.loads(manifest.read_text()), updated)
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_first_install_preserves_manifest_created_during_preparation(self):
        prepare = codex.prepare_skill
        manifest = self.root / codex.MANIFEST
        external = {"brand": "KPBot", "tool": "codex", "installed_skills": [], "version": "external"}

        def create_manifest(*arguments):
            prepare(*arguments)
            write_json(manifest, external)

        with mock.patch.object(codex, "prepare_skill", side_effect=create_manifest):
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                self.install()
        self.assertEqual(json.loads(manifest.read_text()), external)
        self.assertFalse(self.installed().exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_changed_ownership_during_preparation_blocks_retirement(self):
        self.add_skill(self.plugin, "retired")
        self.install()
        shutil.rmtree(self.plugin / "skills/retired")
        original = (self.installed() / "SKILL.md").read_bytes()
        retired = (self.installed("retired") / "SKILL.md").read_bytes()
        manifest = (self.root / codex.MANIFEST).read_bytes()
        prepare = codex.prepare_skill

        def change_owner(*arguments):
            prepare(*arguments)
            write_json(self.installed("retired") / codex.OWNER, {"brand": "User"})

        with mock.patch.object(codex, "prepare_skill", side_effect=change_owner):
            with self.assertRaisesRegex(ValueError, "not owned"):
                self.install()
        self.assertEqual(json.loads((self.installed("retired") / codex.OWNER).read_text()), {"brand": "User"})
        self.assertEqual((self.installed("retired") / "SKILL.md").read_bytes(), retired)
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_failed_staged_manifest_write_keeps_existing_installation(self):
        self.install()
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        write_text = Path.write_text

        def fail_manifest(target, *arguments, **keywords):
            if target.name == codex.MANIFEST and target.parent != self.root:
                raise OSError("fixture staged manifest failure")
            return write_text(target, *arguments, **keywords)

        with mock.patch.object(Path, "write_text", new=fail_manifest):
            with self.assertRaisesRegex(OSError, "fixture staged manifest failure"):
                self.install()
        after = {path.relative_to(self.root): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_uninstall_partial_removal_failure_can_be_retried(self):
        self.add_skill(self.plugin, "another")
        self.install()
        write(self.installed("user") / "notes.txt", "Keep")
        manifest = (self.root / codex.MANIFEST).read_bytes()
        remove = shutil.rmtree

        def fail_example(target, *arguments, **keywords):
            if target == self.installed():
                raise OSError("fixture removal failure")
            return remove(target, *arguments, **keywords)

        with mock.patch.object(codex.shutil, "rmtree", side_effect=fail_example):
            with self.assertRaisesRegex(OSError, "fixture removal failure"):
                codex.uninstall(self.root)
        self.assertFalse(self.installed("another").exists())
        self.assertTrue(self.installed().is_dir())
        self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)
        self.assertEqual(codex.uninstall(self.root), 2)
        self.assertEqual(codex.uninstall(self.root), 0)
        self.assertEqual((self.installed("user") / "notes.txt").read_text(), "Keep")

    def test_uninstall_manifest_removal_failure_can_be_retried(self):
        self.install()
        write(self.installed("user") / "notes.txt", "Keep")
        manifest = self.root / codex.MANIFEST
        unlink = Path.unlink

        def fail_manifest(target, *arguments, **keywords):
            if target == manifest:
                raise OSError("fixture unlink failure")
            return unlink(target, *arguments, **keywords)

        with mock.patch.object(Path, "unlink", new=fail_manifest):
            with self.assertRaisesRegex(OSError, "fixture unlink failure"):
                codex.uninstall(self.root)
        self.assertTrue(manifest.is_file())
        self.assertFalse(self.installed().exists())
        self.assertEqual(codex.uninstall(self.root), 1)
        self.assertEqual(codex.uninstall(self.root), 0)
        self.assertEqual((self.installed("user") / "notes.txt").read_text(), "Keep")

    def test_backup_and_partial_publish_failures_restore_retired_and_current_skills(self):
        self.add_skill(self.plugin, "retired")
        self.install()
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        shutil.rmtree(self.plugin / "skills/retired")
        self.add_skill(self.plugin, "another")
        self.add_skill(self.plugin, "example", body="updated")
        rename = Path.rename
        for failure_at in (2, 4):
            with self.subTest(failure_at=failure_at):
                calls = []

                def fail_once(source, destination):
                    calls.append(source)
                    if len(calls) == failure_at:
                        raise OSError("fixture rename failure")
                    return rename(source, destination)

                with mock.patch.object(Path, "rename", new=fail_once):
                    with self.assertRaisesRegex(OSError, "fixture rename failure"):
                        self.install()
                after = {path.relative_to(self.root): path.read_bytes()
                         for path in self.root.rglob("*") if path.is_file()}
                self.assertEqual(after, before)
                self.assertFalse(list(self.root.glob(".kpbot-codex-*")))

    def test_failed_first_publication_does_not_leave_owned_skills_or_manifest(self):
        with mock.patch.object(codex.os, "replace", side_effect=OSError("fixture manifest failure")):
            with self.assertRaisesRegex(OSError, "fixture manifest failure"):
                self.install()
        self.assertFalse(self.installed().exists())
        self.assertFalse((self.root / codex.MANIFEST).exists())
        self.assertFalse(list(self.root.glob(".kpbot-codex-*")))
        self.assertEqual(self.install(), 1)

    def test_failed_rollback_preserves_backups_and_restores_other_skills(self):
        self.add_skill(self.plugin, "another")
        self.install()
        original = (self.installed() / "SKILL.md").read_bytes()
        manifest = (self.root / codex.MANIFEST).read_bytes()
        self.add_skill(self.plugin, "example", body="new instructions")
        rename = Path.rename

        def fail_restore(source, destination):
            if source.parent.name == "old" and source.name == "another":
                raise OSError("fixture rollback failure")
            return rename(source, destination)

        with mock.patch.object(Path, "rename", new=fail_restore), \
                mock.patch.object(codex.os, "replace", side_effect=OSError("fixture manifest failure")):
            with self.assertRaisesRegex(OSError, "recovery files preserved"):
                self.install()
        self.assertEqual((self.installed() / "SKILL.md").read_bytes(), original)
        self.assertEqual((self.root / codex.MANIFEST).read_bytes(), manifest)
        backups = list(self.root.glob(".kpbot-codex-*/old/another/SKILL.md"))
        self.assertEqual(len(backups), 1)
        self.assertIn("Run the example.", backups[0].read_text(encoding="utf-8"))

    def test_linked_shared_script_is_rejected_before_publication(self):
        self.add_skill(self.plugin, "example", body="bash scripts/print_logo.sh")
        shared = self.repository / "Plugins/common/scripts/print_logo.sh"
        write(shared, "#!/bin/sh\necho fixture\n")
        is_link = codex.is_link
        with mock.patch.object(codex, "is_link", side_effect=lambda path: path == shared or is_link(path)):
            with self.assertRaisesRegex(ValueError, "regular files"):
                self.install()
        self.assertFalse(self.installed().exists())
        self.assertFalse((self.root / codex.MANIFEST).exists())

    def test_linked_skill_resources_are_rejected_before_publication(self):
        resource = self.plugin / "skills/example/references/data.txt"
        write(resource, "external fixture")
        is_link = codex.is_link
        with mock.patch.object(codex, "is_link", side_effect=lambda path: path == resource or is_link(path)):
            with self.assertRaisesRegex(ValueError, "regular files"):
                self.install()
        self.assertFalse(self.installed().exists())

    def test_npm_skill_install_and_update_skip_local_artifacts(self):
        source = self.plugin / "skills/example"
        write_json(source / "package-lock.json", {"lockfileVersion": 3})
        write(source / "test-output/SKILL.md", "Generated output")
        write(source / "references/test-output/example.txt", "Published example")
        tool = source / "node_modules/tool"
        write(tool / "SKILL.md", "Dependency instructions")
        write(tool / "cli.js", "fixture")
        with directory_link(tool, source / "node_modules/.bin/tool"):
            documents = [path for path in codex.skill_resources(source) if path.name == "SKILL.md"]
            self.assertEqual(documents, [source / "SKILL.md"])
            for version in ("first", "updated"):
                write(source / "scripts/diagram.mjs", version)
                self.assertEqual(self.install(), 1)
                self.assertEqual((self.installed() / "scripts/diagram.mjs").read_text(), version)
                self.assertTrue((self.installed() / "package-lock.json").is_file())
                self.assertTrue((self.installed() / "references/test-output/example.txt").is_file())
                self.assertFalse((self.installed() / "node_modules").exists())
                self.assertFalse((self.installed() / "test-output").exists())

            with directory_link(tool, source / "references/tool"):
                with self.assertRaisesRegex(ValueError, "regular files"):
                    self.install()
            self.assertEqual((self.installed() / "scripts/diagram.mjs").read_text(), "updated")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_installation_directories_are_rejected(self):
        outside = self.directory / "outside"
        write(outside / "protected.txt", "preserve")
        for relative in ("", "skills", "skills/example"):
            with self.subTest(relative=relative):
                junction = self.root / relative
                junction.parent.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, repr(result.stderr))
                try:
                    with self.assertRaisesRegex(ValueError, "without links"):
                        self.install()
                    if relative == "skills/example":
                        self.assertEqual(codex.uninstall(self.root), 0)
                    else:
                        with self.assertRaises(ValueError):
                            codex.uninstall(self.root)
                    self.assertEqual((outside / "protected.txt").read_text(), "preserve")
                finally:
                    junction.rmdir()

    def test_invalid_cli_arguments_never_create_installation(self):
        for arguments in ([], ["install"], ["install", "claude"],
                          ["install", "global", "codex", str(self.project), "extra"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    codex.main(arguments)
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(self.root.exists())

    def test_cli_defaults_to_project_and_respects_home_for_global_installation(self):
        with mock.patch.object(codex, "REPOSITORY", self.repository), \
                mock.patch.object(codex.Path, "cwd", return_value=self.project), \
                mock.patch.dict(os.environ, {"HOME": str(self.directory / "home")}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex.main(["install", "codex"]), 0)
            self.assertTrue((self.installed() / "SKILL.md").is_file())
            self.assertEqual(codex.main(["install", "global", "codex"]), 0)
            self.assertTrue((self.directory / "home/.agents/skills/example/SKILL.md").is_file())
            self.assertEqual(codex.main(["uninstall", "codex"]), 0)
            self.assertFalse(self.installed().exists())
            self.assertTrue((self.directory / "home/.agents/skills/example/SKILL.md").is_file())

    @unittest.skipUnless(os.environ.get("KPBOT_CODEX_TEST_EXECUTABLE"), "Set KPBOT_CODEX_TEST_EXECUTABLE for native Codex validation")
    def test_native_codex_discovers_all_repository_skills_without_loader_errors(self):
        codex.install(REPOSITORY, self.root, "project")
        expected = {codex.skill_document(document)[0]
                    for document in (self.root / "skills").rglob("SKILL.md")}
        executable = os.environ["KPBOT_CODEX_TEST_EXECUTABLE"]
        environment = dict(os.environ, CODEX_HOME=str(self.directory / "codex-home"),
                           HOME=str(self.directory / "home"), USERPROFILE=str(self.directory / "home"))
        (self.directory / "codex-home").mkdir()
        (self.directory / "home").mkdir()
        error_log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.addCleanup(error_log.close)
        process = subprocess.Popen([executable, "app-server"], cwd=self.project, env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log,
                                   text=True, encoding="utf-8")
        responses = queue.Queue()

        def receive():
            for line in process.stdout:
                responses.put(json.loads(line))
            responses.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def request(identifier, method, params):
            process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    response = responses.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    self.fail("Timed out waiting for native Codex " + method)
                if response is None:
                    error_log.seek(0)
                    self.fail("Native Codex exited before " + method + ": " + error_log.read())
                if response.get("id") == identifier:
                    self.assertNotIn("error", response, response)
                    return response["result"]
            self.fail("Timed out waiting for native Codex " + method)

        try:
            request(0, "initialize", {"clientInfo": {"name": "kpbot_install_test", "version": "1.0.0"},
                                      "capabilities": {"experimentalApi": True}})
            process.stdin.write('{"method":"initialized"}\n')
            process.stdin.flush()
            result = request(1, "skills/list", {"cwds": [str(self.project)], "forceReload": True})
            names = []
            for entry in result["data"]:
                self.assertEqual(entry.get("errors", []), [])
                names.extend(skill["name"] for skill in entry["skills"]
                             if self.root in Path(skill["path"]).parents)
            self.assertEqual(set(names), expected)
            self.assertEqual(len(names), len(set(names)))
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            reader.join(timeout=5)
            process.stdout.close()
            subprocess.run([executable, "app-server", "daemon", "stop"], env=environment,
                           capture_output=True, timeout=15)

    def test_uninstall_checks_all_ownership_markers_before_removing_any_skill(self):
        self.add_skill(self.plugin, "another")
        self.install()
        (self.installed() / codex.OWNER).unlink()
        with self.assertRaises(ValueError):
            codex.uninstall(self.root)
        self.assertTrue(self.installed("another").is_dir())
        self.assertTrue((self.root / codex.MANIFEST).is_file())

    def test_linked_skill_destinations_are_rejected(self):
        outside = self.directory / "outside"
        write(outside / "SKILL.md", "preserve")
        self.installed().parent.mkdir(parents=True)
        try:
            self.installed().symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Symlink creation is unavailable: " + str(exc))
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), "preserve")

    def test_installs_public_pr_maintainer_plugin(self):
        plugin = Path("Plugins/public/kpbot-pr-maintainer")
        shutil.copytree(REPOSITORY / plugin, self.repository / plugin)
        self.install()
        installed = self.installed("kpbot-pr-maintainer")
        self.assertEqual(codex.skill_document(installed / "SKILL.md")[0], "kpbot-pr-maintainer")
        self.assertTrue((installed / "agents/openai.yaml").is_file())
        self.assertTrue((installed / "references/gitcode-status.md").is_file())

    def test_repository_catalog_matches_measured_counts(self):
        expected = json.loads((REPOSITORY / "tests/fixtures/codex-skill-counts.json").read_text(encoding="utf-8"))
        count = codex.install(REPOSITORY, self.root, "project")
        manifest = json.loads((self.root / codex.MANIFEST).read_text(encoding="utf-8"))
        skills = self.root / "skills"
        names = [codex.skill_document(document)[0] for document in skills.rglob("SKILL.md")]
        source_count = sum(1
                           for metadata in codex.plugin_manifests(REPOSITORY / "Plugins")
                           if codex.plugin_config(metadata) is not None
                           for path in codex.skill_resources(metadata.parent.parent / "skills")
                           if path.name == "SKILL.md")
        measured = {
            "manifest_skills": len(manifest["installed_skills"]),
            "installed_skill_documents": len(names),
            "source_skill_documents_before_exclusions": source_count,
        }
        self.assertEqual(measured, {key: expected[key] for key in measured},
                         "Update the measured count fixture and docs/codex.md together when skill inventory changes")
        self.assertEqual(count, len(manifest["installed_skills"]))
        self.assertEqual(set(manifest["installed_skills"]), {path.name for path in skills.iterdir()})
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(codex.uninstall(self.root), count)

    def test_repository_catalog_installs_with_nested_resources(self):
        codex.install(REPOSITORY, self.root, "project")
        for name in ("kpbot-ai-host-insight", "kpbot-excalidraw-diagrams",
                     "kpbot-code-optimizer", "faiss-build", "kptools-openeuler-agentenv", "openwiki-sync"):
            self.assertTrue((self.installed(name) / "SKILL.md").is_file(), name)
        for name in ("drive-claude-optimize-pipeline", "multimedia-code-optimizer", "kunpeng-knowledge"):
            self.assertFalse(self.installed(name).exists(), name)
        self.assertTrue((self.installed("kpbot-code-optimizer") / "scripts/print_logo.sh").is_file())
        nested = self.installed("kptools-agentenv-tuning") / "skills/agentenv-cluster-tlbi/SKILL.md"
        self.assertTrue(nested.is_file())
        self.assertFalse(self.installed("agentenv-cluster-tlbi").exists())

    @unittest.skipUnless(BASH.is_file(), "Bash is required for CLI integration")
    def test_cli_install_and_uninstall_project_and_global_with_spaces(self):
        for relative in ("install.sh", "bin/kpbot", "bin/_uninstall.sh", "bin/_codex.sh", "adapters/codex/install.py"):
            target = self.repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY / relative, target)
            target.chmod(0o755)
        for level in ("project", "global"):
            target = self.directory / (level + " home with spaces")
            for command in ("install", "uninstall"):
                result = subprocess.run(
                    [str(BASH), shell_path(self.repository / "bin/kpbot"), command,
                     level, "codex", shell_path(target)],
                    env=dict(os.environ, PYTHONUTF8="1"), capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = target / ".agents/skills/example/SKILL.md"
                self.assertEqual(installed.is_file(), command == "install")

    @unittest.skipUnless(BASH.is_file(), "Bash is required for CLI integration")
    def test_shell_cli_handles_apostrophes_in_source_and_project_paths(self):
        package = self.directory / "package's \u4e2d\u6587"
        shutil.copytree(self.repository, package)
        for relative in ("install.sh", "bin/kpbot", "bin/_uninstall.sh", "bin/_codex.sh", "adapters/codex/install.py"):
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY / relative, target)
        project = self.directory / "project's \u4e2d\u6587 \U0001f680"
        for command in ("install", "uninstall"):
            result = subprocess.run(
                [str(BASH), shell_path(package / "bin/kpbot"), command, "codex", shell_path(project)],
                env=dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="gbk"),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((project / ".agents/skills/example/SKILL.md").is_file(), command == "install")

    @unittest.skipUnless(os.name == "nt", "MSYS drive conversion applies on Windows")
    def test_windows_config_root_accepts_msys_and_cygwin_drive_paths(self):
        project = self.directory / "project's \u4e2d\u6587"
        msys = shell_path(project)
        expected = project / ".agents"
        self.assertEqual(codex.config_root("project", msys), expected)
        self.assertEqual(codex.config_root("project", "/cygdrive" + msys), expected)
        with mock.patch.dict(os.environ, {"HOME": msys}):
            self.assertEqual(codex.config_root("global"), expected)

    def test_python_cli_handles_unicode_paths_with_a_legacy_locale(self):
        adapter = self.repository / "adapters/codex/install.py"
        adapter.parent.mkdir(parents=True)
        shutil.copy2(REPOSITORY / "adapters/codex/install.py", adapter)
        project = self.directory / "Codex \u4e2d\u6587 \U0001f680"
        for command in ("install", "uninstall"):
            result = subprocess.run(
                [sys.executable, str(adapter), command, "project", "codex", str(project)],
                env=dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="gbk"),
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(str(project), result.stdout)
            self.assertEqual((project / ".agents/skills/example/SKILL.md").is_file(), command == "install")

    @unittest.skipUnless(BASH.is_file(), "Bash is required for CLI integration")
    def test_legacy_relative_path_named_codex_does_not_dispatch_to_codex(self):
        self.add_plugin("optimizer", platforms=["claude", "codex"])
        for relative in ("install.sh", "bin/_uninstall.sh"):
            target = self.repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY / relative, target)
        write(self.repository / "CLAUDE.md", "Fixture instructions")
        for script in ("install.sh", "bin/_uninstall.sh"):
            result = subprocess.run(
                [str(BASH), shell_path(self.repository / script), "project", "claude", "codex"],
                cwd=self.directory, env=dict(os.environ, PYTHONUTF8="1"),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((self.directory / "codex/.claude/kpbot-manifest.json").is_file(),
                             script == "install.sh")
            if script == "install.sh":
                self.assertTrue((self.directory / "codex/.claude/skills/example/SKILL.md").is_file())
            else:
                self.assertIn("KPBot uninstalled", result.stdout)
            self.assertFalse((self.directory / "codex/.agents").exists())


if __name__ == "__main__":
    unittest.main()
