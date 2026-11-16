"""Tier A: nested intervals, request isolation and archived fraction arithmetic."""

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
    "sessions", ROOT / "scripts/analyze_session.py"
)
sessions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sessions)


def sample():
    spans = []
    for identifier, stage, start, end in (
        ("1", "rle", 0, 12), ("2", "bp", 1, 4), ("3", "bp", 20, 22)
    ):
        spans.append({"span_id": identifier, "session_id": "request-a",
                      "clock_id": "monotonic", "stage": stage,
                      "start_ns": start, "end_ns": end})
    return {"session_id": "request-a", "clock_id": "monotonic",
            "stopped": True, "active_spans": 0, "start_ns": 0, "end_ns": 100,
            "spans": spans}


class ProtocolTests(unittest.TestCase):
    def test_nested_union(self):
        result = sessions.analyze(sample())
        self.assertEqual(result["stage_ns"], {"rle": 12, "bp": 5})
        self.assertEqual(result["combined_wall_union_ns"], 14)
        self.assertEqual(result["combined_wall_union_fraction"], 0.14)

    def test_empty_capture_is_not_zero_cost_evidence(self):
        payload = sample()
        payload["spans"] = []
        result = sessions.analyze(payload)
        self.assertEqual(result["capture_status"], "no_target_spans")
        self.assertEqual(result["combined_wall_union_ns"], 0)
        self.assertIsNone(result["combined_wall_union_fraction"])

    def test_parallel_union_and_cumulative_time_are_distinct(self):
        payload = sample()
        payload["spans"] = payload["spans"][:2]
        payload["spans"][0].update(start_ns=0, end_ns=80)
        payload["spans"][1].update(start_ns=0, end_ns=70)
        result = sessions.analyze(payload)
        self.assertEqual(sum(result["stage_ns"].values()), 150)
        self.assertEqual(result["combined_wall_union_ns"], 80)
        self.assertTrue(result["overlap_observed"])
        self.assertEqual(result["fraction_kind"], "wall_interval_union")

    def test_mixed_request_and_clock_rejected(self):
        for field, value in (("session_id", "request-b"), ("clock_id", "other")):
            with self.subTest(field=field):
                payload = sample()
                payload["spans"][0][field] = value
                with self.assertRaises(ValueError):
                    sessions.analyze(payload)

    def test_incomplete_or_invalid_snapshot(self):
        mutations = [
            lambda p: p.update(stopped=False),
            lambda p: p.update(active_spans=1),
            lambda p: p.update(active_spans=False),
            lambda p: p["spans"][0].update(end_ns=101),
            lambda p: p["spans"][0].update(end_ns=None),
            lambda p: p["spans"].append(copy.deepcopy(p["spans"][0])),
            lambda p: p["spans"][0].update(start_ns=True),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                payload = sample()
                mutate(payload)
                with self.assertRaises(ValueError):
                    sessions.analyze(payload)

    def test_overlapping_stage_has_no_cumulative_fraction(self):
        payload = sample()
        payload["spans"][2].update(start_ns=2, end_ns=5)
        result = sessions.analyze(payload)
        self.assertTrue(result["stage_overlap"]["bp"])
        self.assertIsNone(result["stage_fraction"]["bp"])
        self.assertEqual(result["combined_wall_union_ns"], 12)

    def test_independent_request_snapshots(self):
        first = sample()
        second = copy.deepcopy(first)
        second["session_id"] = "request-b"
        for span in second["spans"]:
            span["session_id"] = "request-b"
        self.assertEqual(sessions.analyze(first)["session_id"], "request-a")
        self.assertEqual(sessions.analyze(second)["session_id"], "request-b")
        self.assertEqual(sessions.analyze(first)["combined_wall_union_ns"], 14)

    def test_command_line_success_and_error(self):
        for payload, expected_code in ((sample(), 0), ({}, 2)):
            with self.subTest(expected_code=expected_code):
                output = io.StringIO()
                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        patch("sys.argv", ["analyze_session.py", "input.json"])
                    )
                    stack.enter_context(
                        patch("builtins.open", mock_open(read_data=json.dumps(payload)))
                    )
                    stack.enter_context(contextlib.redirect_stdout(output))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    self.assertEqual(sessions.main(), expected_code)
                if expected_code == 0:
                    self.assertEqual(
                        json.loads(output.getvalue())["combined_wall_union_ns"], 14
                    )

    def test_historical_twelve_round_fractions(self):
        path = ROOT / "assets/tpch-fractions-evidence.json"
        evidence = json.loads(path.read_text(encoding="utf-8-sig"))
        expected = {("q1", "original"): 1.310, ("q1", "both"): 1.025,
                    ("q6", "original"): 1.495, ("q6", "both"): 1.234}
        groups = {key: [] for key in expected}
        self.assertEqual(len(evidence["batches"]), 2)
        self.assertEqual(evidence["batches"][0]["extension_sha256"],
                         evidence["batches"][1]["extension_sha256"])
        self.assertEqual(evidence["batches"][0]["dataset_sha256"],
                         evidence["batches"][1]["dataset_sha256"])
        for batch in evidence["batches"]:
            for record in batch["records"]:
                self.assertEqual(len(record["samples"]), 10)
                groups[record["query"], record["mode"]].append(statistics.median(
                    row["combined_decode_ns"] / row["query_ns"]
                    for row in record["samples"]
                ))
        for key, values in groups.items():
            self.assertEqual(len(values), 12)
            self.assertAlmostEqual(
                statistics.median(values) * 100, expected[key], places=3
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
        "scope": "tier A intervals and snapshots; service/agent integration not run",
    }, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
