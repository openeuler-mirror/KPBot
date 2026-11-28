#!/usr/bin/env python3
"""agg-changelog reference model — ground truth for a GROUP BY aggregation
(count / sum) over a bounded event stream.

The SQL is (user_name — "user" is a reserved word in the Flink SQL parser
and cannot be a column name, a fact pinned on the real cluster):

    SELECT user_name, COUNT(*), SUM(amount) FROM events GROUP BY user_name;

The live job emits the full changelog: as later events for the same key
arrive, the print sink sees -U[old]/+U[new] retractions and updates. This
model implements the folding semantics directly: the final result set has
one row per key — COUNT(*) over the key's events and SUM(amount) over the
key's events. The executor folds the captured changelog the same way
(+I/+U add the carried row, -U/-D withdraw it), so a stack that loses or
corrupts retractions, or aggregates wrongly, shows up as a row diff.

COUNT(*) is BIGINT and SUM(BIGINT) is BIGINT in Flink; both print as plain
integers.

Contract: expected(input_dir) -> list[str] of print-sink rows "+I[...]" in
Flink print format; "python3 reference.py expected <input_dir>" prints the
same rows as a JSON array.
"""
import csv
import json
import os
import sys


def expected(input_dir):
    """Final print rows for the event stream under <input_dir>/events."""
    counts, totals = {}, {}
    path = os.path.join(input_dir, 'events', 'events.csv')
    with open(path, encoding='utf-8', newline='') as fh:
        for rec in csv.reader(fh):
            if not rec:
                continue
            user, amount = rec[0], int(rec[1])
            counts[user] = counts.get(user, 0) + 1
            totals[user] = totals.get(user, 0) + amount
    rows = ['+I[%s, %d, %d]' % (u, counts[u], totals[u]) for u in counts]
    return sorted(rows)


def main(argv):
    if len(argv) != 3 or argv[1] != 'expected':
        sys.exit('usage: %s expected <input_dir>' % os.path.basename(argv[0]))
    print(json.dumps(expected(argv[2])))


if __name__ == '__main__':
    main(sys.argv)
