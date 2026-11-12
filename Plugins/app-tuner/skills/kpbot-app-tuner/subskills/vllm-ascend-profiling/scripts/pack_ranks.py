#!/usr/bin/env python3
"""Pack the transfer bundle for one run: whole per-rank *_ascend_pt archives + TTFT log.

Reads a run dir laid out per Stage 3e (profiling_runs/<batch>/<IP>/ with
raw/*_ascend_pt/ from Stage 2b analyse + ttft_logs.txt from Stage 3c) and
writes the transfer bundle:

  <out>/<batch>/<IP>/
  ├── ttft_logs.txt                    (copied when present)
  └── <rank_dir>_ascend_pt.tar.gz      (one per rank; the WHOLE export dir —
                                         trace_view.json, kernel_details.csv,
                                         op_statistic.csv, communication.json, ...)

Each *_ascend_pt must already contain ASCEND_PROFILER_OUTPUT/trace_view.json
(Stage 2b analyse), otherwise the central side cannot rebuild the report.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--run-dir",
        required=True,
        help="profiling_runs/<batch>/<IP> run dir (raw/ + ttft_logs.txt)",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="bundle root dir; writes <out>/<batch>/<IP>/",
    )
    args = ap.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}), file=sys.stderr)
        return 2
    raw_dir = run_dir / "raw"
    if not raw_dir.is_dir():
        print(json.dumps({"error": f"raw dir not found under run dir: {raw_dir}"}), file=sys.stderr)
        return 2

    rank_dirs = sorted({p.resolve() for p in raw_dir.rglob("*_ascend_pt") if p.is_dir()})
    if not rank_dirs:
        print(
            json.dumps(
                {
                    "error": "no *_ascend_pt dirs under raw/",
                    "fix_hint": "run Stage 2c collect_data.py (and 2b analyse) before packing",
                }
            ),
            file=sys.stderr,
        )
        return 1
    missing = [d.name for d in rank_dirs if not (d / "ASCEND_PROFILER_OUTPUT" / "trace_view.json").is_file()]
    if missing:
        print(
            json.dumps(
                {
                    "error": "ascend_pt dirs without ASCEND_PROFILER_OUTPUT/trace_view.json",
                    "dirs": missing,
                    "fix_hint": "run Stage 2b analyse_trace.py before packing",
                }
            ),
            file=sys.stderr,
        )
        return 1

    batch = run_dir.parent.name
    ip = run_dir.name
    bundle_dir = Path(args.out).expanduser().resolve() / batch / ip
    bundle_dir.mkdir(parents=True, exist_ok=True)

    archives: list[dict[str, Any]] = []
    for d in rank_dirs:
        arc = bundle_dir / f"{d.name}.tar.gz"
        with tarfile.open(arc, "w:gz") as tf:
            tf.add(d, arcname=d.name)
        archives.append({"archive": arc.name, "size_bytes": arc.stat().st_size, "source": str(d)})

    warnings: list[str] = []
    log_src = run_dir / "ttft_logs.txt"
    ttft_log = False
    if log_src.is_file():
        shutil.copy2(log_src, bundle_dir / log_src.name)
        ttft_log = True
    else:
        warnings.append("ttft_logs.txt missing — run Stage 3c parse_metrics.py to produce it")

    print(
        json.dumps(
            {
                "bundle_dir": str(bundle_dir),
                "batch": batch,
                "ip": ip,
                "archives": archives,
                "ttft_log": ttft_log,
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
