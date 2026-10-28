"""Keep malformed role output from aborting app-tuner result aggregation."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "Plugins/app-tuner/skills/kpbot-app-tuner/scripts/merge_subagent_results.py"
SPEC = importlib.util.spec_from_file_location("app_tuner_result_merger", SCRIPT)
MERGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MERGER)


def result_fixture():
    return {
        "subskill_name": "accelerator-optimization", "current_run_id": "fixture-run",
        "current_evidence_status": "current", "status": "ok",
        "analysis_timestamp": "2026-01-01T00:00:00Z", "evidence_sources": ["test.log"],
        "findings": [], "candidate_actions": [], "required_evidence": [],
        "confidence": "high", "fallback_notes": [], "timing": [],
        "subagent_id": "fixture-role", "result_path": "result.json",
    }


class AppTunerResultContractTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory(prefix="kpbot-app-result-") as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return MERGER.load_result(path)

    def test_valid_result_is_preserved(self):
        document = result_fixture()
        result, errors = self.load(document)
        self.assertEqual(errors, [])
        self.assertEqual(result, document)

    def test_non_object_results_are_reported_without_exceptions(self):
        for value in ([], None, 1, "result"):
            with self.subTest(value=value):
                result, errors = self.load(value)
                self.assertIsNone(result)
                self.assertTrue(errors)

    def test_non_list_actions_are_reported_without_exceptions(self):
        for value in (None, 1, "action", {}):
            with self.subTest(value=value):
                document = result_fixture()
                document["candidate_actions"] = value
                _, errors = self.load(document)
                self.assertTrue(any("candidate_actions_not_list" in error for error in errors))

    def test_one_second_timing_is_not_confused_with_a_boolean(self):
        for value, expected in ((1, 1.0), (1.0, 1.0), ("1", 1.0), (0.5, 0.5), (True, 0.0), (False, 0.0)):
            with self.subTest(value=value):
                result = {"timing": [{"skill_name": "accelerator-optimization", "total_seconds": value,
                                      "notes": "msprof"}]}
                timing = MERGER.merge_npu_timing([result])
                self.assertEqual(timing["npu_profiler_seconds"], expected)

    def test_unhashable_result_keys_are_rejected(self):
        for field in ("status", "subskill_name"):
            with self.subTest(field=field):
                document = result_fixture()
                document[field] = []
                result, errors = self.load(document)
                self.assertIsNone(result)
                self.assertTrue(errors)
        document = result_fixture()
        document["candidate_actions"] = [{"action_id": []}]
        result, errors = self.load(document)
        self.assertIsNone(result)
        self.assertTrue(any("invalid_action_id" in error for error in errors))

    def test_malformed_manifests_produce_a_blocked_report(self):
        for manifest in ([], {"candidate_skill_list": None}, {"candidate_skill_list": [None]}):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory(prefix="kpbot-app-manifest-") as directory:
                root = Path(directory)
                results = root / "results"
                results.mkdir()
                (results / "result.json").write_text(json.dumps(result_fixture()), encoding="utf-8")
                path = root / "manifest.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                pool = root / "pool.json"
                run = subprocess.run(
                    [sys.executable, str(SCRIPT), "--results-dir", str(results), "--candidate-manifest", str(path),
                     "--output-candidate-pool", str(pool), "--output-summary", str(root / "summary.md"), "--gate-check"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                )
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertNotIn("Traceback", run.stdout + run.stderr)
                self.assertIn("invalid_candidate_manifest", pool.read_text(encoding="utf-8"))

    def test_utf8_json_round_trips_with_a_legacy_system_locale(self):
        with tempfile.TemporaryDirectory(prefix="kpbot-app-utf8-") as directory:
            root = Path(directory)
            results = root / "results"
            results.mkdir()
            (results / "result.json").write_text(json.dumps(result_fixture()), encoding="utf-8")
            manifest = {"current_run_id": "fixture-run", "current_evidence_status": "current",
                        "bottleneck_classification": "内存", "candidate_skill_list": []}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            pool = root / "pool.json"
            run = subprocess.run(
                [sys.executable, str(SCRIPT), "--results-dir", str(results), "--candidate-manifest", str(path),
                 "--output-candidate-pool", str(pool), "--output-summary", str(root / "summary.md")],
                env=dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="utf-8"),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            text = pool.read_text(encoding="utf-8")
            self.assertNotIn("invalid_candidate_manifest", text)
            self.assertIn("内存", text)
            self.assertIsInstance(json.loads(text), dict)


if __name__ == "__main__":
    unittest.main()
