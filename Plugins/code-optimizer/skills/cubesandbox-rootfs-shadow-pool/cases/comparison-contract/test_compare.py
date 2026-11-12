"""Behavioral checks for paired create-time analysis; all data is synthetic."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare_create_time.py"
SPEC = importlib.util.spec_from_file_location("compare_create_time", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {
            "schema_version": 1, "metric": "summary.total_time_ms", "minimum_reduction_pct": 2,
            "workload": {"concurrency": 100, "total": 100, "mode": "create-only", "template": "tpl-test"},
            "environment_id": "host-boot-template-test",
            "variants": {name: {"artifact_id": name + "-sha", "correctness_passed": True}
                         for name in ("base", "candidate")}, "runs": [],
        }
        for block in range(12):
            for position, variant in enumerate(("base", "candidate", "candidate", "base")):
                names = []
                for rep in range(5):
                    name = f"b{block}-p{position}-r{rep}.json"
                    names.append(name)
                    # Drift shared by each block, exactly 20% time reduction.
                    value = (1000 + block * 10) * (0.8 if variant == "candidate" else 1)
                    self.write(name, {"config": copy.deepcopy(self.manifest["workload"]),
                                      "summary": {"successful": 100, "errors": 0, "total_time_ms": value}})
                self.manifest["runs"].append({"block": block, "position": position,
                    "variant": variant, "environment_id": self.manifest["environment_id"], "reports": names})

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def analyze(self):
        self.write("comparison.json", self.manifest)
        return MODULE.compare(self.root / "comparison.json")

    def edit_report(self, mutate):
        name = self.manifest["runs"][0]["reports"][0]
        data = json.loads((self.root / name).read_text(encoding="utf-8"))
        mutate(data)
        self.write(name, data)

    def test_time_reduction_not_throughput(self):
        result = self.analyze()
        self.assertEqual(result["verdict"], "improved")
        self.assertAlmostEqual(result["reduction_pct"], 20)
        self.assertAlmostEqual(result["bootstrap_95ci_pct"][0], 20)
        self.assertEqual(len(result["reports"]), 240)
        self.assertLess(result["sign_test_two_sided_p"], 0.05)

    def test_seconds_normalization(self):
        self.manifest["metric"] = "summary.total_time_s"
        for path in self.root.glob("b*.json"):
            report = json.loads(path.read_text())
            report["summary"]["total_time_s"] = report["summary"].pop("total_time_ms") / 1000
            report["config"]["template_id"] = report["config"].pop("template")
            self.write(path.name, report)
        self.assertAlmostEqual(self.analyze()["reduction_pct"], 20)

    def test_ties_and_regression(self):
        for factor, expected in ((1.0, "inconclusive"), (1.1, "regressed")):
            for run in self.manifest["runs"]:
                if run["variant"] == "candidate":
                    for name in run["reports"]:
                        report = json.loads((self.root / name).read_text())
                        report["summary"]["total_time_ms"] = (1000 + run["block"] * 10) * factor
                        self.write(name, report)
            self.assertEqual(self.analyze()["verdict"], expected)

    def test_threshold_is_not_target_matching(self):
        self.manifest["minimum_reduction_pct"] = 25
        self.assertEqual(self.analyze()["verdict"], "inconclusive")

    def test_invalid_evidence(self):
        cases = [
            lambda d: d["summary"].update(errors=1),
            lambda d: d["summary"].update(successful=99),
            lambda d: d["summary"].update(total_time_ms=float("nan")),
            lambda d: d["summary"].update(total_time_ms=0),
            lambda d: d["summary"].update(total_time_ms=True),
            lambda d: d["summary"].update(total_time_s=42),
            lambda d: d["config"].update(template="other"),
            lambda d: d["config"].update(concurrency=50),
            lambda d: d.update(raw=[{"error": "failed"}]),
            lambda d: d["summary"].pop("total_time_ms"),
        ]
        name = self.manifest["runs"][0]["reports"][0]
        original = json.loads((self.root / name).read_text())
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.write(name, original)
                self.edit_report(mutate)
                with self.assertRaises((ValueError, KeyError)):
                    self.analyze()

    def test_manifest_integrity(self):
        original = copy.deepcopy(self.manifest)
        cases = [
            lambda m: m["runs"][1].update(environment_id="rebooted"),
            lambda m: m["runs"][1].update(variant="base"),
            lambda m: m["runs"][0].update(position=1),
            lambda m: m["runs"][0]["reports"].pop(),
            lambda m: m["runs"][1]["reports"].__setitem__(0, m["runs"][0]["reports"][0]),
            lambda m: m["variants"]["candidate"].update(correctness_passed=False),
            lambda m: m.update(runs=m["runs"][:4]),
            lambda m: m.update(minimum_reduction_pct=-1),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.manifest = copy.deepcopy(original)
                mutate(self.manifest)
                with self.assertRaises(ValueError):
                    self.analyze()

    def test_cli_and_preserve_existing_output(self):
        self.write("comparison.json", self.manifest)
        output = self.root / "verdict.json"
        command = [sys.executable, str(SCRIPT), "--manifest", str(self.root / "comparison.json"),
                   "--output", str(output)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = output.read_bytes()
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        missing = subprocess.run([sys.executable, str(SCRIPT), "--manifest", str(self.root / "missing")],
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)

    def test_collection_plan_does_not_fabricate_results(self):
        output = self.root / "plan.json"
        command = [sys.executable, str(SCRIPT.with_name("plan_comparison.py")),
                   "--output", str(output), "--template", "tpl-test", "--environment", "test-host",
                   "--base-artifact", "base-sha", "--candidate-artifact", "candidate-sha",
                   "--metric", "summary.total_time_s"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(output.read_text())
        self.assertEqual(len(plan["runs"]), 48)
        self.assertEqual(len({p for r in plan["runs"] for p in r["reports"]}), 240)
        self.assertFalse(plan["variants"]["candidate"]["correctness_passed"])
        self.assertFalse((self.root / "reports").exists())
        with self.assertRaises(ValueError):
            MODULE.compare(output)
        before = output.read_bytes()
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
