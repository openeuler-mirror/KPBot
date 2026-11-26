"""从 EAT 评分入口实测两个正确候选的耗时和排序。"""

import json
import math
from pathlib import Path
import shutil
from statistics import median
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
LINEAR = """def lookup(values, queries):
    return [key in values for key in queries]
"""
INDEXED = """def lookup(values, queries):
    index = set(values)
    return [key in index for key in queries]
"""


def measure_candidate(root, name, source, bash):
    project = root / name
    (project / "eat").mkdir(parents=True)
    shutil.copyfile(HERE / "bench.py", project / "bench.py")
    shutil.copyfile(HERE / "eval.sh", project / "eat/eval.sh")
    (project / "candidate.py").write_text(source, encoding="utf-8")
    run = subprocess.run([bash, "eat/eval.sh"], cwd=project, capture_output=True,
                         text=True, encoding="utf-8", timeout=30)
    if run.returncode:
        raise SystemExit(f"{name}: eval exited {run.returncode}\n{run.stdout}\n{run.stderr}")
    raw, score_line = run.stdout.strip().splitlines()
    report = json.loads(raw)
    if report["correct"] is not True or not score_line.startswith("SCORE: "):
        raise SystemExit(f"{name}: incorrect candidate or missing score\n{run.stdout}")
    elapsed_ms = median(report["samples_ms"])
    score = float(score_line.removeprefix("SCORE: "))
    if (not math.isfinite(elapsed_ms) or elapsed_ms <= 0
            or not math.isclose(report["elapsed_ms"], elapsed_ms)
            or not math.isclose(score, 100.0 / elapsed_ms)):
        raise SystemExit(f"{name}: score disagrees with the raw timings\n{run.stdout}")
    print(f"{name}: correct=true, median_ms={elapsed_ms:.6f}, SCORE={score:.6f}")
    return elapsed_ms, score


def main():
    bash = shutil.which("bash")
    if bash is None:
        sys.exit("Bash is required to run eat/eval.sh")
    with tempfile.TemporaryDirectory(prefix="eat-latency-") as scratch:
        root = Path(scratch)
        linear_ms, linear_score = measure_candidate(root, "linear", LINEAR, bash)
        indexed_ms, indexed_score = measure_candidate(root, "indexed", INDEXED, bash)
    if not (indexed_ms < linear_ms and indexed_score > linear_score):
        sys.exit("Expected the indexed candidate to finish sooner and receive the higher score")


if __name__ == "__main__":
    main()
