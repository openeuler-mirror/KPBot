"""Legacy shell installation contracts across native Python encodings."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else Path(shutil.which("bash") or "/bin/bash")


def shell_path(path):
    path = Path(path).resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:] if os.name == "nt" else str(path)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@unittest.skipUnless(BASH.is_file(), "Bash is required for shell installation contracts")
class LegacyInstallTests(unittest.TestCase):
    def setUp(self):
        temp_root = REPOSITORY / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="legacy-cli-", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repository = self.directory / "package's \u4e2d\u6587"
        self.project = self.directory / "project's \u4e2d\u6587 \U0001f680"
        for relative in ("install.sh", "bin/_uninstall.sh"):
            target = self.repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY / relative, target)
        write(self.repository / "package.json", '{"version":"test"}')
        write(self.repository / "CLAUDE.md", "Fixture instructions")
        self.plugin = self.repository / "Plugins/fixture"
        metadata = {"description": "\u4e2d\u6587\u63d2\u4ef6", "platforms": ["claude", "opencode"],
                    "opencode": {"exclude": ["drive-only", "batch-only"]}}
        write(self.plugin / ".claude-plugin/plugin.json", json.dumps(metadata, ensure_ascii=False))
        for name in ("example", "drive-only", "batch-only"):
            self.add_skill(self.plugin, name)
        unsupported = self.repository / "Plugins/unsupported"
        write(unsupported / ".claude-plugin/plugin.json", json.dumps({
            "description": "\u4e2d\u6587\u4e0d\u652f\u6301\u7684\u63d2\u4ef6", "platforms": ["other"],
        }, ensure_ascii=False))
        self.add_skill(unsupported, "unsupported")

    def add_skill(self, plugin, name):
        write(plugin / "skills" / name / "SKILL.md",
              "---\nname: " + name + "\ndescription: Fixture skill\n---\nFixture body\n")

    def run_cli(self, command, platform="opencode", fallback=False, missing=False):
        script = self.repository / ("install.sh" if command == "install" else "bin/_uninstall.sh")
        arguments = [str(BASH), shell_path(script), "project", platform, shell_path(self.project)]
        if missing:
            arguments = [str(BASH), "-c", 'python3() { return 1; }; python() { return 1; }; '
                         'export -f python3 python; exec "$BASH" "$@"', "kpbot-test", *arguments[1:]]
        elif fallback:
            arguments = [str(BASH), "-c", 'python3() { return 1; }; python() { "$KPBOT_TEST_PYTHON" "$@"; }; '
                         'export -f python3 python; exec "$BASH" "$@"', "kpbot-test", *arguments[1:]]
        environment = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="gbk",
                           KPBOT_TEST_PYTHON=shell_path(sys.executable))
        return subprocess.run(arguments, cwd=self.directory, env=environment, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_opencode_filters_utf8_metadata_and_retires_managed_skills(self):
        self.add_skill(self.plugin, "retired")
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        for name in ("drive-only", "batch-only", "unsupported"):
            self.assertFalse((config / "skills" / name).exists(), name)
        self.assertTrue((config / "skills/example/SKILL.md").is_file())
        self.assertTrue((config / "skills/retired/SKILL.md").is_file())
        retired_source = self.plugin / "skills/retired"
        (retired_source / "SKILL.md").unlink()
        retired_source.rmdir()
        write(config / "skills/user/SKILL.md", "User skill")
        self.assert_success(self.run_cli("install"))
        self.assertFalse((config / "skills/retired").exists())
        state = json.loads((config / "kpbot-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(state["version"], "test")
        self.assertEqual(state["installed_skills"], ["example"])
        self.assert_success(self.run_cli("uninstall"))
        self.assertFalse((config / "skills/example").exists())
        self.assertFalse((config / "kpbot-manifest.json").exists())
        self.assertEqual((config / "skills/user/SKILL.md").read_text(), "User skill")
        self.assertTrue((self.project / "AGENTS.md").is_file())

    def test_claude_retains_platform_specific_skills_and_uninstalls_all_managed_names(self):
        self.assert_success(self.run_cli("install", "claude"))
        config = self.project / ".claude"
        for name in ("example", "drive-only", "batch-only"):
            self.assertTrue((config / "skills" / name / "SKILL.md").is_file(), name)
        self.assertFalse((config / "skills/unsupported").exists())
        self.assert_success(self.run_cli("uninstall", "claude"))
        for name in ("example", "drive-only", "batch-only"):
            self.assertFalse((config / "skills" / name).exists(), name)

    def test_install_and_uninstall_fall_back_when_python3_is_unavailable(self):
        self.assert_success(self.run_cli("install", fallback=True))
        skill = self.project / ".opencode/skills/example"
        self.assertTrue(skill.is_dir())
        self.assert_success(self.run_cli("uninstall", fallback=True))
        self.assertFalse(skill.exists())

    def test_invalid_manifests_block_install_and_uninstall_before_any_deletion(self):
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        manifest = config / "kpbot-manifest.json"
        protected = self.project / "protected.txt"
        write(protected, "Preserve")
        for names in (["example", "../../protected.txt"], "example", [None]):
            for command in ("install", "uninstall"):
                with self.subTest(names=names, command=command):
                    write(manifest, json.dumps({"brand": "KPBot", "tool": "opencode", "installed_skills": names}))
                    result = self.run_cli(command)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Invalid installed_skills", result.stderr)
                    self.assertTrue((config / "skills/example/SKILL.md").is_file())
                    self.assertTrue(manifest.is_file())
                    self.assertEqual(protected.read_text(), "Preserve")

    def test_legacy_user_directory_names_have_actionable_errors_without_mutation(self):
        for platform in ("claude", "opencode"):
            self.assert_success(self.run_cli("install", platform))
            config = self.project / (".claude" if platform == "claude" else ".opencode")
            manifest = config / "kpbot-manifest.json"
            for name in ("user skill", "我的 skill"):
                write(config / "skills" / name / "SKILL.md", "Preserve user skill")
                write(manifest, json.dumps({"brand": "KPBot", "tool": platform,
                                           "installed_skills": ["example", name]}, ensure_ascii=False))
                before = {path.relative_to(config): path.read_bytes()
                          for path in config.rglob("*") if path.is_file()}
                for command in ("install", "uninstall"):
                    with self.subTest(platform=platform, name=name, command=command):
                        result = self.run_cli(command, platform)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn("Traceback", result.stderr)
                        self.assertIn(repr(name), result.stderr)
                        self.assertIn(str(manifest).replace("\\", "/"), result.stderr.replace("\\", "/"))
                        self.assertIn("Back up", result.stderr)
                        self.assertIn("remove only the manifest file", result.stderr)
                        after = {path.relative_to(config): path.read_bytes()
                                 for path in config.rglob("*") if path.is_file()}
                        self.assertEqual(after, before)

    def test_malformed_json_manifest_has_actionable_error_without_traceback(self):
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        manifest = config / "kpbot-manifest.json"
        write(manifest, "{broken JSON")
        for command in ("install", "uninstall"):
            result = self.run_cli(command)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("Restore a valid manifest", result.stderr)
            self.assertEqual(manifest.read_text(), "{broken JSON")
            self.assertTrue((config / "skills/example/SKILL.md").is_file())

    def test_foreign_tool_manifests_are_rejected_before_any_deletion(self):
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        manifest = config / "kpbot-manifest.json"
        state = json.loads(manifest.read_text(encoding="utf-8"))
        for field, value in (("tool", "claude"), ("brand", "Other")):
            for command in ("install", "uninstall"):
                with self.subTest(field=field, command=command):
                    foreign = dict(state, **{field: value})
                    write(manifest, json.dumps(foreign))
                    result = self.run_cli(command)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Invalid KPBot installation manifest", result.stderr)
                    self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), foreign)
                    self.assertTrue((config / "skills/example/SKILL.md").is_file())

    def test_invalid_manifest_shapes_and_duplicate_names_are_rejected(self):
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        manifest = config / "kpbot-manifest.json"
        base = {"brand": "KPBot", "tool": "opencode"}
        payloads = (None, [], {}, base, dict(base, installed_skills=None),
                    dict(base, installed_skills=["example", "example"]))
        for payload in payloads:
            for command in ("install", "uninstall"):
                with self.subTest(payload=payload, command=command):
                    write(manifest, json.dumps(payload))
                    before = manifest.read_bytes()
                    result = self.run_cli(command)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Invalid", result.stderr)
                    self.assertEqual(manifest.read_bytes(), before)
                    self.assertTrue((config / "skills/example/SKILL.md").is_file())

    def test_unavailable_python_preserves_installed_files_and_manifest(self):
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        before = {path.relative_to(config): path.read_bytes()
                  for path in config.rglob("*") if path.is_file()}
        for command in ("install", "uninstall"):
            with self.subTest(command=command):
                result = self.run_cli(command, missing=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("python", (result.stdout + result.stderr).lower())
                after = {path.relative_to(config): path.read_bytes()
                         for path in config.rglob("*") if path.is_file()}
                self.assertEqual(after, before)

    def test_repeated_uninstall_preserves_project_configuration_and_user_skills(self):
        self.assert_success(self.run_cli("install"))
        config = self.project / ".opencode"
        write(config / "skills/user/SKILL.md", "User content")
        guidance = (self.project / "AGENTS.md").read_bytes()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assert_success(self.run_cli("uninstall"))
                self.assertFalse((config / "skills/example").exists())
                self.assertEqual((config / "skills/user/SKILL.md").read_text(), "User content")
                self.assertEqual((self.project / "AGENTS.md").read_bytes(), guidance)


if __name__ == "__main__":
    unittest.main()
