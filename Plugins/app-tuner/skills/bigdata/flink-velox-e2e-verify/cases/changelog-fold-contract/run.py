#!/usr/bin/env python3
"""changelog-fold-contract — tier-A case for flink-velox-e2e-verify.

Exercises the pure fold/compare contract of scripts/changelog_diff.py with
no Flink cluster: append-only fold, upsert pairs, deletes, withdrawal of a
row never present, multiset duplicate counting, noise-line tolerance, order
independence, the empty-capture guard (an empty capture is a difference,
not a match), truncated capture lines (a row that starts with a flag but
never terminates is a capture error, not noise — exit 3, never a match),
the comma-in-STRING limitation (documented protocol limit, pinned so the
data-domain rule in SKILL.md stays necessary), and CLI exit codes 0/1/2/3
(2 = tool error: missing/unreadable input is a single stderr line, never
a verdict; 1 belongs to MISMATCH alone). These pin the comparison semantics
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


def run_cli(a, b):
    """Run changelog_diff.py as a subprocess on two scratch captures.

    Either side may instead be given as a path string — used for the
    missing-input (tool error) tests."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    a_path = a if isinstance(a, str) else SCRATCH / "native.out"
    b_path = b if isinstance(b, str) else SCRATCH / "gfv.out"
    if not isinstance(a, str):
        a_path.write_text("\n".join(a) + "\n", encoding="utf-8")
    if not isinstance(b, str):
        b_path.write_text("\n".join(b) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(a_path), str(b_path)],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


