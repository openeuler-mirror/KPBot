#!/usr/bin/env python3
"""Rebuild the per-machine report from a transfer bundle (central side).

Bundle layout (from pack_ranks.py):

  <bundle-dir>/                     (= <date>/<IP>/)
  ├── ttft_logs.txt                 (optional)
  └── *_ascend_pt.tar.gz            (one per rank; each contains only
                                      <rank_dir>/ASCEND_PROFILER_OUTPUT/trace_view.json)

Unpacks the rank archives into a temp dir, then runs
parse_trace → check_completeness → parse_metrics(local log) → generate_report.
Multi-rank bundles yield trace_view.rank<N>.json (no merged whole-machine
output) and per-rank completeness reports; the report renders one pane per
rank plus a top-level machine section. Data products and report.html land in
the canonical run dir `profiling_runs/<date>/<IP>/`: when the bundle already
sits there (the recommended transfer target — scp straight into
profiling_runs/), products are rebuilt in place next to the archives;
otherwise they go to `profiling_runs/<date>/<IP>/` relative to CWD. The
unpacked raw copy lives in the temp dir and is removed; the bundle itself is
untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent


def _run_step(name: str, cmd: list[str], ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in ok_codes:
        print(
            json.dumps(
                {"error": f"step {name} failed", "cmd": cmd, "code": proc.returncode,
                 "stderr": (proc.stderr or "")[-1000:]},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(proc.returncode or 1)
    return proc


def _safe_extract(arc: Path, dest: Path) -> None:
    with tarfile.open(arc, "r:gz") as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ValueError(f"unsafe member in {arc.name}: {m.name}")
        tf.extractall(dest)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle-dir", required=True, help="<date>/<IP> dir holding rank archives + ttft_logs.txt")
    ap.add_argument("--out", default="", help="report path (default: run-dir report.html; in-place when the bundle lives under profiling_runs/)")
    ap.add_argument("--title", default="", help="report title (default: vllm-ascend Profiling Report <date> <IP>)")
    args = ap.parse_args()

    bundle = Path(args.bundle_dir).expanduser().resolve()
    if not bundle.is_dir():
        print(json.dumps({"error": f"bundle dir not found: {bundle}"}), file=sys.stderr)
        return 2
    archives = sorted(bundle.glob("*.tar.gz"))
    if not archives:
        print(json.dumps({"error": f"no *.tar.gz archives under {bundle}"}), file=sys.stderr)
        return 1

    label = f"{bundle.parent.name}/{bundle.name}"
    if bundle.parent.parent.name == "profiling_runs":
        run_dir = bundle  # bundle already in its final run dir: rebuild in place
    else:
        run_dir = Path("profiling_runs", bundle.parent.name, bundle.name).resolve()
    out_report = Path(args.out).expanduser().resolve() if args.out else run_dir / "report.html"
    title = args.title or f"vllm-ascend Profiling Report {label}"

    work = Path(tempfile.mkdtemp(prefix="bundle_work_"))
    try:
        unpack = work / "unpack"
        unpack.mkdir()
        for arc in archives:
            try:
                _safe_extract(arc, unpack)
            except (tarfile.TarError, ValueError) as e:
                print(json.dumps({"error": f"bad archive {arc.name}: {e}"}), file=sys.stderr)
                return 1

        run_dir.mkdir(parents=True, exist_ok=True)
        tv_anchor = run_dir / "trace_view.json"
        _run_step(
            "parse_trace",
            [sys.executable, str(SCRIPTS_DIR / "parse_trace.py"),
             "--raw-dir", str(unpack), "--out", str(tv_anchor)],
        )

        # multi-rank bundles: parse_trace emits trace_view.rank<N>.json only
        # (no merged output); completeness is judged per rank. Single-rank
        # bundles keep the merged trace + one completeness report.
        rank_files = sorted(run_dir.glob("trace_view.rank*.json"))
        completeness_overall: Any
        if rank_files:
            per_rank: dict[str, Any] = {}
            for rp in rank_files:
                m = re.search(r"(rank\d+)\.json$", rp.name)
                if not m:
                    continue
                cj = run_dir / f"completeness_report.{m.group(1)}.json"
                _run_step(
                    "check_completeness",
                    [sys.executable, str(SCRIPTS_DIR / "check_completeness.py"),
                     "--trace-view", str(rp), "--out", str(cj)],
                    ok_codes=(0, 1),  # 1 = overall fail: tolerated, surfaced in the summary
                )
                per_rank[m.group(1)] = _load_json(cj).get("overall")
            completeness_overall = per_rank
        else:
            completeness_json = run_dir / "completeness_report.json"
            _run_step(
                "check_completeness",
                [sys.executable, str(SCRIPTS_DIR / "check_completeness.py"),
                 "--trace-view", str(tv_anchor), "--out", str(completeness_json)],
                ok_codes=(0, 1),  # 1 = overall fail: tolerated, surfaced in the summary
            )
            completeness_overall = _load_json(completeness_json).get("overall")

        metrics_json = run_dir / "metrics.json"
        log_src = bundle / "ttft_logs.txt"
        ttft_log = log_src.is_file()
        if ttft_log:
            _run_step(
                "parse_metrics",
                [sys.executable, str(SCRIPTS_DIR / "parse_metrics.py"),
                 "--local-log", str(log_src), "--out", str(metrics_json)],
                ok_codes=(0, 1),  # 1 = no instrumentation lines: tolerated
            )

        metrics_available = _load_json(metrics_json).get("available") if ttft_log else None

        gen_cmd = [sys.executable, str(SCRIPTS_DIR / "generate_report.py")]
        if rank_files:
            gen_cmd += ["--runs", str(run_dir)]
        else:
            gen_cmd += ["--trace-view", str(tv_anchor), "--completeness", str(completeness_json)]
            if ttft_log:
                gen_cmd += ["--metrics", str(metrics_json)]
        gen_cmd += ["--out", str(out_report), "--title", title]
        _run_step("generate_report", gen_cmd)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(
        json.dumps(
            {
                "report": str(out_report),
                "run_dir": str(run_dir),
                "bundle_dir": str(bundle),
                "label": label,
                "ranks": len(archives),
                "ttft_log": ttft_log,
                "metrics_available": metrics_available,
                "completeness_overall": completeness_overall,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
