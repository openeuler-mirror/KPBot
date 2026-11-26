"""在临时副本中复验准备好的 EAT 任务。可选参数为基于 fixture 接入的项目。"""

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
FAST = """def locate(size, key, read):
    for index in reversed(range(size)):
        if read(index) == key:
            return index
    return -1
"""
# 只读一次，碰巧答对 short，在 long 上返回错误位置。
WRONG = """def locate(size, key, read):
    read(0)
    return 3
"""


def run_evaluator(project):
    bash = shutil.which("bash")
    if bash is None:
        raise SystemExit("Bash is required to run eat/eval.sh")
    return subprocess.run(
        [bash, "eat/eval.sh"], cwd=project, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def expect_score(project, label, expected):
    run = run_evaluator(project)
    lines = run.stdout.rstrip().splitlines()
    match = re.fullmatch(r"SCORE:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)",
                         lines[-1]) if lines else None
    if run.returncode != 0 or not match or not math.isclose(float(match[1]), expected):
        raise SystemExit(f"{label}: expected {expected}, exit={run.returncode}\n"
                         f"{run.stdout}\n{run.stderr}")
    print(f"{label}: SCORE: {match[1]}")


def expect_failure(project, label, benchmark_source):
    (project / "bench.py").write_text(benchmark_source, encoding="utf-8")
    run = run_evaluator(project)
    if run.returncode == 0:
        raise SystemExit(f"{label}: expected nonzero exit, got 0\n{run.stdout}\n{run.stderr}")
    if re.search(r"^SCORE:\s*[-\d.]+", f"{run.stdout}\n{run.stderr}", re.MULTILINE):
        raise SystemExit(f"{label}: failed evaluation exposed a score to EAT\n{run.stdout}\n{run.stderr}")
    print(f"{label}: exit {run.returncode}")


def main():
    if len(sys.argv) > 2:
        raise SystemExit("Usage: python3 check.py [prepared-fixture-project]")
    with tempfile.TemporaryDirectory(prefix="eat-score-") as scratch:
        project = Path(scratch) / "task"
        source = Path(sys.argv[1]) if len(sys.argv) == 2 else HERE / "fixture"
        shutil.copytree(source, project, ignore=shutil.ignore_patterns(
            ".git", ".work", "__pycache__"))
        if len(sys.argv) == 1:
            shutil.copytree(HERE / "prepared/eat", project / "eat")
        if not (project / "eat/task.md").is_file():
            raise SystemExit("Missing eat/task.md")
        expect_score(project, "original", 1)
        candidate = project / "candidate.py"
        candidate.write_text(FAST, encoding="utf-8")
        expect_score(project, "correct and faster", 8)
        candidate.write_text(WRONG, encoding="utf-8")
        expect_score(project, "fewer reads but wrong", 0)

        bench = (project / "bench.py").read_text(encoding="utf-8")
        candidate.write_text(FAST, encoding="utf-8")
        good_report = json.loads(subprocess.check_output(
            [sys.executable, "-B", "bench.py"], cwd=project, timeout=30))
        # EAT also sees stderr. A provisional score from a crashing bench must stay in its raw log.
        crash = '\nimport sys\nprint("SCORE: 8", file=sys.stderr)\nraise SystemExit(7)\n'
        expect_failure(project, "benchmark crashed", bench + crash)
        expect_failure(project, "invalid JSON", 'print("invalid JSON")\n')
        # 1e309 is valid JSON syntax but overflows to infinity in the reader.
        rows = json.dumps(good_report["cases"])
        overflow_report = f'{{"cases": {rows}, "correct": true, "score": 1e309}}'
        expect_failure(project, "non-finite score", f"print({overflow_report!r})\n")


if __name__ == "__main__":
    main()
