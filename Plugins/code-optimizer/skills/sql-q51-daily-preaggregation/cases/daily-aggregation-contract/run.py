"""Verify daily grouping before and after a date dimension join."""

import json
from pathlib import Path
import sqlite3


def eligibility(allow_sql, dimension_unique, merge_safe):
    return allow_sql is True and dimension_unique is True and merge_safe is True


def additional_checks():
    baseline = """
      SELECT item_sk, sale_date, sum(cents) FROM sales JOIN dates USING(date_sk)
      WHERE channel=? AND month_seq BETWEEN 1212 AND 1223 AND item_sk IS NOT NULL
      GROUP BY item_sk,sale_date ORDER BY item_sk,sale_date
    """
    candidate = """
      WITH partial AS (
        SELECT item_sk,date_sk,sum(cents) AS cents FROM sales
        WHERE channel=? AND item_sk IS NOT NULL GROUP BY item_sk,date_sk)
      SELECT item_sk,sale_date,sum(cents) FROM partial JOIN dates USING(date_sk)
      WHERE month_seq BETWEEN 1212 AND 1223
      GROUP BY item_sk,sale_date ORDER BY item_sk,sale_date
    """
    def select_query(allow_sql, unique, merge_safe):
        return candidate if eligibility(allow_sql,unique,merge_safe) else baseline
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
          CREATE TABLE dates(date_sk INTEGER,sale_date TEXT,month_seq INTEGER);
          INSERT INTO dates VALUES (0,'1999-12-31',1211),(10,'2000-01-01',1212),
            (11,'2000-01-01',1212),(12,'2000-01-02',1223),(50,'2001-01-01',1224);
          CREATE TABLE sales(channel TEXT,item_sk INTEGER,date_sk INTEGER,cents INTEGER);
          INSERT INTO sales VALUES ('web',1,10,12),('web',1,10,-2),('web',1,11,8),
            ('web',1,12,NULL),('web',1,0,999),('web',1,50,999),('web',3,99,999),
            ('web',NULL,10,999),('store',1,10,3),('store',1,11,-1),('store',2,12,NULL);
        """)
        expected = {
            "web": [(1,"2000-01-01",18),(1,"2000-01-02",None)],
            "store": [(1,"2000-01-01",2),(2,"2000-01-02",None)],
        }
        both = all(db.execute(baseline,(branch,)).fetchall()
                   == db.execute(candidate,(branch,)).fetchall() == rows
                   for branch,rows in expected.items())
        assert eligibility(True,True,True)
        unauthorized = select_query(False,True,True)==baseline and (
            db.execute(select_query(False,True,True),('web',)).fetchall()==expected['web'])
        unsafe_state = select_query(True,True,False)==baseline and (
            db.execute(select_query(True,True,False),('store',)).fetchall()==expected['store'])
        unknown = select_query(True,None,True)==baseline
        db.execute("INSERT INTO dates VALUES (10,'2000-01-01',1212)")
        unique = db.execute("SELECT 1 FROM dates GROUP BY date_sk HAVING count(*)>1").fetchone() is None
        selected = select_query(True,unique,True)
        duplicate_fallback = not unique and selected == baseline and (
            db.execute(selected,('web',)).fetchall() == db.execute(baseline,('web',)).fetchall())

    def bounded_sum(values):
        total = 0
        for value in values:
            total += value
            if not -99 <= total <= 99:
                raise OverflowError("bounded accumulator")
        return total
    try:
        bounded_sum([80,80,-80])
    except OverflowError:
        original_failed = True
    else:
        original_failed = False
    # This is a bounded integer model, not Spark/Velox decimal validation.
    parenthesization = original_failed and bounded_sum([80,bounded_sum([80,-80])]) == 80
    return {
        "both_branches_equivalent": both,
        "filter_boundaries_preserved": both,
        "dimension_guard_fallback": duplicate_fallback and unknown,
        "unauthorized_rewrite_fallback": unauthorized,
        "unsafe_state_fallback": unsafe_state,
        "bounded_sum_parenthesization_differs": parenthesization,
    }


with sqlite3.connect(":memory:") as db:
    db.executescript("""
        CREATE TABLE dates(date_sk INTEGER PRIMARY KEY, sale_date TEXT, month_seq INTEGER);
        INSERT INTO dates VALUES (10, '2000-01-01', 1212), (11, '2000-01-01', 1212),
            (12, '2000-01-02', 1223), (50, '2001-01-01', 1224);
        CREATE TABLE sales(item_sk INTEGER, date_sk INTEGER, cents INTEGER);
        INSERT INTO sales VALUES (1,10,12), (1,10,-2), (1,11,8), (1,12,NULL),
            (1,50,999), (1,99,999), (NULL,10,999), (2,11,5);
    """)
    original = db.execute("""
        SELECT item_sk, sale_date, sum(cents) FROM sales JOIN dates USING (date_sk)
        WHERE month_seq BETWEEN 1212 AND 1223 AND item_sk IS NOT NULL
        GROUP BY item_sk, sale_date ORDER BY item_sk, sale_date
    """).fetchall()
    preaggregated = """
        WITH partial AS (
          SELECT item_sk, date_sk, sum(cents) AS cents FROM sales
          WHERE item_sk IS NOT NULL GROUP BY item_sk, date_sk
        )
        SELECT item_sk, sale_date, {amount} FROM partial JOIN dates USING (date_sk)
        WHERE month_seq BETWEEN 1212 AND 1223 {regroup}
        ORDER BY item_sk, sale_date
    """
    grouped = db.execute(preaggregated.format(amount="sum(cents)",
                                               regroup="GROUP BY item_sk, sale_date")).fetchall()
    ungrouped = db.execute(preaggregated.format(amount="cents", regroup="")).fetchall()

checks = {
    "same_daily_sales": original == grouped == [
        (1, "2000-01-01", 18), (1, "2000-01-02", None), (2, "2000-01-01", 5)
    ],
    "date_alias_requires_regroup": len(ungrouped) > len(original) and ungrouped != original,
    "all_null_stays_null": (1, "2000-01-02", None) in grouped,
}
checks.update(additional_checks())
result = {"success": all(checks.values()), **checks}
target = Path(__file__).resolve().parent / "results" / "result.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result))
raise SystemExit(0 if result["success"] else 1)
