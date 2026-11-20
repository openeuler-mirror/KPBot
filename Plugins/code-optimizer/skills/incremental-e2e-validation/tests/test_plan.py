import importlib.util
from pathlib import Path
import unittest
import json
import subprocess
import sys
import tempfile

spec = importlib.util.spec_from_file_location("planner", Path(__file__).resolve().parents[1] / "scripts" / "plan.py")
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class ContractTests(unittest.TestCase):
    def test_arbitrary_counts(self):
        for n in (1, 2, 3, 5, 30):
            result = planner.plan({"optimizations": [{"id": str(i)} for i in range(n)]})
            self.assertEqual(len(result["configurations"]), 2 * n)
            self.assertEqual(len(result["comparisons"]), 3 * n - 2)
            self.assertEqual(len(result["rows"]), n)
            self.assertEqual(result["all_combined"], result["rows"][-1]["cumulative"])
            configs = {c["id"]: set(c["active"]) for c in result["configurations"]}
            for row in result["rows"]:
                pair = result["comparisons"][row["incremental"]]
                self.assertEqual(configs[pair["candidate"]] - configs[pair["control"]], {row["optimization"]})

    def test_dependencies(self):
        result = planner.plan({"optimizations": [{"id": "A"}, {"id": "B", "requires": ["A"]}, {"id": "C", "excludes": ["B"]}]})
        configs = {tuple(c["active"]): c for c in result["configurations"]}
        self.assertTrue(configs[("B",)]["reasons"])
        self.assertFalse(configs[("A", "B")]["reasons"])
        self.assertTrue(configs[("A", "B", "C")]["reasons"])

    def test_bad_ids(self):
        for items in ([], [{"id": "A"}, {"id": "A"}], [{"id": "A", "requires": ["X"]}]):
            with self.assertRaises(ValueError):
                planner.plan({"optimizations": items})

    def test_metrics(self):
        for direction, x, y, expected in (("lower", 100, 90, 10), ("higher", 100, 110, 10), ("lower", 88, 89, -100 / 88)):
            result = planner.summarize({"direction": direction, "blocks": [{"control": x, "candidate": y}]})
            self.assertAlmostEqual(result["gain_pct"], expected)
            self.assertEqual(result["verdict"], "requires_evidence_review")

    def test_invalid_metrics(self):
        for value in (0, -1, True, float("nan"), float("inf"), "100"):
            with self.assertRaises(ValueError):
                planner.summarize({"direction": "lower", "blocks": [{"control": value, "candidate": 1}]})

    def test_mixed_direction_samples(self):
        result = planner.summarize({"direction": "lower", "blocks": [{"control": 100, "candidate": 99}, {"control": 100, "candidate": 101}]})
        self.assertEqual(result["range_pct"], [-1, 1])
        self.assertEqual(result["gain_pct"], 0)

    def test_cli(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "plan.py"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            for data, code in (({"optimizations": [{"id": "A"}]}, 0),
                               ({"optimizations": [None]}, 2), ([], 2)):
                source.write_text(json.dumps(data), encoding="utf-8")
                result = subprocess.run([sys.executable, "-B", str(script), str(source)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stderr)
                if code == 0:
                    self.assertEqual(len(json.loads(result.stdout)["rows"]), 1)
            source.write_text('{"direction":"higher","blocks":[{"control":100,"candidate":110}]}', encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(script), str(source), "--summarize"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["gain_pct"], 10)


if __name__ == "__main__":
    unittest.main()
