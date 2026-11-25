"""Exercise the Q51 alignment and Top N contracts on synthetic integer cents."""

import json
from pathlib import Path
import sqlite3


JOIN = """
SELECT coalesce(w.item_sk, s.item_sk) AS item_sk,
       coalesce(w.sale_date, s.sale_date) AS sale_date,
       w.cume_sales AS web_sales, s.cume_sales AS store_sales
FROM web_rows AS w FULL OUTER JOIN store_rows AS s
  ON w.item_sk = s.item_sk AND w.sale_date = s.sale_date
"""

PIVOT = """
SELECT item_sk, sale_date,
       max(web_sales) AS web_sales, max(store_sales) AS store_sales
FROM (
  SELECT item_sk, sale_date, cume_sales AS web_sales, NULL AS store_sales FROM web_rows
  UNION ALL
  SELECT item_sk, sale_date, NULL, cume_sales FROM store_rows
)
GROUP BY item_sk, sale_date
"""

WINDOWS = """
SELECT *, max(web_sales) OVER prefix AS web_cumulative,
          max(store_sales) OVER prefix AS store_cumulative
FROM aligned
WINDOW prefix AS (
  PARTITION BY item_sk ORDER BY sale_date
  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
)
"""


def prepare(db):
    db.executescript("""
        CREATE TABLE dates(date_sk INTEGER, sale_date TEXT, month_seq INTEGER);
        INSERT INTO dates VALUES
          (0, '1999-12-31', 1211), (1, '2000-01-01', 1212),
          (2, '2000-01-02', 1212), (3, '2000-01-03', 1212),
          (4, '2000-12-31', 1223), (5, '2001-01-01', 1224);
        CREATE TABLE web_fact(item_sk INTEGER, sold_date_sk INTEGER, cents INTEGER);
        CREATE TABLE store_fact(item_sk INTEGER, sold_date_sk INTEGER, cents INTEGER);
        INSERT INTO web_fact VALUES
          (1, 1, 60), (1, 1, 40), (1, 3, -70), (1, 4, NULL),
          (2, 1, NULL), (2, 2, 5), (3, 4, 20),
          (1, 0, 900), (1, 5, 900), (2, 99, 900), (NULL, 1, 900);
        INSERT INTO store_fact VALUES
          (1, 2, 60), (1, 4, 20), (2, 1, 2), (2, 2, 1), (4, 4, 15);
    """)
    for channel in ("web", "store"):
        db.execute(f"""
            CREATE TABLE {channel}_rows AS
            WITH daily AS (
              SELECT f.item_sk, d.sale_date, sum(f.cents) AS daily_sales
              FROM {channel}_fact AS f JOIN dates AS d ON f.sold_date_sk = d.date_sk
              WHERE d.month_seq BETWEEN 1212 AND 1223 AND f.item_sk IS NOT NULL
              GROUP BY f.item_sk, d.sale_date
            )
            SELECT item_sk, sale_date, sum(daily_sales) OVER (
              PARTITION BY item_sk ORDER BY sale_date
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS cume_sales FROM daily
        """)


def alignment_has_unique_nonnull_keys(db):
    for name in ("web_rows", "store_rows"):
        invalid = db.execute(f"""
            SELECT 1 FROM {name} GROUP BY item_sk, sale_date
            HAVING item_sk IS NULL OR sale_date IS NULL OR count(*) > 1 LIMIT 1
        """).fetchone()
        if invalid:
            return False
    return True


def result_rows(db, alignment, *, qualifying_only=False, limit=None):
    predicate = "WHERE web_cumulative > store_cumulative" if qualifying_only else ""
    tail = "LIMIT ?" if limit is not None else ""
    params = (limit,) if limit is not None else ()
    return db.execute(f"""
        WITH aligned AS ({alignment}), windowed AS ({WINDOWS})
        SELECT * FROM windowed {predicate} ORDER BY item_sk, sale_date {tail}
    """, params).fetchall()


def alignment_rejects(db, web_key, store_key):
    db.execute("SAVEPOINT invalid_alignment")
    try:
        db.execute("INSERT INTO web_rows VALUES (?, ?, ?)", web_key)
        db.execute("INSERT INTO store_rows VALUES (?, ?, ?)", store_key)
        join_count = db.execute(f"SELECT count(*) FROM ({JOIN})").fetchone()[0]
        pivot_count = db.execute(f"SELECT count(*) FROM ({PIVOT})").fetchone()[0]
        return not alignment_has_unique_nonnull_keys(db) and join_count != pivot_count
    finally:
        db.execute("ROLLBACK TO invalid_alignment")
        db.execute("RELEASE invalid_alignment")