class FoldSemanticsTest(unittest.TestCase):
    """fold() must implement the multiset semantics of changelog-semantics.md."""

    def test_append_only(self):
        final, stats, errors, caperr = fold_lines("append.out", ["+I[a, b]", "+I[a, b]", "+I[c]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"a, b": 2, "c": 1})
        self.assertEqual(dict(stats), {"+I": 3})

    def test_upsert_pair_replaces_old_row(self):
        final, stats, errors, caperr = fold_lines(
            "upsert.out", ["+I[1, old]", "-U[1, old]", "+U[1, new]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"1, new": 1})
        self.assertEqual(dict(stats), {"+I": 1, "-U": 1, "+U": 1})

    def test_delete_withdraws_row(self):
        final, _, errors, caperr = fold_lines("delete.out", ["+I[x]", "-D[x]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {})

    def test_withdrawal_of_missing_row_is_fold_error(self):
        final, _, errors, caperr = fold_lines("ghost.out", ["+I[x]", "-U[ghost]", "-D[x]"])
        self.assertEqual(len(errors), 1)
        self.assertIn("withdraws row never present", errors[0])
        self.assertEqual(dict(final), {})

    def test_noise_lines_ignored(self):
        # Log noise and any row still carrying a subtask prefix must be
        # ignored: capture strips prefixes, and the fold only accepts pure
        # FLAG[payload] rows.
        final, stats, errors, caperr = fold_lines(
            "noise.out",
            ["INFO: job switched to RUNNING", "+I[keep]", "3> +I[leaked-prefix]",
             "", "garbage without flags"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"keep": 1})
        self.assertEqual(dict(stats), {"+I": 1})

    def test_order_independent_same_multiset(self):
        # Different changelog paths (retractions interleaved differently)
        # folding to the same final multiset are equivalent.
        final_a, _, err_a, caperr_a = fold_lines("order_a.out",
                                       ["+I[x]", "+I[y]", "-U[x]", "+U[x2]"])
        final_b, _, err_b, caperr_b = fold_lines("order_b.out",
                                       ["+I[y]", "+I[x]", "+U[x2]", "-U[x]"])
        self.assertEqual(err_a, [])
        self.assertEqual(err_b, [])
        self.assertEqual(final_a, final_b)
        self.assertEqual(dict(final_a), {"y": 1, "x2": 1})

    def test_duplicates_have_multiplicity(self):
        final, _, errors, _ = fold_lines("dups.out", ["+I[x]", "+I[x]"])
        self.assertEqual(errors, [])
        self.assertEqual(dict(final), {"x": 2})

    def test_truncated_flag_line_is_a_capture_error(self):
        # "+I[broken" starts like a real row but never terminates — it was
        # output once; dropping it as noise would silently drop rows from
        # the comparison. It must surface as a capture error (with line
        # number and raw text), never enter the multiset, and never fold
        # errors.
        final, stats, errors, caperr = fold_lines(
            "trunc.out", ["+I[ok]", "+I[broken", "-U[cut"])
        self.assertEqual(errors, [])
        self.assertEqual(len(caperr), 2)
        self.assertIn(":2:", caperr[0])
        self.assertIn("+I[broken", caperr[0])
        self.assertIn(":3:", caperr[1])
        self.assertIn("-U[cut", caperr[1])
        self.assertEqual(dict(final), {"ok": 1})
        self.assertEqual(dict(stats), {"+I": 1})

    def test_flag_starting_mid_line_is_still_noise(self):
        # a flag that does not sit at the start of the line is log noise
        # (capture strips subtask prefixes; anything else is not a row)
        final, _, errors, caperr = fold_lines(
            "midnoise.out", ["+I[ok]", "client log +I[truncated echo"])
        self.assertEqual(errors, [])
        self.assertEqual(caperr, [])
        self.assertEqual(dict(final), {"ok": 1})


class CliContractTest(unittest.TestCase):
    """CLI verdicts and exit codes: 0=match, 1=mismatch/malformed, 2=usage
    or tool error (unreadable input — never a verdict), 3=capture error
    (truncated/corrupt capture — never a match)."""

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

    def test_missing_input_file_is_a_tool_error_exit_2(self):
        # a missing input is a tool error, not a verdict: one line to
        # stderr, exit 2, no traceback, no RESULT line — exit 1 belongs to
        # MISMATCH alone.
        missing = str(SCRATCH / "no-such-file.out")
        rc, out, err = run_cli(missing, ["+I[x]"])
        self.assertEqual(rc, 2)
        self.assertNotIn("RESULT:", out)
        self.assertNotIn("Traceback", err)
        err_lines = [l for l in err.splitlines() if l.strip()]
        self.assertEqual(len(err_lines), 1, err)
        self.assertIn("cannot read capture", err_lines[0])
        self.assertIn("no-such-file.out", err_lines[0])

    def test_tool_error_outranks_a_would_be_mismatch(self):
        # one readable side with real differences, one missing file: the
        # tool error must still be exit 2, not conflated with exit 1.
        rc, out, err = run_cli(["+I[a]"], str(SCRATCH / "no-such-file.out"))
        self.assertEqual(rc, 2)
        self.assertNotIn("RESULT:", out)
        self.assertEqual(len([l for l in err.splitlines() if l.strip()]), 1)

    def test_truncated_line_is_capture_error_exit_3(self):
        # both sides hold +I[ok] and one side also a truncated row: the
        # old fold ignored the truncated line and reported MATCH — it must
        # now be CAPTURE ERROR (exit 3), reported with line number and raw
        # text, and never a match.
        rc, out, err = run_cli(["+I[ok]", "+I[broken"], ["+I[ok]"])
        self.assertEqual(rc, 3)
        self.assertIn("CAPTURE ERROR:", out)
        self.assertIn(":2:", out)
        self.assertIn("+I[broken", out)
        self.assertIn("RESULT: CAPTURE ERROR", out)
        self.assertNotIn("RESULT: MATCH", out)

    def test_capture_error_on_either_side_exit_3(self):
        # a truncated row on the gfv side alone must refuse just the same
        rc, out, _ = run_cli(["+I[ok]"], ["+I[ok]", "-D[cut"])
        self.assertEqual(rc, 3)
        self.assertIn("RESULT: CAPTURE ERROR", out)
        self.assertNotIn("RESULT: MATCH", out)

    def test_comma_in_string_field_is_the_documented_limitation(self):
        # ('a, b', 'c') and ('a', 'b, c') print as the same +I[a, b, c]:
        # the fold is text-level and cannot recover field boundaries — a
        # protocol limit documented in references/changelog-semantics.md.
        # This pins that text-level behavior (and is exactly why SKILL.md
        # constrains case data: STRING values must not contain characters
        # that break print-row field boundaries).
        rc, out, _ = run_cli(["+I[a, b, c]"], ["+I[a, b, c]"])
        self.assertEqual(rc, 0)
        self.assertIn("MATCH (1 row", out)

    def test_usage_error_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True,
            check=False)
        self.assertEqual(proc.returncode, 2)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    module = sys.modules[__name__]
    loader = unittest.defaultTestLoader
    names = sys.argv[1:]
    suite = (loader.loadTestsFromNames(names, module) if names
             else loader.loadTestsFromModule(module))
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
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
