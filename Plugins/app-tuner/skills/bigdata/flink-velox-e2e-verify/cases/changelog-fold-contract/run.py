#!/usr/bin/env python3
"""changelog-fold-contract — tier-A case for flink-velox-e2e-verify.

Exercises the pure fold/compare contract of scripts/changelog_diff.py with
no Flink cluster: append-only fold, upsert pairs, deletes, withdrawal of a
row never present, multiset duplicate counting, noise-line tolerance, order
independence, the empty-capture guard (an empty capture is a difference,
not a match), and CLI exit codes 0/1/2. These pin the comparison semantics
the dual-run protocol relies on — the exact-match verdict is only as sound
as this fold.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of scripts/

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "changelog_diff.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

spec = importlib.util.spec_from_file_location("changelog_diff", SCRIPT)
changelog_diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog_diff)


def fold_lines(name, lines):
    """Write capture lines to a scratch file and fold it."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changelog_diff.fold(path)


def run_cli(a_lines, b_lines):
    """Run changelog_diff.py as a subprocess on two scratch captures."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    a = SCRATCH / "native.out"
    b = SCRATCH / "gfv.out"
    a.write_text("\n".join(a_lines) + "\n", encoding="utf-8")
    b.write_text("\n".join(b_lines) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(a), str(b)],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


class FoldSemanticsTest(unittest.TestCase):
    """fold() must implement the multiset semantics of changelog-semantics.md."""

    def test_append_only(self):
        final, stats, errors = fold_lines("append.out", ["+I[a, b]", "+I[a, b]", "+I[c]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"a, b": 2, "c": 1})
        self.assertEqual(dict(stats), {"+I": 3})

    def test_upsert_pair_replaces_old_row(self):
        final, stats, errors = fold_lines(
            "upsert.out", ["+I[1, old]", "-U[1, old]", "+U[1, new]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"1, new": 1})
        self.assertEqual(dict(stats), {"+I": 1, "-U": 1, "+U": 1})

    def test_delete_withdraws_row(self):
        final, _, errors = fold_lines("delete.out", ["+I[x]", "-D[x]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {})

    def test_withdrawal_of_missing_row_is_fold_error(self):
        final, _, errors = fold_lines("ghost.out", ["+I[x]", "-U[ghost]", "-D[x]"])
        self.assertEqual(len(errors), 1)
        self.assertIn("withdraws row never present", errors[0])
        self.assertEqual(dict(final), {})

    def test_noise_lines_ignored(self):
        # Log noise and any row still carrying a subtask prefix must be
        # ignored: capture strips prefixes, and the fold only accepts pure
        # FLAG[payload] rows.
        final, stats, errors = fold_lines(
            "noise.out",
            ["INFO: job switched to RUNNING", "+I[keep]", "3> +I[leaked-prefix]",
             "", "garbage without flags"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"keep": 1})
        self.assertEqual(dict(stats), {"+I": 1})

    def test_order_independent_same_multiset(self):
        # Different changelog paths (retractions interleaved differently)
        # folding to the same final multiset are equivalent.
        final_a, _, err_a = fold_lines("order_a.out",
                                       ["+I[x]", "+I[y]", "-U[x]", "+U[x2]"])
        final_b, _, err_b = fold_lines("order_b.out",
                                       ["+I[y]", "+I[x]", "+U[x2]", "-U[x]"])
        self.assertEqual(err_a, [])
        self.assertEqual(err_b, [])
        self.assertEqual(final_a, final_b)
        self.assertEqual(dict(final_a), {"y": 1, "x2": 1})

    def test_duplicates_have_multiplicity(self):
        final, _, errors = fold_lines("dups.out", ["+I[x]", "+I[x]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"x": 2})


class CliContractTest(unittest.TestCase):
    """CLI verdicts and exit codes: 0=match, 1=mismatch/malformed, 2=usage."""

    def test_exact_match_exit_0(self):
        rc, out, _ = run_cli(["+I[a, b]", "+I[c]"], ["+I[c]", "+I[a, b]"])
        self.assertEqual(rc, 0)
        self.assertIn("RESULT: MATCH (2 rows, exact)", out)

    def test_row_difference_exit_1(self):
        rc, out, _ = run_cli(["+I[a]", "+I[shared]"], ["+I[b]", "+I[shared]"])
        self.assertEqual(rc, 1)
        self.assertIn("RESULT: MISMATCH", out)
        self.assertIn("missing in gfv  (x1): [a]", out)
        self.assertIn("extra in gfv    (x1): [b]", out)

    def test_multiset_count_difference_exit_1(self):
        # Same row set, different duplicate counts — must be a mismatch.
        rc, out, _ = run_cli(["+I[x]", "+I[x]"], ["+I[x]"])
        self.assertEqual(rc, 1)
        self.assertIn("missing in gfv  (x1): [x]", out)

    def test_malformed_fold_exit_1(self):
        rc, out, _ = run_cli(["+I[x]", "-U[ghost]"], ["+I[x]"])
        self.assertEqual(rc, 1)
        self.assertIn("FOLD ERROR:", out)
        self.assertIn("MISMATCH (malformed changelog", out)

    def test_empty_captures_exit_1(self):
        # Empty-vs-empty must NOT pass as "MATCH (0 rows)" — an empty
        # capture means a side never produced output and proves nothing.
        rc, out, _ = run_cli([], [])
        self.assertEqual(rc, 1)
        self.assertIn("empty capture on the native side", out)
        self.assertIn("empty capture on the gfv side", out)
        self.assertIn("RESULT: MISMATCH (empty capture", out)
        self.assertNotIn("RESULT: MATCH", out)

    def test_one_empty_side_exit_1(self):
        rc, out, _ = run_cli(["+I[x]"], [])
        self.assertEqual(rc, 1)
        self.assertIn("empty capture on the gfv side", out)
        self.assertNotIn("empty capture on the native side", out)

    def test_rows_all_withdrawn_is_not_an_empty_capture(self):
        # Rows that legitimately existed and were all withdrawn fold to an
        # empty final set on BOTH sides — a real equivalence result.
        rc, out, _ = run_cli(["+I[x]", "-D[x]"], ["+I[x]", "-D[x]"])
        self.assertEqual(rc, 0)
        self.assertIn("MATCH (0 rows, exact)", out)

    def test_formatting_difference_is_real_mismatch(self):
        # DECIMAL/TIMESTAMP rendering differences must not be normalized away.
        rc, out, _ = run_cli(["+I[1, 2024-01-01 00:00:00.0]"],
                             ["+I[1, 2024-01-01 00:00:00]"])
        self.assertEqual(rc, 1)
        self.assertIn("RESULT: MISMATCH", out)

    def test_usage_error_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True,
            check=False)
        self.assertEqual(proc.returncode, 2)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "changelog-fold-contract",
        "skill": "flink-velox-e2e-verify",
        "success": result.wasSuccessful(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }
    (RESULTS / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
