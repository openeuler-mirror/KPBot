"""Contract test for the stackless profiling step detector."""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py"
)
REPORT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-analysis-nostack/scripts/report_html_nostack.py"
)


def ev(name, pid, ts_us, dur_us, cat):
    return {
        "ph": "X",
        "name": name,
        "pid": pid,
        "tid": pid,
        "ts": f"{ts_us:.3f}",
        "dur": dur_us,
        "cat": cat,
    }


def write_trace(root, with_sidecar):
    events = [
        ev("Event::synchronize", 100, 1_000.0, 5.0, "cpu_op"),
        ev("aten::matmul", 100, 5_000.0, 10.0, "cpu_op"),
        ev("Computing", 300, 6_000.0, 1_000.0, "Overlap Analysis"),
        ev("Computing", 300, 10_000.0, 1_000.0, "Overlap Analysis"),
        ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 200, 20_000.0, 100.0, "cann"),
        ev("aten::matmul", 100, 21_000.0, 10.0, "cpu_op"),
        ev("Computing", 300, 25_000.0, 1_000.0, "Overlap Analysis"),
        ev("Computing", 300, 30_000.0, 1_000.0, "Overlap Analysis"),
        ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 200, 40_000.0, 100.0, "cann"),
        ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 200, 40_050.0, 10.0, "cann"),
    ]
    path = root / "trace_view.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    if with_sidecar:
        con = sqlite3.connect(root / "ascend_pytorch_profiler_1.db")
        con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
        con.executemany(
            "INSERT INTO STEP_TIME VALUES(?,?,?)",
            [
                (0, 1_000_000, 20_000_000),
                (1, 20_000_000, 40_000_000),
            ],
        )
        con.commit()
        con.close()
    return path


class NoStackStepDetectTests(unittest.TestCase):
    def run_detect(self, trace, *args):
        run = subprocess.run(
            [sys.executable, str(SCRIPT), str(trace), "--json", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_trace_only_uses_device_sync_and_drops_shutdown_sync(self):
        with tempfile.TemporaryDirectory(prefix="nostack-step-") as directory:
            trace = write_trace(Path(directory), with_sidecar=False)
            result = self.run_detect(trace, "--source", "trace")
        self.assertEqual(result["source"], "trace")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["start_ms"], 0.0)
        self.assertAlmostEqual(result["steps"][0]["end_ms"], 19.1, places=3)
        self.assertAlmostEqual(result["steps"][1]["end_ms"], 39.1, places=3)

    def test_step_time_sidecar_is_preferred(self):
        with tempfile.TemporaryDirectory(prefix="nostack-step-") as directory:
            trace = write_trace(Path(directory), with_sidecar=True)
            result = self.run_detect(trace)
        self.assertEqual(result["source"], "step_time")
        self.assertEqual(result["confidence"], "exact")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["start_ns"], 1_000_000)
        self.assertEqual(result["steps"][1]["end_ns"], 40_000_000)

    def test_comm_step_mismatch_falls_back_to_trace_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="nostack-step-") as directory:
            trace = write_trace(Path(directory), with_sidecar=False)
            (Path(directory) / "communication.json").write_text(
                json.dumps({"step0": {}, "step1": {}, "step2": {}}), encoding="utf-8"
            )
            result = self.run_detect(trace)
        self.assertEqual(result["source"], "comm")
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(len(result["steps"]), 2)
        self.assertTrue(any("降级用 trace sync 边界" in n for n in result["notes"]))

    def test_report_flags_step_failure_instead_of_zero_steps(self):
        with tempfile.TemporaryDirectory(prefix="nostack-step-") as directory:
            path = Path(directory) / "trace_view.json"
            single = [ev("aten::matmul", 100, 5_000.0, 10.0, "cpu_op")]
            path.write_text(json.dumps(single), encoding="utf-8")
            (Path(directory) / "communication.json").write_text(
                json.dumps({"step0": {}}), encoding="utf-8"
            )
            out = Path(directory) / "report.html"
            run = subprocess.run(
                [sys.executable, str(REPORT_SCRIPT), str(path), "--out", str(out)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertIn("step 定位失败", html)
        self.assertNotIn("设备基本跟上", html)
        self.assertNotIn("Free 0.0%", html)
        self.assertNotIn("整份 profiling", html)


if __name__ == "__main__":
    unittest.main()