def run_case(db):
    prepare(db)
    full = result_rows(db, JOIN)
    ordered = result_rows(db, JOIN, qualifying_only=True, limit=100)
    expected = [
        (1, "2000-01-02", None, 60, 100, 60),
        (1, "2000-01-03", 30, None, 100, 60),
        (1, "2000-12-31", 30, 80, 100, 80),
        (2, "2000-01-02", 5, 3, 5, 3),
    ]
    return {
        "ordered_rows_equal": (
            alignment_has_unique_nonnull_keys(db) and full == result_rows(db, PIVOT)
            and ordered == expected == result_rows(db, PIVOT, qualifying_only=True, limit=100)
        ),
        "rejects_nullable_keys": alignment_rejects(db, (5, None, 3), (5, None, 4)),
        "rejects_duplicate_keys": alignment_rejects(
            db, (1, "2000-12-31", 45), (1, "2000-12-31", 90)
        ),
        "covers_negative_and_null_sales": (
            (1, "2000-01-03", 30, None, 100, 60) in full
            and (2, "2000-01-01", None, 2, None, 2) in full
            and (3, "2000-12-31", 20, None, 20, None) in full
            and (4, "2000-12-31", None, 15, None, 15) in full
        ),
    }


def main():
    if sqlite3.sqlite_version_info < (3, 39, 0):
        raise SystemExit("SQLite 3.39 or newer is required for FULL OUTER JOIN")
    with sqlite3.connect(":memory:") as db:
        checks = run_case(db)
    checks.update(boundary_checks())
    result = {"success": all(checks.values()), **checks}
    target = Path(__file__).resolve().parent / "results" / "result.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["success"] else 1


def guarded_alignment(db, allow_sql=True, types_safe=True):
    if not allow_sql or not types_safe or not alignment_has_unique_nonnull_keys(db):
        return JOIN, "original"
    return PIVOT, "pivot"


def boundary_checks():
    from collections import Counter
    with sqlite3.connect(":memory:") as db:
        prepare(db)
        _, unauthorized = guarded_alignment(db,allow_sql=False)
        _, unsupported = guarded_alignment(db,types_safe=False)
        db.execute("DELETE FROM web_rows")
        db.execute("DELETE FROM store_rows")
        both_empty = result_rows(db,JOIN) == result_rows(db,PIVOT) == []
        db.execute("INSERT INTO web_rows VALUES (5,'2000-01-01',NULL)")
        one_side = result_rows(db,JOIN) == result_rows(db,PIVOT) == [
            (5,'2000-01-01',None,None,None,None)]
        db.execute("INSERT INTO store_rows VALUES (5,'2000-01-01',NULL)")
        all_null = result_rows(db,JOIN) == result_rows(db,PIVOT) == [
            (5,'2000-01-01',None,None,None,None)]
        db.execute("DELETE FROM web_rows")
        db.execute("DELETE FROM store_rows")
        duplicate_guards = []
        fallback_keeps_rows = []
        for side in ('web','store'):
            db.execute("DELETE FROM web_rows")
            db.execute("DELETE FROM store_rows")
            other = 'store' if side=='web' else 'web'
            db.executemany(f"INSERT INTO {side}_rows VALUES (?,?,?)",
                           [(5,'2000-01-01',1),(5,'2000-01-01',2)])
            db.execute(f"INSERT INTO {other}_rows VALUES (5,'2000-01-01',3)")
            selected,route = guarded_alignment(db)
            original = result_rows(db,JOIN)
            duplicate_guards.append(route=='original' and len(original)==2
                                    and len(result_rows(db,PIVOT))==1)
            fallback_keeps_rows.append(Counter(result_rows(db,selected))==Counter(original))
    return {
        "empty_sides_equivalent": both_empty,
        "one_sided_null_amount_retained": one_side,
        "matched_all_null_amount_retained": all_null,
        "rejects_web_only_duplicates": duplicate_guards[0],
        "rejects_store_only_duplicates": duplicate_guards[1],
        "fallback_preserves_multiplicity": all(fallback_keeps_rows),
        "unauthorized_rewrite_falls_back": unauthorized=='original',
        "type_guard_falls_back": unsupported=='original',
    }


if __name__ == "__main__":
    raise SystemExit(main())
