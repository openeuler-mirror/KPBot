#!/usr/bin/env python3
"""two-way-join reference model — ground truth for a regular inner join
between two bounded streams with many events per key.

Flink's regular join keeps both sides keyed and, for insert-only streams,
emits one +I row per (left, right) pair that ever shares a key — no
retractions, no tie-breaking between multiple events of the same key. The
final result set is therefore exactly the set-theoretic inner join: every
(order, shipment) pair with o.key = s.key, duplicates included. Keys
present on only one side (orders 4/5 with key 3, shipment 13 with key 4)
contribute nothing.

The model computes that join directly over the two CSVs.

Contract: expected(input_dir) -> list[str] of print-sink rows "+I[...]" in
Flink print format; "python3 reference.py expected <input_dir>" prints the
same rows as a JSON array.
"""
import csv
import json
import os
import sys


def _rows(path):
    with open(path, encoding='utf-8', newline='') as fh:
        return [rec for rec in csv.reader(fh) if rec]


def expected(input_dir):
    """Final print rows for the join under <input_dir>/{orders,shipments}."""
    orders = _rows(os.path.join(input_dir, 'orders', 'orders.csv'))
    shipments = _rows(os.path.join(input_dir, 'shipments', 'shipments.csv'))
    rows = []
    for o in orders:
        for s in shipments:
            if o[1] == s[1]:  # o.key = s.key
                rows.append('+I[%s, %s, %s]' % (o[1], o[2], s[2]))
    return sorted(rows)


def main(argv):
    if len(argv) != 3 or argv[1] != 'expected':
        sys.exit('usage: %s expected <input_dir>' % os.path.basename(argv[0]))
    print(json.dumps(expected(argv[2])))


if __name__ == '__main__':
    main(sys.argv)
