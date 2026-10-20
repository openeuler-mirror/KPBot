"""Private local backend configuration tests; no services or external SDKs.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.memory.test_backend_config -v
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from kpbot_memory.backend_config import (  # noqa: E402
    BackendConfigError, MAX_CONFIG_BYTES, clone_json, load_config, validate_config,
)
from kpbot_memory import backend_config  # noqa: E402


def config() -> dict:
    return {
        "backend": "openviking", "user_id": "local-user", "current_project": "project-a",
        "allowed_projects": ["project-a", "project-b"],
        "options": {"data_dir": "/explicit/local/data", "model": {"name": "用户模型"}},
    }


class ConfigValidationTests(unittest.TestCase):
    def test_preserves_values_and_makes_deep_copy(self):
        original = config()
        result = validate_config(original)
        self.assertEqual(result, original)
        result["options"]["model"]["name"] = "changed"
        result["allowed_projects"].append("project-c")
        self.assertEqual(original, config())

    def test_all_five_fields_required_and_unknown_rejected(self):
        for field in config():
            candidate = config()
            del candidate[field]
            with self.subTest(missing=field), self.assertRaises(BackendConfigError):
                validate_config(candidate)
        for field in ("tokens", "owner_id", "roles", "project_path"):
            candidate = config()
            candidate[field] = "untrusted"
            with self.subTest(unknown=field), self.assertRaises(BackendConfigError):
                validate_config(candidate)

    def test_invalid_configuration_types(self):
        cases = [None, [], "config"]
        for field, values in {
            "user_id": [None, "", "  ", 1, []],
            "current_project": [None, "", " ", 1, []],
            "allowed_projects": [None, [], "project-a", [""], [1], ["project-a", "project-a"]],
            "options": [None, [], "options"],
        }.items():
            for value in values:
                candidate = config()
                candidate[field] = value
                cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(BackendConfigError):
                validate_config(candidate)

    def test_current_project_requires_explicit_scope(self):
        candidate = config()
        candidate["current_project"] = "other-project"
        with self.assertRaises(BackendConfigError) as caught:
            validate_config(candidate)
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")

    def test_backend_selection_is_explicit(self):
        for name in ("", "OPENVIKING", "OpenViking", None):
            candidate = config()
            candidate["backend"] = name
            with self.subTest(name=name), self.assertRaises(BackendConfigError) as caught:
                validate_config(candidate)
            self.assertEqual(caught.exception.code, "UNSUPPORTED")

    def test_non_json_and_nonfinite_options_rejected_without_echo(self):
        for value in (float("nan"), float("inf"), object(), {1: "secret"}, (1, 2), "\ud800"):
            candidate = config()
            candidate["options"]["credential"] = value
            with self.subTest(value=type(value)), self.assertRaises(BackendConfigError) as caught:
                validate_config(candidate)
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNone(caught.exception.source_id)

    def test_json_copy_preserves_numeric_types_and_order(self):
        value = {"z": [1, 1.0, True, None, "\n完整正文"], "a": {"x": " "}}
        result = clone_json(value)
        self.assertEqual(list(result), ["z", "a"])
        self.assertIs(type(result["z"][0]), int)
        self.assertIs(type(result["z"][1]), float)
        self.assertEqual(result, value)
        self.assertIsNot(result["z"], value["z"])

    def test_cycles_and_excessively_deep_objects_rejected(self):
        cycle = []
        cycle.append(cycle)
        deep = []
        current = deep
        for _ in range(2000):
            following = []
            current.append(following)
            current = following
        for value in (cycle, deep):
            with self.assertRaises(BackendConfigError):
                clone_json(value)


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "backend.json"

    def write(self, raw=None, mode=0o600):
        if raw is None:
            raw = json.dumps(config(), ensure_ascii=False).encode("utf-8")
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if self.path.exists():
            self.path.chmod(0o600)  # Reset only this test-owned fixture, not loader behaviour.
        self.path.write_bytes(raw)
        self.path.chmod(mode)
        return self.path

    def test_loads_explicit_path_and_does_not_modify_file(self):
        path = self.write()
        before = path.read_bytes()
        self.assertEqual(load_config(path), config())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_or_invalid_path(self):
        for value in (self.path, None, "\0", 123):
            with self.subTest(value=value), self.assertRaises(BackendConfigError):
                load_config(value)

    def test_group_other_and_special_modes_rejected_not_repaired(self):
        for mode in (0o400, 0o640, 0o644, 0o660, 0o700, 0o4600):
            self.write(mode=mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(BackendConfigError) as caught:
                load_config(self.path)
            self.assertEqual(caught.exception.code, "ACCESS_DENIED")
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)

    def test_owner_must_be_effective_os_user(self):
        self.write()
        with mock.patch.object(backend_config.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(BackendConfigError) as caught:
                load_config(self.path)
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")

    def test_directory_symlink_and_fifo_are_not_configuration(self):
        self.write()
        linked = Path(self.temp.name) / "link.json"
        linked.symlink_to(self.path)
        fifo = Path(self.temp.name) / "config.fifo"
        os.mkfifo(fifo, 0o600)
        for candidate in (Path(self.temp.name), linked, fifo):
            with self.subTest(candidate=candidate), self.assertRaises(BackendConfigError):
                load_config(candidate)

    def test_opened_file_identity_mode_and_owner_are_rechecked(self):
        self.write()
        info = self.path.stat()
        base = {"st_mode": info.st_mode, "st_uid": info.st_uid, "st_dev": info.st_dev, "st_ino": info.st_ino}
        for change in ({"st_ino": info.st_ino + 1}, {"st_mode": stat.S_IFREG | 0o644}, {"st_uid": info.st_uid + 1}):
            altered = SimpleNamespace(**(base | change))
            with self.subTest(change=change), mock.patch.object(backend_config.os, "fstat", return_value=altered):
                with self.assertRaises(BackendConfigError) as caught:
                    load_config(self.path)
            self.assertEqual(caught.exception.code, "ACCESS_DENIED")

    def test_strict_json_file_values(self):
        raw_cases = [
            b"\xff", "[]", "null", "{}", "{", "{} {}",
            '{"credential":"TOP-SECRET","credential":"TOP-SECRET"}',
            '{"options":{"number":NaN}}', '{"options":{"number":Infinity}}',
            json.dumps({**config(), "user_id": "\ud800"}),
            json.dumps({**config(), "options": {"credential": "TOP-SECRET", "number": float("inf")}}),
        ]
        for raw in raw_cases:
            self.write(raw)
            with self.subTest(raw=type(raw)), self.assertRaises(BackendConfigError) as caught:
                load_config(self.path)
            self.assertNotIn("TOP-SECRET", str(caught.exception))

    def test_file_limit_is_explicit_and_never_truncated(self):
        self.write(b" " * (MAX_CONFIG_BYTES + 1))
        with self.assertRaises(BackendConfigError) as caught:
            load_config(self.path)
        self.assertEqual(caught.exception.code, "TOO_LARGE")
        self.assertIn(str(MAX_CONFIG_BYTES), caught.exception.message)

    def test_token_file_is_not_backend_identity(self):
        self.write(json.dumps({"tokens": {"TOP-SECRET": {"user": "someone"}}}))
        with self.assertRaises(BackendConfigError) as caught:
            load_config(self.path)
        self.assertNotIn("TOP-SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
