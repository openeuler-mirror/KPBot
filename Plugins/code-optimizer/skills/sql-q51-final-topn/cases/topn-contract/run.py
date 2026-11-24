"""Check local/global Top N after the Q51 filter on synthetic window outputs."""

import json
from pathlib import Path
import sqlite3
from functools import cmp_to_key


def sort_key(row):
    item, day, *_ = row
    return item, day is not None, day


def qualifies(row):
    web, store = row[2:4]
    return web is not None and store is not None and web > store


rows = [(item, day, 4, 2) for item in range(1, 111) for day in (None, 1, 2)]
rows += [(0, 1, 3, None), (0, 2, 1, 2)]
partitions = [rows[index::3] for index in range(3)]

with sqlite3.connect(":memory:") as db:
    db.execute("CREATE TABLE windowed(item INTEGER, day INTEGER, web INTEGER, store INTEGER)")
    db.executemany("INSERT INTO windowed VALUES (?, ?, ?, ?)", rows)
    expected = db.execute("""
        SELECT * FROM windowed WHERE web > store
        ORDER BY item ASC, day ASC NULLS FIRST LIMIT 100
    """).fetchall()

local = [row for part in partitions for row in sorted(filter(qualifies, part), key=sort_key)[:100]]
merged = sorted(local, key=sort_key)[:100]
early = list(filter(qualifies, sorted(rows, key=sort_key)[:100]))
checks = {
    "local_merge_equivalent": expected == merged and len(merged) == 100,
    "limit_before_filter_changes_result": len(early) == 98 and early != expected,
    "sort_nulls_first_retained": merged[:3] == [(1, None, 4, 2), (1, 1, 4, 2), (1, 2, 4, 2)],
}


def compare_rows(left, right, specification):
    for index,ascending,nulls_first in specification:
        a,b = left[index],right[index]
        if a is None or b is None:
            if a is None and b is None:
                continue
            return (-1 if nulls_first else 1) if a is None else (1 if nulls_first else -1)
        order = (a>b)-(a<b)
        if order:
            return order if ascending else -order
    return 0


def choose(parts, k, offset=0, *, specification=((0,True,True),(1,True,True)), with_ties=False,
           allow_local=True):
    if k<0 or offset<0:
        raise ValueError("negative bound")
    comparator = lambda a,b:compare_rows(a,b,specification)
    key = cmp_to_key(comparator)
    qualified = [list(filter(qualifies,part)) for part in parts]
    if not allow_local or with_ties:
        ordered = sorted([row for part in qualified for row in part],key=key)
        stop = min(offset+k,len(ordered))
        if with_ties and k>0 and stop>offset:
            while stop<len(ordered) and comparator(ordered[stop],ordered[stop-1])==0:
                stop += 1
        return ordered[offset:stop],sum(map(len,qualified)),"full-sort"
    if k==0:
        return [],0,"local"
    candidates = [row for part in qualified for row in sorted(part,key=key)[:k+offset]]
    return sorted(candidates,key=key)[offset:offset+k],len(candidates),"local"


def sqlite_reference(values,k,offset,specification):
    clauses = []
    for index,ascending,nulls_first in specification:
        column = ('item','day')[index]
        clauses.append(column+(' ASC' if ascending else ' DESC')
                       +(' NULLS FIRST' if nulls_first else ' NULLS LAST'))
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE data(item INTEGER,day INTEGER,web INTEGER,store INTEGER)")
        db.executemany("INSERT INTO data VALUES(?,?,?,?)",values)
        return db.execute('SELECT * FROM data WHERE web>store ORDER BY '
                          +','.join(clauses)+' LIMIT ? OFFSET ?',(k,offset)).fetchall()


def additional_checks():
    tested = 0
    mismatches = []
    for size in (0,1,99,100,101,333):
        values = [(i//3,None if i%3==0 else i%3,4,2) for i in range(size)]
        for count in (1,3,7):
            parts = [values[i::count] for i in range(count)]+[[]]
            for ascending in (True,False):
                for nulls_first in (True,False):
                    specification = ((0,True,True),(1,ascending,nulls_first))
                    candidate,work,route = choose(parts,100,specification=specification)
                    reference = sqlite_reference(values,100,0,specification)
                    equivalent = candidate == reference
                    work_ok = work<=len(parts)*100 and route=='local'
                    if not equivalent or not work_ok:
                        mismatches.append({
                            "size":size,"partitions":count,"ascending":ascending,
                            "nulls_first":nulls_first,"equivalent":equivalent,
                            "work_within_bound":work_ok,"candidate_rows":len(candidate),
                            "reference_rows":len(reference),"work":work,"route":route,
                            "candidate_prefix":candidate[:5],"reference_prefix":reference[:5],
                        })
                    tested += 1
    small = [(i,1,4,2) for i in range(1,7)]
    split = [small[:3],small[3:]]
    expected_offset = [(3,1,4,2),(4,1,4,2)]
    correct,_,_ = choose(split,2,2)
    too_few = sorted([row for part in split for row in part[:2]],key=sort_key)[2:4]
    offset_ok = correct == expected_offset == sqlite_reference(small,2,2,((0,True,True),(1,True,True)))
    different_k = all(choose(split,k)[0] == sqlite_reference(small,k,0,((0,True,True),(1,True,True)))
                      for k in (0,1,2,100))
    tied = [[(1,1,4,2)]*3,[(2,1,4,2)]]
    with_ties,_,ties_route = choose(tied,2,with_ties=True)
    ordinary,_,_ = choose(tied,2)
    original,_,fallback = choose(split,2,allow_local=False)
    selected,work,_ = choose(partitions,100)
    qualified_count = sum(1 for row in rows if qualifies(row))
    small_filter = [[(0,1,1,2),(1,1,4,2),(2,1,4,2)]]
    filter_first,_,_ = choose(small_filter,2)
    early_local = list(filter(qualifies,sorted(small_filter[0],key=sort_key)[:2]))
    # Extra payload columns stand for raw channel outputs, not sorting keys.
    payload = [(1,1,4,2,None,7),(2,1,5,3,100,None)]
    payload_selected,_,_ = choose([payload],2)
    checks = {
        "null_and_direction_variants": tested==72 and not mismatches,
        "different_k_equivalent": different_k,
        "offset_requires_extra_candidates": offset_ok and too_few!=expected_offset,
        "with_ties_fallback_preserves_peers": ties_route=='full-sort' and len(with_ties)==3
            and len(ordinary)==2,
        "unknown_local_capability_falls_back": fallback=='full-sort' and original==small[:2],
        "candidate_row_count_correct": qualified_count==330
            and work==300 and selected==expected,
        "local_limit_before_filter_rejected": filter_first==[(1,1,4,2),(2,1,4,2)]
            and early_local!=filter_first,
        "payload_columns_preserved": payload_selected==payload
            and payload_selected[0][4] is None and payload_selected[1][5] is None,
    }
    return checks,{"matrix_cases":tested,"matrix_mismatch_count":len(mismatches),
                   "matrix_mismatches":mismatches,"qualified_rows":qualified_count,"local_candidate_rows":work,
                   "final_rows":len(selected)}


extra,metrics = additional_checks()
checks.update(extra)
result = {"success": all(checks.values()), **checks}
result.update(metrics)
target = Path(__file__).resolve().parent / "results" / "result.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result))
raise SystemExit(0 if result["success"] else 1)
