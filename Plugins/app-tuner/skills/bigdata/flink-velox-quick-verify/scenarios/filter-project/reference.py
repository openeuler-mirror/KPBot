#!/usr/bin/env python3
"""filter-project reference model — ground truth for a predicate filter +
projection + expression arithmetic over a bounded item stream.

The SQL is:
    SELECT id, name, price * qty, qty + 1
    FROM items WHERE price > 100 AND qty >= 2;

The input pins both predicate boundaries: price 100 is NOT taken (strict
>), qty 2 IS taken (>=). price * qty is BIGINT * INT -> BIGINT and
qty + 1 is INT + INT -> INT in Flink; both print as plain integers, so the
model formats them with str(int(...)).

Contract: expected(input_dir) -> list[str] of print-sink rows "+I[...]" in
Flink print format; "python3 reference.py expected <input_dir>" prints the
same rows as a JSON array.
"""
import csv
import json
import os
import sys


def expected(input_dir):
    """Final print rows for the item stream under <input_dir>/items."""
    rows = []
    path = os.path.join(input_dir, 'items', 'items.csv')
    with open(path, encoding='utf-8', newline='') as fh:
        for rec in csv.reader(fh):
            if not rec:
                continue
            bid, name, price, qty = rec[0], rec[1], int(rec[2]), int(rec[3])
            if price > 100 and qty >= 2:
                rows.append('+I[%s, %s, %d, %d]'
                            % (bid, name, price * qty, qty + 1))
    return sorted(rows)


def main(argv):
    if len(argv) != 3 or argv[1] != 'expected':
        sys.exit('usage: %s expected <input_dir>' % os.path.basename(argv[0]))
    print(json.dumps(expected(argv[2])))


if __name__ == '__main__':
    main(sys.argv)
