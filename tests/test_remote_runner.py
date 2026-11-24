#!/usr/bin/env python3
"""Unit tests for the built-in remote target mode CLI (no SSH required)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "Plugins"
    / "code-optimizer"
    / "skills"
    / "remote-runner"
    / "scripts"
    / "remote_runner.py"
)

spec = importlib.util.spec_from_file_location("kpbot_remote", MODULE_PATH)
assert spec and spec.loader
kpbot_remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kpbot_remote)


class WorkspaceTests(unittest.TestCase):
    def test_accepts_dedicated_absolute_path(self) -> None:
        self.assertEqual(
            kpbot_remote.normalize_workspace("/ssd/KBot-workspace/"),
            "/ssd/KBot-workspace",
        )
        self.assertEqual(
            kpbot_remote.normalize_workspace("/home/user/kpbot-workspace"),
            "/home/user/kpbot-workspace",
        )
        self.assertEqual(
            kpbot_remote.normalize_workspace("/ssd/工作区+KBot@1"),
            "/ssd/工作区+KBot@1",
        )

    def test_unmangles_git_bash_path_conversion(self) -> None:
        self.assertEqual(
            kpbot_remote.normalize_workspace("C:/Program Files/Git/ssd/KBot-workspace"),
            "/ssd/KBot-workspace",
        )
        self.assertEqual(
            kpbot_remote.normalize_workspace("C:/msys64/ssd/KBot-workspace"),
            "/ssd/KBot-workspace",
        )

    def test_rejects_unsafe_paths(self) -> None:
        for value in (
            "",
            "/",
            "/etc",
            "/tmp",
            "/tmp/work",
            "/usr/local/work",
            "/root/work",
            "relative/path",
            "C:/workspace",
            "/ssd/work space",
            "/ssd/ws;id",
            "/ssd/ws$id",
            "/ssd/ws`id`",
            "/ssd/ws'id'",
            '/ssd/ws"id"',
            "/ssd/ws%id",
            "/ssd/ws:id",
            "/ssd",
        ):
            with self.subTest(value=value):
                with self.assertRaises(kpbot_remote.UserError):
                    kpbot_remote.normalize_workspace(value)

class ConfigTests(unittest.TestCase):
    def test_valid_config(self) -> None:
        cfg = kpbot_remote.validate_config(
            {"host": "10.0.0.1", "user": "runner", "port": 2222, "workspace": "/ssd/ws"}
        )
        self.assertEqual(cfg["port"], 2222)
        self.assertEqual(cfg["workspace"], "/ssd/ws")

    def test_rejects_bad_host_user_port(self) -> None:
        base = {"host": "host", "user": "runner", "port": 22, "workspace": "/ssd/ws"}
        cases = (
            {**base, "host": "-oProxyCommand=evil"},
            {**base, "host": "host name"},
            {**base, "user": "-root"},
            {**base, "port": 0},
            {**base, "port": 70000},
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(kpbot_remote.UserError):
                    kpbot_remote.validate_config(case)

class RemotePathTests(unittest.TestCase):
    def test_remote_layout(self) -> None:
        cfg = {"host": "host", "user": "runner", "port": 22, "workspace": "/ssd/ws"}
        project = Path(tempfile.gettempdir()) / "kpbot-remote-path-test"
        pid = kpbot_remote.project_id(project)
        self.assertRegex(pid, r"^[0-9a-f]{8}$")
        root = f"/ssd/ws/.kpbot/projects/{pid}"
        self.assertEqual(kpbot_remote.remote_root(cfg, project), root)
        self.assertEqual(kpbot_remote.remote_src(cfg, project), root + "/src")
        self.assertEqual(kpbot_remote.remote_build(cfg, project), root + "/build")

class SourceScanTests(unittest.TestCase):
    def test_excludes_git_runs_and_build_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            files = (
                "src/main.c",
                ".git/config",
                ".kpbot/runs/r1/meta.json",
                ".kpbot/hosts/host-1.json",
                ".kpbot/remote/build.sh",
                ".kpbot/remote/config.json",
                "build/app",
                "dist/app",
                "node_modules/pkg/index.js",
                "docs/readme.md",
            )
            for rel in files:
                path = project / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            found = {rel for _, rel in kpbot_remote.iter_source_files(project)}
            self.assertIn("src/main.c", found)
            self.assertIn(".kpbot/remote/build.sh", found)
            self.assertIn("docs/readme.md", found)
            self.assertNotIn(".git/config", found)
            self.assertNotIn(".kpbot/runs/r1/meta.json", found)
            self.assertNotIn(".kpbot/hosts/host-1.json", found)
            self.assertNotIn(".kpbot/remote/config.json", found)
            self.assertNotIn("build/app", found)
            self.assertNotIn("dist/app", found)
            self.assertNotIn("node_modules/pkg/index.js", found)

    def test_fingerprint_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "main.c"
            source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
            first = kpbot_remote.source_fingerprint(project)
            second = kpbot_remote.source_fingerprint(project)
            self.assertEqual(first, second)
            source.write_text("int main(void) { return 42; }\n", encoding="utf-8")
            self.assertNotEqual(first, kpbot_remote.source_fingerprint(project))

class GitignoreTests(unittest.TestCase):
    def test_adds_kpbot_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            gitignore = project / ".gitignore"
            gitignore.write_text("node_modules/\n", encoding="utf-8")
            with mock.patch.object(kpbot_remote, "git_worktree", return_value=True):
                with mock.patch.object(kpbot_remote, "tracked_kpbot_files", return_value=[]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        kpbot_remote.ensure_kpbot_gitignore(project)
                        kpbot_remote.ensure_kpbot_gitignore(project)
            text = gitignore.read_text(encoding="utf-8")
            self.assertIn("node_modules/", text)
            self.assertEqual(text.count(".kpbot/"), 1)

class SymlinkTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "symlink creation requires privileges on Windows")
    def test_detects_escape_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            outside = Path(tmp) / "outside.txt"
            project.mkdir()
            outside.write_text("x", encoding="utf-8")
            (project / "leak").symlink_to(outside)
            escapes = kpbot_remote.find_symlink_escapes(project)
            self.assertEqual(len(escapes), 1)
            self.assertEqual(escapes[0][0], "leak")

class CommandTests(unittest.TestCase):
    def _project(self, tmp: str) -> Path:
        project = Path(tmp)
        config = project / ".kpbot" / "remote" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {"host": "host", "user": "runner", "port": 2222, "workspace": "/ssd/ws"}
            ),
            encoding="utf-8",
        )
        return project

    def test_config_writes_and_rejects_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                project=tmp,
                host="host",
                user="runner",
                port=2222,
                workspace="/ssd/ws",
                force=False,
                create_workspace=False,
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = kpbot_remote.cmd_config(args)
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertTrue(data["ok"])
            self.assertEqual(data["config"]["workspace"], "/ssd/ws")
            config = Path(tmp) / ".kpbot" / "remote" / "config.json"
            self.assertTrue(config.is_file())
            with self.assertRaises(kpbot_remote.UserError) as ctx:
                kpbot_remote.cmd_config(args)
            self.assertEqual(ctx.exception.code, 2)

    def test_config_rejects_invalid_workspace_as_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                project=tmp,
                host="host",
                user="runner",
                port=2222,
                workspace="/etc/kpbot",
                force=False,
                create_workspace=False,
            )
            with self.assertRaises(kpbot_remote.UserError) as ctx:
                kpbot_remote.cmd_config(args)
            self.assertEqual(ctx.exception.code, 64)

    def test_config_create_workspace_rejects_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                project=tmp,
                host="host",
                user="runner",
                port=2222,
                workspace="/home/runner",
                force=False,
                create_workspace=True,
            )

            def fake_ssh(cfg, command, timeout=None):
                return subprocess.CompletedProcess([], 0, "/home/runner\n", "")

            with mock.patch.object(kpbot_remote, "run_ssh", side_effect=fake_ssh):
                with self.assertRaises(kpbot_remote.UserError) as ctx:
                    kpbot_remote.cmd_config(args)
            self.assertEqual(ctx.exception.code, 64)
            self.assertIn("home", str(ctx.exception))
            self.assertFalse(
                (Path(tmp) / ".kpbot" / "remote" / "config.json").exists()
            )

    def test_config_create_workspace_only_chmods_new_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            args = argparse.Namespace(
                project=tmp,
                host="host",
                user="runner",
                port=2222,
                workspace="/ssd/ws",
                force=False,
                create_workspace=True,
            )

            def fake_ssh(cfg, command, timeout=None):
                calls.append(command)
                if "$HOME" in command:
                    return subprocess.CompletedProcess([], 0, "/home/runner\n", "")
                return subprocess.CompletedProcess([], 0, "", "")

            with mock.patch.object(kpbot_remote, "run_ssh", side_effect=fake_ssh):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = kpbot_remote.cmd_config(args)

            self.assertEqual(code, 0)
            create_cmd = calls[-1]
            self.assertIn("if [ -d", create_cmd)
            self.assertIn("test -w", create_cmd)
            self.assertIn("chmod 700", create_cmd)

    def test_config_invalidates_sync_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            args = argparse.Namespace(
                project=tmp,
                host="host",
                user="runner",
                port=2222,
                workspace="/ssd/ws1",
                force=False,
                create_workspace=False,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                kpbot_remote.cmd_config(args)
            cfg = {"host": "host", "user": "runner", "port": 2222, "workspace": "/ssd/ws1"}
            fingerprint, count = kpbot_remote.source_fingerprint(project)
            kpbot_remote.write_last_sync(
                project, cfg, "test", count, fingerprint, False, 0
            )
            marker = project / kpbot_remote.LAST_SYNC_REL
            self.assertTrue(marker.is_file())

            args.workspace = "/ssd/ws2"
            args.force = True
            with contextlib.redirect_stdout(io.StringIO()):
                kpbot_remote.cmd_config(args)
            self.assertFalse(marker.exists())

    def test_load_config_error_mentions_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(kpbot_remote.UserError) as ctx:
                kpbot_remote.load_config(Path(tmp))
            self.assertEqual(ctx.exception.code, 5)
            self.assertIn("config", str(ctx.exception))
            self.assertNotIn("init", str(ctx.exception))

    def test_cli_errors_return_json_with_usage_code(self) -> None:
        cases = (
            ["config"],
            [
                "config",
                "--host",
                "host",
                "--user",
                "runner",
                "--workspace",
                "/ssd/ws",
                "--port",
                "bad",
            ],
            ["unknown-command"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        code = kpbot_remote.main(argv)
                self.assertEqual(code, 64)
                data = json.loads(out.getvalue())
                self.assertFalse(data["ok"])
                self.assertEqual(data["code"], 64)
                self.assertNotIn("usage:", err.getvalue())

    def test_help_and_version_are_not_json_errors(self) -> None:
        for argv in (["--help"], ["--version"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    with self.assertRaises(SystemExit) as ctx:
                        kpbot_remote.main(argv)
                self.assertEqual(ctx.exception.code, 0)
                self.assertNotIn('"ok": false', out.getvalue())

    def test_probe_returns_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp)
            info = chr(10).join(
                [
                    "arch=aarch64",
                    "hostname=node-01",
                    "home=/home/runner",
                    "bash=/bin/bash",
                    "rsync=missing",
                    "tar=/bin/tar",
                    "realpath=/bin/realpath",
                    "timeout=/bin/timeout",
                    "ws_exists=1",
                    "ws_writable=1",
                    "df=Filesystem 1024-blocks Used Available Capacity Mounted",
                ]
            )
            calls = []

            def fake_ssh(cfg, command, timeout=None):
                calls.append(command)
                if command == "true":
                    return subprocess.CompletedProcess([], 0, "", "")
                return subprocess.CompletedProcess([], 0, info, "")

            with mock.patch.object(kpbot_remote, "run_ssh", side_effect=fake_ssh):
                with mock.patch.object(
                    kpbot_remote, "known_host_fingerprints", return_value=["SHA256:abc"]
                ):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        code = kpbot_remote.cmd_probe(
                            argparse.Namespace(project=tmp)
                        )
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["arch"], "aarch64")
            self.assertFalse(data["rsync"])
            self.assertTrue(data["workspace"]["exists"])
            self.assertEqual(data["fingerprints"], ["SHA256:abc"])
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
