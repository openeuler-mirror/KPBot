"""Tier A: paired calculations, invalid records and historical excerpt checks."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import statistics
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "paired", ROOT / "scripts/analyze_pairs.py"
)
paired = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(paired)


def measurement():
    blocks = []
    for experiment in ("AA", "BB", "AB"):
        for block_id, order in enumerate(("ABBA", "BAAB")):
            times = (10, 8, 8, 14) if order == "ABBA" else (9, 12, 12, 9)
            legs = []
            for index, label in enumerate(order, 1):
                if experiment == "AA":
                    implementation = "original"
                elif experiment == "BB":
                    implementation = "candidate"
                else:
                    implementation = "original" if label == "A" else "candidate"
                elapsed = times[index - 1] if experiment == "AB" else 10
                legs.append({"leg_index": index, "label": label,
                             "implementation": implementation, "iterations": 1,
                             "elapsed_ns": elapsed})
            blocks.append({"experiment": experiment, "block_id": block_id,
                           "order": order, "legs": legs})
    return {"experiment_id": "synthetic", "workload_id": "tiny", "process_run_id": "1",
            "controls": {"median_min": 0.97, "median_max": 1.03,
                         "p10_min": 0.95, "p90_max": 1.05}, "blocks": blocks}


class ProtocolTests(unittest.TestCase):
    def test_paired_statistic(self):
        result = paired.analyze(measurement())
        self.assertAlmostEqual(result["experiments"]["AB"]["median"], 17 / 12)
        self.assertEqual(result["measurement_status"], "controls_passed")
        self.assertEqual(result["experiments"]["AA"]["median"], 1)

    def test_failed_control_keeps_candidate_data(self):
        payload = measurement()
        payload["blocks"][0]["legs"][0]["elapsed_ns"] = 20
        result = paired.analyze(payload)
        self.assertEqual(result["measurement_status"], "inconclusive")
        self.assertEqual(result["inconclusive_reasons"], ["self_controls_failed"])
        self.assertAlmostEqual(result["experiments"]["AB"]["median"], 17 / 12)

    def test_unbalanced_orders_cannot_pass_on_unit_ratios(self):
        for experiment in ("AA", "BB", "AB"):
            with self.subTest(experiment=experiment):
                payload = measurement()
                for block in payload["blocks"]:
                    if block["experiment"] == experiment:
                        block["order"] = "ABBA"
                        for leg, label in zip(block["legs"], "ABBA"):
                            leg["label"] = label
                            leg["implementation"] = (
                                "original" if experiment == "AA"
                                or (experiment == "AB" and label == "A")
                                else "candidate"
                            )
                            leg["elapsed_ns"] = 10
                result = paired.analyze(payload)
                self.assertEqual(result["measurement_status"], "inconclusive")
                self.assertEqual(result["inconclusive_reasons"], ["unbalanced_orders"])
                self.assertEqual(result["experiments"][experiment]["order_counts"],
                                 {"ABBA": 2, "BAAB": 0})
                self.assertEqual(len(result["experiments"]["AB"]["ratios"]), 2)

    def test_odd_group_counts_allow_one_extra_order(self):
        payload = measurement()
        payload["expected_blocks"] = {"AA": 3, "BB": 3, "AB": 3}
        for experiment in ("AA", "BB", "AB"):
            block = copy.deepcopy(next(
                block for block in payload["blocks"]
                if block["experiment"] == experiment and block["order"] == "ABBA"
            ))
            block["block_id"] = 2
            payload["blocks"].append(block)
        result = paired.analyze(payload)
        self.assertEqual(result["measurement_status"], "controls_passed")
        self.assertEqual(result["inconclusive_reasons"], [])
        self.assertEqual(result["experiments"]["AB"]["order_counts"],
                         {"ABBA": 2, "BAAB": 1})

    def test_mixed_identity_on_individual_leg(self):
        payload = measurement()
        payload["blocks"][0]["legs"][0]["workload_id"] = "another-input"
        with self.assertRaises(ValueError):
            paired.analyze(payload)

    def test_unrepresentable_positive_ratios(self):
        for case, numerator, denominator in (
            ("underflow", 1, 10 ** 400), ("overflow", 10 ** 400, 1)
        ):
            with self.subTest(case=case):
                payload = measurement()
                for block in payload["blocks"]:
                    if block["experiment"] == "AB":
                        for leg in block["legs"]:
                            leg["elapsed_ns"] = (
                                numerator if leg["label"] == "A" else denominator
                            )
                with self.assertRaises(ValueError):
                    paired.analyze(payload)

    def test_large_finite_median(self):
        payload = measurement()
        for block in payload["blocks"]:
            if block["experiment"] == "AB":
                for leg in block["legs"]:
                    leg["elapsed_ns"] = 10 ** 308 if leg["label"] == "A" else 1
        result = paired.analyze(payload)
        self.assertEqual(result["experiments"]["AB"]["median"], 1e308)

    def test_declared_block_counts(self):
        payload = measurement()
        payload["expected_blocks"] = {"AA": 2, "BB": 2, "AB": 2}
        self.assertEqual(paired.analyze(payload)["completeness_status"], "complete")
        payload["blocks"].pop()
        with self.assertRaises(ValueError):
            paired.analyze(payload)

    def test_undeclared_counts_are_not_called_complete(self):
        self.assertEqual(paired.analyze(measurement())["completeness_status"],
                         "not_declared")

    def test_malformed_records(self):
        mutations = [
            lambda p: p["blocks"][0]["legs"].pop(),
            lambda p: p["blocks"].append(copy.deepcopy(p["blocks"][0])),
            lambda p: p["blocks"][0]["legs"][0].update(label="B"),
            lambda p: p["blocks"][0]["legs"][0].update(implementation="candidate"),
            lambda p: p["blocks"][0]["legs"][0].update(iterations=2),
            lambda p: p["blocks"][0]["legs"][0].update(iterations=True),
            lambda p: p["blocks"][0]["legs"][0].update(elapsed_ns=0),
            lambda p: p["controls"].update(p90_max=float("nan")),
            lambda p: p.update(workload_id=""),
            lambda p: p["blocks"][0].update(workload_id="another-input"),
            lambda p: p.update(expected_blocks=None),
            lambda p: p.update(expected_blocks={"AA": 1}),
            lambda p: p.update(expected_blocks={"AA": True, "BB": 2, "AB": 2}),
            lambda p: p.update(blocks=p["blocks"][:2]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                payload = measurement()
                mutate(payload)
                with self.assertRaises(ValueError):
                    paired.analyze(payload)

    def test_reset_every_operation_and_after_warmup(self):
        def run_batch(reset_each):
            state = [100]
            state[0] -= 1  # Warmup changes the same input.
            state[0] = 100
            outputs = []
            for _ in range(3):
                if reset_each:
                    state[0] = 100
                state[0] -= 1
                outputs.append(state[0])
            return outputs
        self.assertEqual(run_batch(True), [99, 99, 99])
        self.assertEqual(run_batch(False), [99, 98, 97])

    def test_historical_w1_medians(self):
        path = ROOT / "assets/bp-w1-evidence.json"
        evidence = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(len(evidence["runs"]), 3)
        for run, expected in zip(evidence["runs"], (1.953, 2.024, 1.554)):
            self.assertEqual(len(run["paired_ratios"]), 21)
            self.assertAlmostEqual(
                statistics.median(run["paired_ratios"]), expected, places=3
            )

    def test_command_line_success_and_error(self):
        for payload, expected_code in ((measurement(), 0), ({}, 2)):
            with self.subTest(expected_code=expected_code):
                output = io.StringIO()
                with (
                    patch("sys.argv", ["analyze_pairs.py", "input.json"]),
                    patch("builtins.open", mock_open(read_data=json.dumps(payload))),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(paired.main(), expected_code)
                if expected_code == 0:
                    self.assertEqual(
                        json.loads(output.getvalue())["measurement_status"],
                        "controls_passed",
                    )


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolTests)
    )
    destination = Path(__file__).parent / "results"
    destination.mkdir(exist_ok=True)
    (destination / "result.json").write_text(json.dumps({
        "success": result.wasSuccessful(), "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "scope": "tier A logic and mock reset; no agent or hardware run",
    }, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
