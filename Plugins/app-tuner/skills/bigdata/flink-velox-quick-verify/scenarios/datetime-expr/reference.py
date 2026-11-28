#!/usr/bin/env python3
"""datetime-expr reference model — ground truth for the 10 time-extraction
functions (YEAR, MONTH, DAYOFMONTH, DAYOFYEAR, DAYOFWEEK, QUARTER, HOUR,
MINUTE, SECOND, WEEK) projected over the bounded bid stream.

Semantics this model pins (see README.md for the full anchors):

- Timezone convention. The CSV's dt column carries UTC wall clocks written
  offset-free in SQL format ("yyyy-MM-dd HH:mm:ss.SSS") — the only form
  Flink 1.19's CSV TIMESTAMP(3) parser accepts (LocalDateTime / SQL format,
  no offset; converting the GlutenFlink e2e dump's "…Z" ISO form was required before
  native Flink could read it). Flink's TIMESTAMP (without time zone) parse
  and its time-extraction both go through the session timezone, so the two
  conversions cancel and the extracted components equal the wall-clock
  components as written. This model therefore parses the timestamps as
  naive local datetimes and extracts components directly — no offset is
  applied. A stack whose parse and extract disagree about the session
  timezone (the GlutenFlink history: velox's InitSessionTimezone defaulted to UTC
  while the Flink side ran -Duser.timezone=Asia/Shanghai) breaks that
  round-trip invariance and shows up here as a column diff — which is
  exactly what this scenario exists to catch.

- DAYOFWEEK uses the Flink/SQL-standard convention Sun=1..Sat=7. The GlutenFlink
  e2e exposed velox's sparksql DayOfWeekFunction returning raw tm_wday
  (Sun=0..Sat=6): every Wednesday printed 3 where native Flink printed 4.

- WEEK is the ISO 8601 week (week 1 contains the year's first Thursday;
  early-January dates may belong to the previous ISO year's last week) —
  Python's date.isocalendar() returns exactly this.

- QUARTER is (month - 1) // 3 + 1.

Contract: expected(input_dir) -> list[str] of print-sink rows "+I[...]" in
Flink print format; "python3 reference.py expected <input_dir>" prints the
same rows as a JSON array.
"""
import csv
import json
import os
import sys
from datetime import datetime


def _extract(dt):
    """The 10 extracted components, in the SQL projection's order."""
    return [
        dt.year,                                   # YEAR
        dt.month,                                  # MONTH
        dt.day,                                    # DAYOFMONTH
        dt.timetuple().tm_yday,                    # DAYOFYEAR
        (dt.weekday() + 1) % 7 + 1,                # DAYOFWEEK, Sun=1..Sat=7
        (dt.month - 1) // 3 + 1,                   # QUARTER
        dt.hour,                                   # HOUR
        dt.minute,                                 # MINUTE
        dt.second,                                 # SECOND
        dt.isocalendar()[1],                      # WEEK, ISO 8601
    ]


def expected(input_dir):
    """Final print rows for the bid stream under <input_dir>/bids."""
    rows = []
    path = os.path.join(input_dir, 'bids', 'bids.csv')
    with open(path, encoding='utf-8', newline='') as fh:
        for rec in csv.reader(fh):
            if not rec:
                continue
            bid, _price, _channel, dt_text = rec[0], rec[1], rec[2], rec[3]
            # naive wall clock, no offset — see the timezone convention above
            dt = datetime.strptime(dt_text, '%Y-%m-%d %H:%M:%S.%f')
            values = [bid] + [str(v) for v in _extract(dt)]
            rows.append('+I[' + ', '.join(values) + ']')
    return sorted(rows)


def main(argv):
    if len(argv) != 3 or argv[1] != 'expected':
        sys.exit('usage: %s expected <input_dir>' % os.path.basename(argv[0]))
    print(json.dumps(expected(argv[2])))


if __name__ == '__main__':
    main(sys.argv)
