"""Scalar FFI-prefix contract examples; does not execute Lance or SVE."""

import json
from pathlib import Path


def expand(values, lengths, requested, clamp):
    written = []
    if requested < 0 or len(values) != len(lengths):
        return "ERROR", written
    for value, length in zip(values, lengths):
        if length <= 0:
            return "ERROR", written
        remaining = requested - len(written)
        if length > remaining and not clamp:
            return "ERROR", written
        written.extend([value] * min(length, remaining))
        if length > remaining:
            break
    return ("OK" if len(written) == requested else "ERROR"), written


def main():
    # Literal expected states and prefixes for the documented examples.
    cases = [
        ([7, 9], [2, 4], 5, False, "ERROR", [7, 7]),
        ([7, 9], [2, 4], 5, True, "OK", [7, 7, 9, 9, 9]),
        ([7, 9], [2, 4], 6, False, "OK", [7, 7, 9, 9, 9, 9]),
        ([7, 9], [2, 4], 6, True, "OK", [7, 7, 9, 9, 9, 9]),
        ([7, 9], [2, 4], 7, False, "ERROR", [7, 7, 9, 9, 9, 9]),
        ([7, 9], [2, 4], 7, True, "ERROR", [7, 7, 9, 9, 9, 9]),
        ([7], [4], 3, False, "ERROR", []),
        ([7], [4], 3, True, "OK", [7, 7, 7]),
        ([7, 9], [3, 0], 2, True, "OK", [7, 7]),
        ([7, 9], [2, 0], 2, True, "ERROR", [7, 7]),
        ([7, 9], [3, 0], 2, False, "ERROR", []),
        ([7, 9], [2, 0], 2, False, "ERROR", [7, 7]),
    ]
    for values, lengths, requested, clamp, state, prefix in cases:
        actual_state, actual_prefix = expand(values, lengths, requested, clamp)
        assert (actual_state, actual_prefix) == (state, prefix)
        assert len(actual_prefix) <= requested
    result = {
        "success": True,
        "checked_cases": len(cases),
        "clamp_truncated_then_zero": expand([7, 9], [3, 0], 2, True)[0],
        "clamp_exact_then_zero": expand([7, 9], [2, 0], 2, True)[0],
    }
    output = Path(__file__).resolve().parent / "results/result.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
