"""Explicit configure wizard tests: no services, network, SDK or runtime changes."""
from __future__ import annotations

import getpass
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import warnings

SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from kpbot_memory.cli import configure as configure_cli
from kpbot_memory.backend_config import load_config


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "private client.json"
        self.secret = "TOP-SECRET-test-only-key"

    def answers(self, backend="mem0", *, endpoint="", projects="", confirmation="yes"):
        answers = [endpoint, "explicit-owner", "project-a", projects, confirmation]
        if backend == "openviking":
            answers += ["account-a", "provider-user"]
        elif backend == "tencentdb":
            answers += ["instance-a", ""]
        return "\n".join(answers) + "\n"

    def invoke(self, backend="mem0", *, answers=None, secret=None, extra=(), tty=True, path=None):
        stdout = io.StringIO()
        stderr = Terminal() if tty else io.StringIO()
        stdin = Terminal(self.answers(backend) if answers is None else answers) if tty else io.StringIO("")
        value = self.secret if secret is None else secret
        key = mock.Mock(side_effect=value) if isinstance(value, BaseException) else mock.Mock(return_value=value)
        with mock.patch.object(configure_cli.sys, "stdin", stdin), \
             mock.patch.object(configure_cli.sys, "stdout", stdout), \
             mock.patch.object(configure_cli.sys, "stderr", stderr), \
             mock.patch.object(configure_cli.getpass, "getpass", key), \
             mock.patch.dict(sys.modules, {"httpx": None}):
            code = configure_cli.main(["--backend", backend, "--config", str(path or self.path), *extra])
        output = stdout.getvalue()
        self.assertEqual(len(output.splitlines()), 1, output)
        result = json.loads(output)
        self.assertEqual(code, 0 if result["ok"] else 1)
        self.assertNotIn(self.secret, output + stderr.getvalue())
        return result, stderr.getvalue(), key

    def test_all_backends_create_private_loadable_config_without_connection(self):
        for backend in ("mem0", "openviking", "tencentdb"):
            with self.subTest(backend=backend):
                path = self.root / (backend + ".json")
                result, progress, key = self.invoke(backend, path=path)
                self.assertEqual(result, {"ok": True, "data": {
                    "config_path": str(path), "connection_verified": False}})
                config = load_config(path)
                self.assertEqual(config["backend"], backend)
                self.assertEqual(config["user_id"], "explicit-owner")
                self.assertEqual(config["allowed_projects"], ["project-a"])
                self.assertEqual(config["options"]["api_key"], self.secret)
                self.assertNotIn("owner_id", config["options"])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertIn("backend status --config", progress)
                self.assertIn("Connection has not been verified", progress)
                key.assert_called_once()
                self.assertIn("stream", key.call_args.kwargs)
                if backend == "mem0":
                    self.assertIs(config["options"]["derive_memories"], False)
                elif backend == "openviking":
                    self.assertEqual(config["options"]["processing_mode"], "vectors_only")
                    self.assertEqual(config["options"]["user_id"], "provider-user")
                else:
                    self.assertEqual(config["options"]["service_id"], "instance-a")
                    self.assertEqual(config["options"]["agent_id"], "kpbot")

    def test_parent_creation_is_private_and_existing_parent_mode_unchanged(self):
        os.chmod(self.root, 0o755)
        path = self.root / "new" / "nested" / "config.json"
        result, _, _ = self.invoke(path=path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        for parent in (path.parent, path.parent.parent):
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)

    def test_existing_file_and_symlink_are_never_overwritten(self):
        original = self.root / "existing.json"
        original.write_text("keep existing contents")
        linked = self.root / "linked.json"
        linked.symlink_to(original)
        dangling = self.root / "dangling.json"
        dangling.symlink_to(self.root / "absent")
        for path in (original, linked, dangling):
            with self.subTest(path=path.name):
                result, _, key = self.invoke(path=path)
                self.assertEqual(result["error"]["code"], "ACCESS_DENIED")
                key.assert_not_called()
        self.assertEqual(original.read_text(), "keep existing contents")
        self.assertTrue(linked.is_symlink() and dangling.is_symlink())
        self.assertFalse((self.root / "absent").exists())

    def test_target_created_during_prompt_is_preserved(self):
        def collect(_backend):
            self.path.write_text("racing existing file")
            return {"secret": self.secret}
        with mock.patch.object(configure_cli, "_collect", side_effect=collect):
            result, _, _ = self.invoke()
        self.assertEqual(result["error"]["code"], "ACCESS_DENIED")
        self.assertEqual(self.path.read_text(), "racing existing file")

    def test_non_terminal_does_not_prompt_for_secret(self):
        result, _, key = self.invoke(tty=False)
        self.assertFalse(result["ok"])
        key.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_raw_secret_command_argument_is_rejected_without_echo(self):
        result, progress, key = self.invoke(extra=("--api-key", self.secret))
        self.assertFalse(result["ok"])
        self.assertNotIn(self.secret, progress)
        key.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_scope_requires_confirmation_and_contains_current_project(self):
        for projects, confirmation in (("", "no"), ("", ""), ("other-project", "yes"),
                                       ("project-a,project-a", "yes")):
            with self.subTest(projects=projects, confirmation=confirmation):
                result, _, _ = self.invoke(answers=self.answers(projects=projects, confirmation=confirmation))
                self.assertFalse(result["ok"])
                self.assertFalse(self.path.exists())

    def test_explicit_multi_project_scope_is_preserved(self):
        result, _, _ = self.invoke(answers=self.answers(projects="project-a, project-b"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(load_config(self.path)["allowed_projects"], ["project-a", "project-b"])

    def test_adapter_normalizer_checks_endpoint_without_echoing_input(self):
        endpoint = "http://" + self.secret + "@remote.example"
        normalize = configure_cli.Mem0Backend._normalize_options
        with mock.patch.object(configure_cli.Mem0Backend, "_normalize_options", wraps=normalize) as checked:
            result, progress, _ = self.invoke(answers=self.answers(endpoint=endpoint))
        checked.assert_called_once()
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertNotIn(endpoint, progress)
        self.assertFalse(self.path.exists())

    def test_openviking_reuses_loopback_only_validation(self):
        result, _, _ = self.invoke("openviking", answers=self.answers("openviking", endpoint="https://remote.example"))
        self.assertFalse(result["ok"])
        self.assertFalse(self.path.exists())

    def test_empty_or_control_character_key_is_rejected(self):
        for secret in ("", "   ", "invalid\nkey"):
            with self.subTest(secret=repr(secret)):
                result, progress, _ = self.invoke(secret=secret)
                self.assertFalse(result["ok"])
                self.assertNotIn("invalid\nkey", progress)
                self.assertFalse(self.path.exists())

    def test_eof_and_cancel_during_secret_entry_leave_no_file(self):
        for cancelled in (EOFError(), KeyboardInterrupt()):
            with self.subTest(cancelled=type(cancelled).__name__):
                result, _, _ = self.invoke(secret=cancelled)
                self.assertFalse(result["ok"])
                self.assertFalse(self.path.exists())
        result, _, key = self.invoke(answers="")
        self.assertFalse(result["ok"])
        key.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_getpass_fallback_warning_cannot_echo_secret(self):
        def fallback(*args, **kwargs):
            warnings.warn("terminal cannot hide input", getpass.GetPassWarning)
            raise AssertionError("must not continue to echoing fallback")
        stdout, stderr = io.StringIO(), Terminal()
        with mock.patch.object(configure_cli.sys, "stdin", Terminal(self.answers())), \
             mock.patch.object(configure_cli.sys, "stdout", stdout), \
             mock.patch.object(configure_cli.sys, "stderr", stderr), \
             mock.patch.object(configure_cli.getpass, "getpass", side_effect=fallback):
            self.assertEqual(configure_cli.main(["--backend", "mem0", "--config", str(self.path)]), 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "ACCESS_DENIED")
        self.assertFalse(self.path.exists())

    def test_write_failure_does_not_delete_a_replacement_target(self):
        def replace_before_failure(_fd):
            replacement = self.root / "replacement"
            replacement.write_text("keep replacement")
            os.replace(replacement, self.path)
            raise OSError("write failed")
        with mock.patch.object(configure_cli.os, "fsync", side_effect=replace_before_failure):
            result, _, _ = self.invoke()
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_text(), "keep replacement")

    def test_write_failure_or_interrupt_removes_only_new_partial_file(self):
        for failure in (OSError("TOP-SECRET-test-only-key"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(configure_cli.os, "fsync", side_effect=failure):
                    result, _, _ = self.invoke()
                self.assertFalse(result["ok"])
                self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
