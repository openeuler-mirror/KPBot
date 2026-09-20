"""Tests for the diagnostic experiment record contract."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import experiment_record
from experiment_record import validate_record, write_record


SCRIPT = Path(experiment_record.__file__).resolve()


def sample(root: Path, *, experiment_id: str = "e1") -> dict:
    """Return a logic fixture; its evidence is not target performance proof."""
    (root / "raw.txt").write_text("fixture: path observation\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "run_id": "r1",
        "point_id": "p1",
        "experiment_id": experiment_id,
        "hypothesis_id": "h1-v1",
        "parent_state": "source-digest-fixture",
        "execution_context": {
            "target": "fixture",
            "build": "fixture",
            "workload": "fixture",
        },
        "hypothesis": {
            "claim": "运行时选择了变体 A",
            "alternative": "运行时选择了变体 B",
            "support_if": "直接执行记录包含 A",
            "refute_if": "直接执行记录包含 B",
        },
        "experiment": {
            "command": ["fixture-only"],
            "controlled": ["相同输入与构建"],
            "changed": "仅观察实际分派",
            "timeout_seconds": 600,
        },
        "evidence": [
            {
                "path": "raw.txt",
                "scope": "request",
                "kind": "observation",
                "source": "fixture",
            }
        ],
    }


class RecordTests(unittest.TestCase):
    def test_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = sample(root)
            self.assertTrue(write_record(record, root).is_file())
            with self.assertRaisesRegex(ValueError, "experiment already exists"):
                write_record(record, root)

    def test_missing_or_outside_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for path in ("missing.txt", "../outside.txt", "/etc/hosts"):
                with self.subTest(path=path):
                    record = sample(root)
                    record["evidence"][0]["path"] = path
                    with self.assertRaisesRegex(ValueError, "invalid evidence path"):
                        validate_record(record, root)

    def test_symlink_evidence_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d)
            external_file = Path(outside) / "external.txt"
            external_file.write_text("not in run directory\n", encoding="utf-8")
            (root / "escape.txt").symlink_to(external_file)
            record = sample(root)
            record["evidence"][0]["path"] = "escape.txt"
            with self.assertRaisesRegex(ValueError, "invalid evidence path"):
                validate_record(record, root)

    def test_symlink_output_escape_is_rejected_before_mutation(self):
        for link_path in ("experiments", "experiments/e1"):
            for existing_target in (True, False):
                with self.subTest(link=link_path, existing_target=existing_target), \
                        tempfile.TemporaryDirectory() as d, \
                        tempfile.TemporaryDirectory() as outside:
                    root = Path(d)
                    record = sample(root)
                    destination = Path(outside) / "output"
                    if existing_target:
                        destination.mkdir()
                    link = root / link_path
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(destination, target_is_directory=True)
                    before = sorted(Path(outside).rglob("*"))
                    with self.assertRaisesRegex(ValueError, "invalid experiment output path"):
                        write_record(record, root)
                    self.assertEqual(sorted(Path(outside).rglob("*")), before)
                    self.assertTrue(link.is_symlink())

    def test_hypothesis_requires_refutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = sample(root)
            del record["hypothesis"]["refute_if"]
            with self.assertRaisesRegex(ValueError, "hypothesis.refute_if"):
                validate_record(record, root)

    def test_design_requires_controlled_build_and_workload(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cases = (
                (("experiment", "controlled"), "experiment.controlled"),
                (("execution_context", "build"), "execution_context.build"),
                (("execution_context", "workload"), "execution_context.workload"),
            )
            for keys, expected in cases:
                with self.subTest(field=".".join(keys)):
                    record = sample(root)
                    del record[keys[0]][keys[1]]
                    with self.assertRaisesRegex(ValueError, expected.replace(".", r"\.")):
                        validate_record(record, root)

    def test_unknown_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for value in (2, 1.0, "1", True):
                with self.subTest(value=value):
                    record = sample(root)
                    record["schema_version"] = value
                    with self.assertRaisesRegex(ValueError, "schema_version"):
                        validate_record(record, root)

    def test_ids_use_only_portable_characters(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for field in ("run_id", "point_id", "experiment_id", "hypothesis_id"):
                with self.subTest(field=field):
                    record = sample(root)
                    record[field] = "bad/id"
                    with self.assertRaisesRegex(ValueError, field):
                        validate_record(record, root)

    def test_required_text_and_conditions_cannot_be_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cases = (
                (("parent_state",), ""),
                (("execution_context", "target"), "   "),
                (("execution_context", "build"), ""),
                (("execution_context", "workload"), ""),
                (("hypothesis", "claim"), ""),
                (("experiment", "changed"), ""),
                (("experiment", "command"), []),
                (("experiment", "controlled"), []),
            )
            for keys, value in cases:
                with self.subTest(field=".".join(keys)):
                    record = sample(root)
                    target = record
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    with self.assertRaisesRegex(ValueError, "\\.".join(keys)):
                        validate_record(record, root)

    def test_timeout_must_be_a_positive_integer(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for value in (0, -1, 1.5, True, "600"):
                with self.subTest(value=value):
                    record = sample(root)
                    record["experiment"]["timeout_seconds"] = value
                    with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                        validate_record(record, root)

    def test_evidence_enums_and_fields_are_validated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cases = (
                ("kind", "measurement"),
                ("source", "unknown"),
                ("scope", ""),
            )
            for field, value in cases:
                with self.subTest(field=field):
                    record = sample(root)
                    record["evidence"][0][field] = value
                    with self.assertRaisesRegex(ValueError, f"evidence\\[0\\]\\.{field}"):
                        validate_record(record, root)

    def test_empty_evidence_design_is_valid_but_not_persistable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = sample(root)
            record["evidence"] = []
            validate_record(record, root)
            with self.assertRaisesRegex(ValueError, "final experiment record requires evidence"):
                write_record(record, root)
            self.assertFalse((root / "experiments").exists())

    def test_post_creation_failure_retains_and_reports_incomplete_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = sample(root)
            record["non_json_fixture"] = object()
            folder = root / "experiments" / "e1"

            with self.assertRaises(ValueError) as raised:
                write_record(record, root)

            self.assertIn("incomplete experiment record retained at", str(raised.exception))
            self.assertIn(str(folder), str(raised.exception))
            self.assertTrue(folder.is_dir())
            self.assertFalse((folder / "record.json").exists())

    def test_different_experiments_can_share_a_parent_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first = sample(root, experiment_id="e1")
            second = sample(root, experiment_id="e2")
            self.assertTrue(write_record(first, root).is_file())
            self.assertTrue(write_record(second, root).is_file())

    def test_cli_writes_record_and_prints_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            input_path = root / "input.json"
            record = sample(root)
            input_path.write_text(json.dumps(record), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--record", str(input_path), "--run-dir", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )

            expected = root / "experiments" / "e1" / "record.json"
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), str(expected))
            self.assertEqual(json.loads(expected.read_text(encoding="utf-8")), record)

    def test_cli_reports_invalid_json_without_running_command(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            input_path = root / "input.json"
            input_path.write_text("not json", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--record", str(input_path), "--run-dir", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(completed.stderr.strip())
            self.assertFalse((root / "experiments").exists())


if __name__ == "__main__":
    unittest.main()
