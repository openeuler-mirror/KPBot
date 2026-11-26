"""原生指标为完成一批查询的总耗时（毫秒），越小越好。"""

from statistics import median
from time import perf_counter_ns

from candidate import lookup


def measure():
    values = list(range(0, 8192, 2))
    queries = [(i * 97) % 9000 for i in range(512)]
    expected = [key < 8192 and key % 2 == 0 for key in queries]
    correct = lookup(values, queries) == expected  # 预热
    samples_ms = []
    for _ in range(5):
        start = perf_counter_ns()
        actual = lookup(values, queries)
        samples_ms.append((perf_counter_ns() - start) / 1_000_000)
        correct = correct and actual == expected
    return {"correct": correct, "samples_ms": samples_ms, "elapsed_ms": median(samples_ms)}
