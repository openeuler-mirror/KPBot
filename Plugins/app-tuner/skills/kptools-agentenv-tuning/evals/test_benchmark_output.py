"""Test benchmark-log rejection rules with explicit synthetic protocol fixtures."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_benchmark_output.py"


class BenchmarkOutputTests(unittest.TestCase):
    """These parser tests are not model evaluations or actual performance samples."""

    def check_log(self, text: str, exit_code: int = 0, expected: int = 3) -> dict:
        """Exercise the CLI without invoking a shell or a real benchmark."""
        with tempfile.TemporaryDirectory(prefix="agentenv-log-test-") as directory:
            log = Path(directory) / "benchmark.log"
            log.write_text(text, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, str(CHECKER), "--log", str(log),
                    "--benchmark", "snapshot_resume", "--expected-samples", str(expected),
                    "--exit-code", str(exit_code),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        result = json.loads(completed.stdout)
        usable = result["artifact_status"] == "usable"
        self.assertEqual(completed.returncode, 0 if usable else 1)
        self.assertIsNone(result["gain_pct"])
        return result

    def test_valid_raw_samples(self) -> None:
        result = self.check_log("snapshot_resume    samples [110.00 ms, 111.25 ms, 109.50 ms]\n")
        self.assertEqual(result["artifact_status"], "usable")
        self.assertEqual(result["samples_ms"], [110.0, 111.25, 109.5])

    def test_exit_zero_with_skipping_is_rejected(self) -> None:
        result = self.check_log("Skipping snapshot_resume: sandbox setup failed\n")
        self.assertEqual(result["artifact_status"], "rejected")

    def test_nonzero_exit_with_samples_is_rejected(self) -> None:
        result = self.check_log("snapshot_resume samples [1 ms, 2 ms, 3 ms]\n", exit_code=1)
        self.assertEqual(result["artifact_status"], "rejected")

    def test_summary_without_raw_samples_is_rejected(self) -> None:
        self.assertEqual(
            self.check_log("snapshot_resume mean 110.00 ms\n")["artifact_status"], "rejected"
        )

    def test_wrong_sample_count_is_rejected(self) -> None:
        self.assertEqual(
            self.check_log("snapshot_resume samples [1 ms, 2 ms]\n")["artifact_status"], "rejected"
        )

    def test_negative_sample_is_rejected(self) -> None:
        self.assertEqual(
            self.check_log("snapshot_resume samples [-1 ms, 2 ms, 3 ms]\n")["artifact_status"],
            "rejected",
        )

    def test_nonfinite_or_malformed_samples_are_rejected(self) -> None:
        for value in ("NaN", "inf", "1e999", "1.2.3"):
            with self.subTest(value=value):
                result = self.check_log(f"snapshot_resume samples [{value} ms, 2 ms, 3 ms]\n")
                self.assertEqual(result["artifact_status"], "rejected")

    def test_unsupported_unit_is_rejected(self) -> None:
        result = self.check_log("snapshot_resume samples [1 s, 2 ms, 3 ms]\n")
        self.assertEqual(result["artifact_status"], "rejected")

    def test_duplicate_sample_lines_are_rejected(self) -> None:
        line = "snapshot_resume samples [1 ms, 2 ms, 3 ms]\n"
        self.assertEqual(self.check_log(line + line)["artifact_status"], "rejected")

    def test_panic_or_unmatched_filter_is_rejected(self) -> None:
        for marker in (
            "thread 'main' panicked at benchmark.rs:12\n",
            "No snapshot benchmarks matched filter(s): typo\n",
        ):
            with self.subTest(marker=marker):
                result = self.check_log(marker + "snapshot_resume samples [1 ms, 2 ms, 3 ms]\n")
                self.assertEqual(result["artifact_status"], "rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
