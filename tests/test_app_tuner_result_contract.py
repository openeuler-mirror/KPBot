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


def action_fixture():
    return {
        "action_id": "fixture-action", "title": "Reuse an existing buffer",
        "category": "memory", "priority": "medium", "change_mode": "code",
        "requires_root": False, "risk": "low",
        "implementation_plan": ["Reuse the buffer between requests"],
        "validation_plan": ["Run correctness and allocation checks"],
        "rollback": "Restore per-request allocation",
        "expected_effect": "Fewer allocations", "expected_gain_metric": "allocations/request",
        "rejection_criteria": ["Correctness regression"], "evidence_refs": ["test.log"],
    }


class AppTunerResultContractTests(unittest.TestCase):
    def run_cli(self, root, documents, manifest):
        results = root / "results"
        results.mkdir()
        for index, document in enumerate(documents):
            (results / f"result-{index}.json").write_text(json.dumps(document), encoding="utf-8")
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        pool = root / "pool.json"
        summary = root / "summary.md"
        run = subprocess.run(
            [sys.executable, str(SCRIPT), "--results-dir", str(results), "--candidate-manifest", str(path),
             "--output-candidate-pool", str(pool), "--output-summary", str(summary), "--gate-check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return run, pool, summary

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
            for value in ([], {}, None, 1, "", " "):
                with self.subTest(field=field, value=value):
                    document = result_fixture()
                    document[field] = value
                    result, errors = self.load(document)
                    self.assertIsNone(result)
                    self.assertTrue(errors)
        for value in ([], {}, None, 1, "", " "):
            with self.subTest(action_id=value):
                document = result_fixture()
                document["candidate_actions"] = [dict(action_fixture(), action_id=value)]
                result, errors = self.load(document)
                self.assertIsNone(result)
                self.assertTrue(any("invalid_action_id" in error for error in errors))

    def test_malformed_manifests_produce_a_blocked_report(self):
        for manifest in ([], None, {"candidate_skill_list": None}, {"candidate_skill_list": [None]},
                         {"dynamic_route_plan": [None]}):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory(prefix="kpbot-app-manifest-") as directory:
                root = Path(directory)
                run, pool, summary = self.run_cli(root, [result_fixture()], manifest)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertNotIn("Traceback", run.stdout + run.stderr)
                document = json.loads(pool.read_text(encoding="utf-8"))
                self.assertTrue(any("invalid_candidate_manifest" in error for error in document["gate_errors"]))
                self.assertEqual(document["candidate_skill_list"], [])
                self.assertIn("invalid_candidate_manifest", summary.read_text(encoding="utf-8"))

    def test_malformed_results_keep_healthy_results_and_write_blocked_reports(self):
        cases = [([], "invalid_result_root")]
        for field, token in (("status", "invalid_status"), ("subskill_name", "invalid_subskill_name")):
            for value in ([], {}):
                cases.append((dict(result_fixture(), **{field: value}), token))
        for value in ([], {}, ""):
            cases.append((dict(result_fixture(), candidate_actions=[dict(action_fixture(), action_id=value)]),
                          "invalid_action_id"))
        cases.append((dict(result_fixture(), candidate_actions=None), "candidate_actions_not_list"))
        healthy = dict(result_fixture(), candidate_actions=[action_fixture()])
        manifest = {"current_run_id": "fixture-run", "current_evidence_status": "current",
                    "candidate_skill_list": []}
        for broken, token in cases:
            with self.subTest(error=token, document=broken), tempfile.TemporaryDirectory(prefix="kpbot-app-cli-") as directory:
                run, pool, summary = self.run_cli(Path(directory), [broken, healthy], manifest)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertNotIn("Traceback", run.stdout + run.stderr)
                document = json.loads(pool.read_text(encoding="utf-8"))
                self.assertIn("validation_errors_present", document["gate_errors"])
                self.assertTrue(any(token in error for error in document["validation_errors"]))
                self.assertEqual(document["summary"]["subagent_result_count"], 1)
                self.assertEqual([action["action_id"] for action in document["candidate_actions"]], ["fixture-action"])
                self.assertIn(token, summary.read_text(encoding="utf-8"))

    def test_valid_cli_input_passes_the_gate(self):
        document = dict(result_fixture(), candidate_actions=[action_fixture()])
        manifest = {"current_run_id": "fixture-run", "current_evidence_status": "current",
                    "candidate_skill_list": [{"subskill_name": "accelerator-optimization"}]}
        with tempfile.TemporaryDirectory(prefix="kpbot-app-valid-") as directory:
            run, pool, summary = self.run_cli(Path(directory), [document], manifest)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(pool.read_text(encoding="utf-8"))
            self.assertEqual(report["validation_errors"], [])
            self.assertEqual(report["gate_errors"], [])
            self.assertEqual(report["summary"]["candidate_action_count"], 1)
            self.assertIn("fixture-action", summary.read_text(encoding="utf-8"))

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
