"""Tier A: accumulator planning formula, boundary rejection and CLI exit codes."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "accplan", ROOT / "scripts/accumulator_plan.py"
)
accplan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(accplan)


class ProtocolTests(unittest.TestCase):
    def test_accumulator_formula(self):
        plan = accplan.recommend(4.0, 0.5)
        self.assertEqual(plan["accumulators"], 8)
        self.assertEqual(plan["unroll_factor"], 8)
        self.assertEqual(plan["elements_per_iter"], 8)

    def test_simd_lanes_scale_elements_per_iter(self):
        plan = accplan.recommend(4.0, 0.5, simd_lanes=4)
        self.assertEqual(plan["accumulators"], 8)
        self.assertEqual(plan["elements_per_iter"], 32)

    def test_exact_division(self):
        plan = accplan.recommend(4.0, 1.0)
        self.assertEqual(plan["accumulators"], 4)

    def test_ceil_rounding(self):
        plan = accplan.recommend(5.0, 2.0)
        self.assertEqual(plan["accumulators"], 3)

    def test_invalid_latency_throughput(self):
        for latency, throughput in ((0, 0.5), (-1, 0.5), (4, 0), (4, -1)):
            with self.subTest(latency=latency, throughput=throughput):
                with self.assertRaises(ValueError):
                    accplan.recommend(latency, throughput)

    def test_latency_less_than_throughput_rejected(self):
        with self.assertRaises(ValueError):
            accplan.recommend(0.5, 4.0)

    def test_invalid_simd_lanes(self):
        for lanes in (0, -1, 1.5, True):
            with self.subTest(lanes=lanes):
                with self.assertRaises(ValueError):
                    accplan.recommend(4.0, 0.5, simd_lanes=lanes)

    def test_command_line_success_and_error(self):
        with (
            patch("sys.argv", ["accumulator_plan.py", "--latency", "4",
                               "--throughput", "0.5", "--simd-lanes", "4"]),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(accplan.main(), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["accumulators"], 8)
            self.assertEqual(payload["elements_per_iter"], 32)

        with (
            patch("sys.argv", ["accumulator_plan.py", "--latency", "-1"]),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(accplan.main(), 2)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolTests)
    )
    destination = Path(__file__).parent / "results"
    destination.mkdir(exist_ok=True)
    (destination / "result.json").write_text(json.dumps({
        "success": result.wasSuccessful(), "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "scope": "tier A accumulator planning logic; no agent or hardware run",
    }, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)