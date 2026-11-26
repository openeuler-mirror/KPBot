"""Check the comparison helper shipped with the Faiss skill bundle."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "Plugins/code-optimizer/skills/vector-experiment-methodology/scripts/compare_results.py"
SPEC = importlib.util.spec_from_file_location("faiss_result_comparison", SCRIPT)
COMPARISON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPARISON)


def fixture():
    return {"performance_field": "qps", "correctness_fields": ["recall"], "datasets": {
        "fixture": {"baseline": [{"qps": 10, "recall": 0.9}],
                    "candidate": [{"qps": 20, "recall": 0.9}]}}}


class FaissComparisonContractTests(unittest.TestCase):
    def test_throughput_and_latency_keep_the_declared_direction(self):
        result = COMPARISON.compare(fixture())
        self.assertEqual(result["geomean_speedup"], 2)
        data = fixture()
        data["higher_is_better"] = False
        self.assertEqual(COMPARISON.compare(data)["geomean_speedup"], 0.5)

    def test_boolean_or_nonfinite_performance_is_rejected(self):
        for value in (True, False, None, "20", float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                data = fixture()
                data["datasets"]["fixture"]["candidate"][0]["qps"] = value
                with self.assertRaises(ValueError):
                    COMPARISON.compare(data)

    def test_a_missing_sample_is_not_silently_omitted(self):
        data = fixture()
        data["datasets"]["fixture"]["candidate"].append({"recall": 0.9})
        with self.assertRaises(ValueError):
            COMPARISON.compare(data)

    def test_incomplete_correctness_is_reported_unavailable(self):
        data = fixture()
        data["datasets"]["fixture"]["candidate"].append({"qps": 20})
        result = COMPARISON.compare(data)
        self.assertEqual(result["datasets"]["fixture"]["correctness"]["recall"], {"available": False})

    def test_nonpositive_medians_are_rejected(self):
        for value in (0, -1):
            data = fixture()
            data["datasets"]["fixture"]["candidate"][0]["qps"] = value
            with self.assertRaises(ValueError):
                COMPARISON.compare(data)

    def test_overflow_cannot_be_reported_as_a_speedup(self):
        data = fixture()
        data["datasets"]["fixture"]["baseline"][0]["qps"] = 1e-300
        data["datasets"]["fixture"]["candidate"][0]["qps"] = 1e300
        with self.assertRaises(ValueError):
            COMPARISON.compare(data)

    def test_cli_rejects_invalid_data_without_a_report_or_traceback(self):
        invalid = fixture()
        invalid["datasets"]["fixture"]["candidate"][0]["qps"] = float("nan")
        overflow = fixture()
        overflow["datasets"]["fixture"]["baseline"][0]["qps"] = 1
        overflow["datasets"]["fixture"]["candidate"][0]["qps"] = 1e308
        for data in ([], invalid, overflow):
            with self.subTest(data=data), tempfile.TemporaryDirectory(prefix="faiss-compare-") as folder:
                source = Path(folder) / "input.json"
                output = Path(folder) / "output.json"
                source.write_text(json.dumps(data), encoding="utf-8")
                result = subprocess.run([sys.executable, str(SCRIPT), str(source), "--output", str(output)],
                                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
