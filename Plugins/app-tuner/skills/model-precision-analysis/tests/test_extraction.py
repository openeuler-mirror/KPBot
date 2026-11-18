#!/usr/bin/env python3
"""Answer-extraction regression tests for scripts/analyze_precision.py.

Run: python3 evals/test_extraction.py
Exit code 0 means every extraction rule still behaves as documented in SKILL.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analyze_precision import answer_of, answers_match  # noqa: E402

CASES = [
    # (prediction, gold, expected_extracted_answer, expected_correct)
    (r"so \boxed{127}", "127", "127", True),
    (r"\boxed{1.4\text{ hours}}", "84", "84", True),
    (r"\boxed{3\text{ hours and }24\text{ minutes}}", "204", "204", True),
    (r"\boxed{204\text{ minutes}}", "204", "204", True),
    (r"\boxed{0.5\text{ hours}}", "30", "30", True),
    (r"\boxed{2 hours 30 min}", "150", "150", True),
    (r"\boxed{1,000}", "1000", "1000", True),
    (r"\boxed{1,234.5}", "1234.5", "1234.5", True),
    (r"\boxed{1{,}000}", "1000", "1000", True),
    (r"\boxed{90\%}", "90%", "90%", True),
    (r"\boxed{5.0}", "5", "5.0", True),
    (r"\boxed{2.50}", "2.5", "2.50", True),
    (r"\boxed{12.}", "12", "12", True),
    # mixed prose: not a pure time expression, must NOT be converted
    (r"\boxed{5 apples in 3 hours}", "180", "3", False),
    # wrong answer stays wrong
    (r"\boxed{54}", "45", "54", False),
    # letter answers: last ANSWER wins over distractor letters
    ("Option A is wrong, C is tempting.\nANSWER: B", "B", "B", True),
    ("Option A is wrong.\nanswer: c", "C", "C", True),
    ("A, B and D are all rejected, so the answer is C", "C", "C", True),
    ("Option A ... ANSWER: A", "B", "A", False),
    # no answer / truncated output
    ("", "210", "", False),
    ("Reasoning was cut off mid", "210", "", False),
    # truncated inside \boxed: must not be read as an answer
    (r"Working\ldots so \boxed{21", "21", "", False),
    # a truncated tail wins over an earlier complete box
    (r"Earlier \boxed{3} then cut off at \boxed{4", "3", "", False),
    # truncated inside nested braces (\frac) must not fall back to the last number
    (r"so \boxed{\frac{1}{2", "0.5", "", False),
    # multiple complete boxes: the LAST one is the final answer
    (r"We had \boxed{12} but after checking the answer is \boxed{45}", "45", "45", True),
    (r"We had \boxed{45} but corrected it to \boxed{12}", "45", "12", False),
    (r"reject \boxed{0} then \boxed{127}", "127", "127", True),
    (r"Options: \boxed{A} is a distractor, so \boxed{D}", "D", "D", True),
    (r"\boxed{draft} then finalize \boxed{equilateral}", "equilateral", "equilateral", True),
    # answer stated without \boxed
    ("therefore the answer is 127", "127", "127", True),
    # text gold
    (r"\boxed{equilateral}", "equilateral", "equilateral", True),
]

failures = 0
for prediction, gold, expected_ans, expected_ok in CASES:
    answer, source = answer_of(prediction, gold)
    got_ok = answers_match(answer, gold) if answer else False
    ok = answer == expected_ans and got_ok == expected_ok
    if not ok:
        failures += 1
    print(
        f"{'PASS' if ok else 'FAIL'}  gold={gold!r:>12}  answer={answer!r:>14} "
        f"source={source:<6} pred={prediction[:52]!r}"
    )

print(f"\n{len(CASES) - failures}/{len(CASES)} extraction cases passed")
sys.exit(1 if failures else 0)
