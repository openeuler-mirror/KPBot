#!/usr/bin/env python3
"""changelog_diff.py — reduce two changelog captures to their final result
sets and compare them exactly.

Usage: changelog_diff.py <native-out> <gfv-out>

Input: files of print-connector rows, one per line, in the form
    +I[a, b, c]   -U[...]   +U[...]   -D[...]
Lines that do not start with a changelog flag are ignored (log noise).
A line that starts with a flag but never terminates (a truncated
"+I[broken") is a capture error, not noise: silently dropping it would
drop real rows from the comparison.

Method: multiset fold — +I/+U add the payload row, -U/-D remove it
(retraction rows carry the exact old value). Row order is not compared.
See references/changelog-semantics.md for why this is exact.

Exit codes (a tool error and a capture error are never verdicts, and
never exit 1 — 1 means MISMATCH and only MISMATCH):
  0  exact match
  1  any difference (result-set difference, fold error, or an empty
     capture — an empty capture is a difference, not a match: a side
     whose capture stayed empty must never compare equal to anything,
     including another empty capture)
  2  tool error (missing or unreadable input) — one line to stderr
  3  capture error (a line starting with a changelog flag that cannot
     be parsed in full — the capture is truncated or corrupt; reported
     with line number and raw text; not a match)
Report goes to stdout in a paste-into-TEST_REPORT friendly form.
"""
import collections
import re
import sys

ROW = re.compile(r'^([-+][IUD])\[(.*)\]$')
FLAG_START = re.compile(r'^[-+][IUD]\[')


def fold(path):
    """Return (final multiset, stats, fold errors, capture errors)."""
    final = collections.Counter()
    stats = collections.Counter()
    errors = []
    capture_errors = []
    with open(path, encoding='utf-8', errors='replace') as fh:
        for lineno, line in enumerate(fh, 1):
            raw = line.rstrip('\n')
            m = ROW.match(raw)
            if not m:
                # starts like a real changelog row but never terminates:
                # this was once output — the capture is truncated/corrupt
                if FLAG_START.match(raw):
                    capture_errors.append(
                        f'{path}:{lineno}: line starts with a changelog '
                        f'flag but never terminates: {raw!r}')
                continue  # anything else is log noise
            flag, payload = m.groups()
            stats[flag] += 1
            if flag in ('+I', '+U'):
                final[payload] += 1
            else:  # -U, -D withdraw the exact row they carry
                if final[payload] <= 0:
                    errors.append(f'{path}:{lineno}: {flag} withdraws row '
                                  f'never present: [{payload}]')
                    continue  # malformed: never drive the multiset negative
                final[payload] -= 1
                if final[payload] == 0:
                    del final[payload]
    return final, stats, errors, capture_errors


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    a_path, b_path = sys.argv[1:3]
    try:
        a, a_stats, a_err, a_caperr = fold(a_path)
        b, b_stats, b_err, b_caperr = fold(b_path)
    except OSError as exc:
        # a missing/unreadable input is a tool error, not a verdict —
        # exit 1 belongs to MISMATCH alone
        print(f'changelog_diff.py: cannot read capture: {exc}',
              file=sys.stderr)
        return 2

    print(f'native: {sum(a_stats.values())} rows folded -> '
          f'{sum(a.values())} final rows  '
          f'(flags: {", ".join(f"{k}={v}" for k, v in sorted(a_stats.items())) or "none"})')
    print(f'gfv:    {sum(b_stats.values())} rows folded -> '
          f'{sum(b.values())} final rows  '
          f'(flags: {", ".join(f"{k}={v}" for k, v in sorted(b_stats.items())) or "none"})')
    for e in a_err + b_err:
        print(f'FOLD ERROR: {e}')
    for e in a_caperr + b_caperr:
        print(f'CAPTURE ERROR: {e}')
    if a_caperr or b_caperr:
        print('RESULT: CAPTURE ERROR (truncated or unparseable capture '
              'lines above — the capture is incomplete and cannot be '
              'compared; not a match)')
        return 3
    if a_err or b_err:
        print('RESULT: MISMATCH (malformed changelog — fold errors above)')
        return 1

    # Empty capture guard: refuse to compare a side that never produced a
    # single changelog row (run-sql.sh also fails such sides earlier; this
    # keeps the comparator safe when fed captures by hand).
    empty_sides = [side for side, rows in (('native', a_stats),
                                           ('gfv', b_stats))
                   if sum(rows.values()) == 0]
    if empty_sides:
        for side in empty_sides:
            print(f'  empty capture on the {side} side — '
                  'no changelog rows at all')
        print('RESULT: MISMATCH (empty capture — refusing to compare; '
              'confirm that side actually reached FINISHED and produced '
              'output)')
        return 1

    missing = a - b  # in native, not in gfv
    extra = b - a    # in gfv, not in native
    if not missing and not extra:
        print(f'RESULT: MATCH ({sum(a.values())} rows, exact)')
        return 0

    print('RESULT: MISMATCH')
    for row, n in sorted(missing.items()):
        print(f'  missing in gfv  (x{n}): [{row}]')
    for row, n in sorted(extra.items()):
        print(f'  extra in gfv    (x{n}): [{row}]')
    print('  note: multiset compare — duplicate counts count; '
          'field formatting differences are real mismatches')
    return 1


if __name__ == '__main__':
    sys.exit(main())
