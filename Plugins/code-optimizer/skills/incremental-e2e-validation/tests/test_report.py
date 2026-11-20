"""Behavior checks using synthetic evidence declarations."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import report


def fixture():
    def sample(config, active, value, run):
        return {"config": config, "active": active, "value": value,
                "condition_id": "conditions-1", "correctness": "passed",
                "completed": True, "hit": True, "run_id": run,
                "artifact_id": config + "-build", "log": run + ".json",
                "correctness_log": config + "-tests.log"}
    blocks = []
    for index in range(3):
        blocks.append({"id": str(index), "sequence": "ABBA",
                       "control": sample("c0", [], 100, f"base-{index}"),
                       "candidate": sample("c1", ["A"], 90, f"a-{index}")})
    return {
        "optimizations": [{"id": "A"}],
        "experiment": {
            "scenario": "synthetic", "baseline": "base", "condition_id": "conditions-1",
            "conditions_log": "conditions.json", "metric": "duration", "unit": "ms",
            "direction": "lower", "performance_framework": "bench",
            "correctness_framework": "tests", "minimum_blocks": 3},
        "results": {"0": {
            "status": "measured", "direction": "lower", "unit": "ms",
            "environment_review": "stable", "blocks": blocks,
            "noise": {"floor_pct": 1, "evidence": "aa.json", "condition_id": "conditions-1"}}}}


class ReportTests(unittest.TestCase):
    def test_full_report(self):
        output = report.render(fixture())
        self.assertIn("+10.000%", output)
        self.assertIn("| 全部组合 |", output)
        self.assertIn("观察到改善", output)

    def test_noise_and_drift(self):
        for change, expected in (
                ({"noise": None}, "波动依据不足"),
                ({"environment_review": "drift"}, "环境漂移")):
            data = fixture()
            data["results"]["0"].update(change)
            self.assertIn(expected, report.render(data))
        data = fixture()
        data["results"]["0"]["noise"]["floor_pct"] = 11
        self.assertIn("可能存在波动", report.render(data))

    def test_evidence_gates(self):
        for field, value, expected in (
                ("condition_id", "changed", "条件不可比"),
                ("correctness", "failed", "正确性未通过"),
                ("completed", False, "执行未完成"),
                ("hit", False, "未确认生效")):
            data = fixture()
            data["results"]["0"]["blocks"][0]["candidate"][field] = value
            output = report.render(data)
            self.assertIn(expected, output)
            self.assertNotIn("+10.000%", output)

    def test_missing_and_incompatible(self):
        data = fixture()
        data["results"] = {}
        self.assertIn("未运行", report.render(data))
        data["optimizations"] = [{"id": "A"}, {"id": "B", "excludes": ["A"]}]
        output = report.render(data)
        self.assertIn("不适用", output)
        self.assertIn("excludes", output)

    def test_invalid_records(self):
        data = fixture()
        invalid = copy.deepcopy(data)
        invalid["results"]["9"] = {}
        with self.assertRaises(ValueError):
            report.render(invalid)
        for field, value in (("value", True), ("active", []), ("log", "")):
            invalid = copy.deepcopy(data)
            invalid["results"]["0"]["blocks"][0]["candidate"][field] = value
            with self.assertRaises(ValueError):
                report.render(invalid)

    def test_duplicate_blocks(self):
        data = fixture()
        data["results"]["0"]["blocks"][1]["id"] = "0"
        with self.assertRaises(ValueError):
            report.render(data)

    def test_insufficient_samples(self):
        data = fixture()
        data["results"]["0"]["blocks"] = data["results"]["0"]["blocks"][:1]
        self.assertIn("样本不足", report.render(data))

    def test_higher_direction(self):
        data = fixture()
        data["experiment"]["direction"] = "higher"
        data["results"]["0"]["direction"] = "higher"
        for block in data["results"]["0"]["blocks"]:
            block["candidate"]["value"] = 110
        self.assertIn("+10.000%", report.render(data))

    def test_multiple_optimizations(self):
        data = fixture()
        template = data["results"]["0"]
        data["optimizations"] = [{"id": name} for name in ("A", "B", "C", "D", "E")]
        matrix = report.plan(data)
        configs = {item["id"]: item for item in matrix["configurations"]}
        data["results"] = {}
        for index, pair in enumerate(matrix["comparisons"]):
            record = copy.deepcopy(template)
            for block in record["blocks"]:
                for side in ("control", "candidate"):
                    config = configs[pair[side]]
                    block[side]["config"] = config["id"]
                    block[side]["active"] = config["active"]
                    block[side]["value"] = 100 - 2 * len(config["active"])
            data["results"][str(index)] = record
        output = report.render(data)
        self.assertEqual(len(matrix["comparisons"]), 13)
        for name in ("A", "B", "C", "D", "E"):
            self.assertIn(f"| {name} |", output)
        self.assertIn("| 全部组合 | — | — | 比较 12；+10.000%", output)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "results.json"
            source.write_text(json.dumps(fixture()), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "report.py"), str(source)],
                capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("全部组合", result.stdout)
            source.write_text("[]", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS / "report.py"), str(source)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
