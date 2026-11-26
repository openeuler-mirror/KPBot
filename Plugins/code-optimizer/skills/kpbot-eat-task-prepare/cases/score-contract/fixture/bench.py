"""既有 benchmark。用 python3 bench.py 从项目根评测 candidate.py 的 locate。

只允许优化 candidate.py。locate 通过 read(index) 访问数组，并返回目标的
位置，未找到时返回 -1。评分用数组读取次数，便于在任意机器上重复验证。
short、long 两个用例等权。正确时，原版和候选读取次数之比取几何平均。
任何一个答案错误或没有读取数据时，整次评测完成并得零分。
"""

import json
import math

from candidate import locate


def measure(name, size):
    values = list(range(1, size * 2, 2))
    key = values[-1]
    reads = 0

    def read(index):
        nonlocal reads
        reads += 1
        return values[index]

    actual = locate(size, key, read)
    return dict(case=name, expected=size - 1, actual=actual,
                baseline_reads=size, candidate_reads=reads)


rows = [measure("short", 4), measure("long", 16)]
correct = all(row["actual"] == row["expected"] and row["candidate_reads"] > 0
              for row in rows)
score = 0
if correct:
    ratios = [row["baseline_reads"] / row["candidate_reads"] for row in rows]
    score = math.sqrt(math.prod(ratios))
print(json.dumps(dict(cases=rows, correct=correct, score=score), indent=2))
