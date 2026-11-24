"""Check ordered prefix semantics against a SQLite reference window."""

import json
from pathlib import Path
import sqlite3


def prefix(rows):
    running = peak = current = None
    result = []
    for item, day, amount in rows:
        if current != item:
            running = peak = None
            current = item
        if amount is not None:
            running = amount if running is None else running + amount
            peak = running if peak is None else max(peak, running)
        result.append((item, day, running, peak))
    return result


rows = [(1, 1, 100), (1, 2, None), (1, 3, -70), (2, 1, None), (2, 2, 7)]


def stream_batches(batches):
    current = object()
    running = peak = None
    output = []
    for batch in batches:
        for item,day,amount in batch:
            if item != current:
                current,running,peak = item,None,None
            if amount is not None:
                running = amount if running is None else running+amount
                peak = running if peak is None else max(peak,running)
            output.append((item,day,running,peak))
    return output


def route(order_known, partition_valid, frame, function_supported,
          bounds=("UNBOUNDED PRECEDING","CURRENT ROW")):
    return "stream" if (order_known is True and partition_valid is True
                        and frame == "ROWS" and bounds == ("UNBOUNDED PRECEDING","CURRENT ROW")
                        and function_supported is True) else "sort"


def additional_checks():
    values = rows + [(3,1,None),(3,2,None)]
    batches = [values[:1],values[1:3],values[3:4],values[4:]]
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE sales(item INTEGER,day INTEGER,cents INTEGER)")
        db.executemany("INSERT INTO sales VALUES(?,?,?)",values)
        expected = db.execute("""
          WITH sums AS (SELECT item,day,sum(cents) OVER(
            PARTITION BY item ORDER BY day ROWS UNBOUNDED PRECEDING) AS cume FROM sales)
          SELECT item,day,cume,max(cume) OVER(
            PARTITION BY item ORDER BY day ROWS UNBOUNDED PRECEDING) FROM sums ORDER BY item,day
        """).fetchall()
        db.execute("DELETE FROM sales")
        db.executemany("INSERT INTO sales VALUES(?,?,?)",[(1,1,10),(1,1,20),(1,2,-5)])
        peers_rows = db.execute("""SELECT sum(cents) OVER(ORDER BY day ROWS UNBOUNDED PRECEDING)
                                    FROM sales ORDER BY rowid""").fetchall()
        peers_range = db.execute("""SELECT sum(cents) OVER(ORDER BY day RANGE UNBOUNDED PRECEDING)
                                     FROM sales ORDER BY rowid""").fetchall()
    streamed = stream_batches(batches)
    wrong_reset = [row for batch in batches for row in prefix(batch)]
    descending_dates = [(1,2,20),(1,1,10)]
    only_item_sorted = sorted(descending_dates,key=lambda row:row[0])
    expected_ascending = [(1,1,10,10),(1,2,30,30)]
    assert route(True,True,"ROWS",True)=="stream"
    return {
        "cross_batch_state_retained": streamed == expected and wrong_reset != expected,
        "all_null_partition_retained": streamed[-2:] == [(3,1,None,None),(3,2,None,None)],
        "peer_frame_difference_detected": peers_rows == [(10,),(30,),(25,)]
            and peers_range == [(30,),(30,),(25,)] and peers_rows != peers_range,
        "unknown_order_falls_back": route(None,True,"ROWS",True)=="sort",
        "range_frame_falls_back": route(True,True,"RANGE",True)=="sort",
        "invalid_partition_falls_back": route(True,False,"ROWS",True)=="sort",
        "unsupported_function_falls_back": route(True,True,"ROWS",False)=="sort",
        "unsupported_bounds_falls_back": route(True,True,"ROWS",True,
            bounds=("1 PRECEDING","CURRENT ROW"))=="sort",
        "item_sort_does_not_prove_date_order": sorted(prefix(only_item_sorted))!=expected_ascending
            and prefix(sorted(descending_dates,key=lambda row:(row[0],row[1])))==expected_ascending,
    }


with sqlite3.connect(":memory:") as db:
    db.execute("CREATE TABLE sales(item INTEGER, day INTEGER, cents INTEGER)")
    db.executemany("INSERT INTO sales VALUES (?, ?, ?)", rows)
    expected = db.execute("""
        WITH running AS (
            SELECT item, day, sum(cents) OVER (
                PARTITION BY item ORDER BY day ROWS UNBOUNDED PRECEDING
            ) AS cume FROM sales
        )
        SELECT item, day, cume, max(cume) OVER (
            PARTITION BY item ORDER BY day ROWS UNBOUNDED PRECEDING
        ) AS peak FROM running ORDER BY item, day
    """).fetchall()

streamed = prefix(rows)
checks = {
    "prefix_sum_may_decrease": (1, 3, 30, 100) in expected,
    "running_max_retained": streamed == expected and (1, 2, 100, 100) in streamed,
    "partition_state_resets": streamed[-2:] == [(2, 1, None, None), (2, 2, 7, 7)],
    "unordered_input_changes_result": sorted(prefix([rows[2], *rows[:2], *rows[3:]])) != expected,
}
checks.update(additional_checks())
result = {"success": all(checks.values()), **checks}
target = Path(__file__).resolve().parent / "results" / "result.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result))
raise SystemExit(0 if result["success"] else 1)
