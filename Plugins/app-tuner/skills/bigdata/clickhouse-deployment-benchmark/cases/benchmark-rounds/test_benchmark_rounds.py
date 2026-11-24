"""Offline tests for the fresh-directory benchmark round wrapper."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/run_benchmark_rounds.sh"
)

SUCCESS_CLIENT = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALL_LOG"
echo "QPS 1.0"
"""

FAILING_CLIENT = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALL_LOG"
count=$(wc -l < "$CALL_LOG")
if [[ $count -ge 3 ]]; then
    echo "simulated benchmark failure" >&2
    exit 1
fi
echo "QPS 1.0"
"""

QUERY_FILE_CLIENT = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALL_LOG"
count=$(wc -l < "$CALL_LOG")
cat > "$QUERY_COPIES/query-$count.sql"
echo "QPS 1.0"
"""


class BenchmarkRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = self.root / "calls.log"
        self.env = dict(
            os.environ,
            CALL_LOG=str(self.calls),
            QUERY_COPIES=str(self.root),
        )

    def client(self, body=SUCCESS_CLIENT):
        path = self.root / "fake-clickhouse-benchmark"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPT), *map(str, args)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def bench_args(self):
        return ("--", "--query", "SELECT 1", "--iterations", "1")

    def test_success_writes_separate_warmup_and_round_files(self):
        output = self.root / "results" / "run-1"
        completed = self.run_script(
            "--output",
            output,
            "--rounds",
            "2",
            "--client",
            self.client(),
            *self.bench_args(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for name in ("warmup", "round-1", "round-2"):
            log = output / f"{name}.log"
            self.assertTrue(log.is_file(), name)
            self.assertIn("QPS 1.0", log.read_text())
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("--query SELECT 1" in line for line in calls))

    def test_query_file_is_reopened_for_warmup_and_every_round(self):
        output = self.root / "run-1"
        query_file = self.root / "queries with spaces.sql"
        queries = "SELECT 1;\nSELECT 2;\n"
        query_file.write_text(queries, encoding="utf-8")
        completed = self.run_script(
            "--output",
            output,
            "--rounds",
            "2",
            "--query-file",
            query_file.name,
            "--client",
            self.client(QUERY_FILE_CLIENT),
            "--",
            "--queries-format",
            "script",
            "--iterations",
            "1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 3)
        for invocation in range(1, 4):
            copied = self.root / f"query-{invocation}.sql"
            self.assertEqual(copied.read_text(encoding="utf-8"), queries)

    def test_query_file_input_errors_happen_before_client_or_output(self):
        query_file = self.root / "queries.sql"
        query_file.write_text("SELECT 1\n", encoding="utf-8")
        client = self.client()
        cases = {
            "conflicting query": (
                "--query-file",
                query_file,
                "--",
                "--query",
                "SELECT 1",
                "--iterations",
                "1",
            ),
            "missing file": (
                "--query-file",
                self.root / "missing.sql",
                "--",
                "--iterations",
                "1",
            ),
            "directory instead of file": (
                "--query-file",
                self.root,
                "--",
                "--iterations",
                "1",
            ),
            "implicit standard input": ("--", "--iterations", "1"),
        }
        unreadable = self.root / "unreadable.sql"
        unreadable.write_text("SELECT 1\n", encoding="utf-8")
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o600)
        if not os.access(unreadable, os.R_OK):
            cases["unreadable file"] = (
                "--query-file",
                unreadable,
                "--",
                "--iterations",
                "1",
            )
        for name, trailing_args in cases.items():
            with self.subTest(name=name):
                output = self.root / "run-1"
                completed = self.run_script(
                    "--output",
                    output,
                    "--client",
                    client,
                    *trailing_args,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(output.exists())
                self.assertFalse(self.calls.exists())

    def test_missing_or_invalid_finite_limit_fails_before_client(self):
        client = self.client()
        invalid_limits = (
            (),
            ("--iterations", "0"),
            ("--iterations", "-1"),
            ("--iterations", "invalid"),
            ("--iterations",),
            ("--iterations=0"),
            ("--timelimit", "0"),
            ("--timelimit", "-1"),
            ("--timelimit", "invalid"),
            ("--timelimit", "nan"),
            ("--timelimit", "inf"),
            ("--timelimit", "1e309"),
            ("--timelimit", "1e-4000"),
            ("--timelimit",),
            ("--timelimit=0"),
            ("--iterations", "1", "--iterations", "0"),
            ("--timelimit", "1", "--timelimit", "0"),
        )
        for limit_args in invalid_limits:
            with self.subTest(limit_args=limit_args):
                output = self.root / "run-1"
                completed = self.run_script(
                    "--output",
                    output,
                    "--rounds",
                    "1",
                    "--client",
                    client,
                    "--",
                    "--query",
                    "SELECT 1",
                    *limit_args,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(output.exists())
                self.assertFalse(self.calls.exists())

    def test_positive_iterations_or_timelimit_is_accepted(self):
        valid_limits = (
            ("--iterations=1",),
            ("-i", "1"),
            ("-i1",),
            ("--timelimit", "0.1"),
            ("--timelimit=1e-3",),
            ("-t", "0.1"),
            ("-t0.1",),
            ("--iterations", "0", "--iterations", "1"),
            ("--timelimit", "0", "--timelimit", "1"),
        )
        for index, limit_args in enumerate(valid_limits):
            with self.subTest(limit_args=limit_args):
                output = self.root / f"run-{index}"
                completed = self.run_script(
                    "--output",
                    output,
                    "--rounds",
                    "1",
                    "--client",
                    self.client(),
                    "--",
                    "--query",
                    "SELECT 1",
                    *limit_args,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_existing_output_directory_preserves_prior_files(self):
        output = self.root / "run-1"
        output.mkdir()
        previous = output / "warmup.log"
        previous.write_text("previous baseline\n", encoding="utf-8")
        completed = self.run_script(
            "--output",
            output,
            "--client",
            self.client(),
            *self.bench_args(),
        )
        self.assertEqual(completed.returncode, 3)
        self.assertIn("already exists", completed.stderr)
        self.assertEqual(previous.read_text(), "previous baseline\n")
        self.assertFalse(self.calls.exists())

    def test_failed_round_keeps_earlier_logs_and_stops(self):
        output = self.root / "run-1"
        completed = self.run_script(
            "--output",
            output,
            "--rounds",
            "3",
            "--client",
            self.client(FAILING_CLIENT),
            *self.bench_args(),
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("round-2", completed.stderr)
        self.assertIn("QPS 1.0", (output / "warmup.log").read_text())
        self.assertIn("QPS 1.0", (output / "round-1.log").read_text())
        self.assertIn(
            "simulated benchmark failure",
            (output / "round-2.log").read_text(),
        )
        self.assertFalse((output / "round-3.log").exists())
        self.assertEqual(len(self.calls.read_text().splitlines()), 3)

    def test_single_binary_subcommand_is_forwarded(self):
        output = self.root / "run-1"
        completed = self.run_script(
            "--output",
            output,
            "--rounds",
            "1",
            "--client",
            self.client(),
            "--subcommand",
            "benchmark",
            *self.bench_args(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        first = self.calls.read_text().splitlines()[0]
        self.assertTrue(first.startswith("benchmark --query SELECT 1"))

    def test_bash_invocation_works_with_read_only_script_mode(self):
        copied = self.root / "run_benchmark_rounds.sh"
        copied.write_bytes(SCRIPT.read_bytes())
        copied.chmod(0o644)
        completed = subprocess.run(
            ["bash", str(copied), "--help"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_usage_errors_do_not_create_evidence(self):
        for args in (
            ("--rounds", "2", *self.bench_args()),
            (
                "--output",
                self.root / "run-1",
                "--rounds",
                "0",
                *self.bench_args(),
            ),
            ("--output", self.root / "run-1"),
        ):
            completed = self.run_script(*args)
            self.assertEqual(completed.returncode, 2, args)
        self.assertFalse((self.root / "run-1").exists())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": result.wasSuccessful(),
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
