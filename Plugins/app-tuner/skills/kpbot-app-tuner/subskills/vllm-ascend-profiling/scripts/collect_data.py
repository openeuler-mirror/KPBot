#!/usr/bin/env python3
"""Archive raw profiling output into a run directory with a manifest."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--src",
        default=os.environ.get("VLLM_TORCH_PROFILER_DIR"),
        help="raw profiler output dir (default: $VLLM_TORCH_PROFILER_DIR)",
    )
    ap.add_argument(
        "--dest",
        default="profiling_runs",
        help="runs root dir (default: ./profiling_runs)",
    )
    ap.add_argument("--run-id", default="", help="run id (default: timestamp)")
    ap.add_argument("--move", action="store_true", help="move instead of copy raw files")
    ap.add_argument("--api-base", default="", help="recorded into manifest if set")
    ap.add_argument("--window", type=float, default=3.0, help="collection window seconds, recorded")
    ap.add_argument(
        "--extra-meta-json",
        default="{}",
        help='extra manifest metadata, e.g. \'{"max_iterations": 100, "model": "qwen"}\'',
    )
    args = ap.parse_args()

    if not args.src:
        print(
            json.dumps({"error": "--src or VLLM_TORCH_PROFILER_DIR is required"}),
            file=sys.stderr,
        )
        return 2

    src = Path(args.src).expanduser().resolve()
    if not src.is_dir():
        print(json.dumps({"error": f"src is not a directory: {src}"}), file=sys.stderr)
        return 2

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.dest).expanduser().resolve() / run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    total_bytes = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        dst = raw_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if args.move:
            shutil.move(str(p), str(dst))
        else:
            shutil.copy2(str(p), str(dst))
        size = dst.stat().st_size
        total_bytes += size
        copied.append(str(rel))

    try:
        extra = json.loads(args.extra_meta_json) if args.extra_meta_json else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid --extra-meta-json: {e}"}), file=sys.stderr)
        return 2

    manifest = {
        "run_id": run_id,
        "created_unix": time.time(),
        "src": str(src),
        "raw_dir": str(raw_dir),
        "file_count": len(copied),
        "total_bytes": total_bytes,
        "files": copied,
        "window_seconds": args.window,
        "api_base": args.api_base,
        "env_snapshot": {
            k: os.environ.get(k, "")
            for k in (
                "VLLM_TORCH_PROFILER_DIR",
                "VLLM_TORCH_PROFILER_START_STEP",
                "VLLM_TORCH_PROFILER_END_STEP",
                "ASCEND_HOME_PATH",
            )
        },
        "extra": extra,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps({"run_dir": str(run_dir), "raw_dir": str(raw_dir), "files": copied}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
