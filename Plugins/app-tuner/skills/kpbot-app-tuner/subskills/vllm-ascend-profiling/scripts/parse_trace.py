#!/usr/bin/env python3
"""Normalize raw torch_npu chrome traces into per-rank trace_view.rank<N>.json.

Multi-rank input (several *_ascend_pt sources): emits one normalized file per
rank next to --out and NO merged whole-machine trace_view.json — ranks keep
independent timelines (no cross-rank clock alignment) and the report renders
one pane per rank. Single source (or scattered chrome jsons without rank
identity): writes the merged output to --out as before.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Canonical process labels discovered from torch_npu.profiler chrome export.
REQUIRED_KEYS = ("ph", "name", "pid", "tid", "ts")


def _under_ascend_pt(p: Path) -> bool:
    """True when the file sits inside a torch_npu export dir (..._ascend_pt)."""
    return any(part.endswith("_ascend_pt") for part in p.parts)


def find_trace_files(raw_dir: Path, out_path: Path) -> list[Path]:
    pats = ("*.json", "*.pt.trace.json", "*.trace.json")
    found: list[Path] = []
    for pat in pats:
        found.extend(raw_dir.rglob(pat))
    uniq = sorted({p.resolve() for p in found if p.is_file()})
    # never ingest our own output; ignore run side-car files
    uniq = [p for p in uniq if p != out_path.resolve()]
    skip_names = {"manifest.json", "completeness_report.json", "metrics.json", "msprof_summary.json"}
    uniq = [p for p in uniq if p.name not in skip_names]
    uniq = [p for p in uniq if not re.match(r"trace_view\.rank\d+\.json$", p.name)
            and not re.match(r"completeness_report\.rank\d+\.json$", p.name)]
    # primary input: torch_npu analyse() writes trace_view.json INSIDE the
    # collected ..._ascend_pt export dirs — prefer those when present
    analysed = [p for p in uniq if p.name == "trace_view.json" and _under_ascend_pt(p)]
    if analysed:
        return analysed
    return uniq


def load_trace(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # some exporters wrap events
        for key in ("traceEvents", "events", "schemaVersion"):
            if key in data and isinstance(data[key], list):
                return data[key]
        if "traceEvents" in data:
            return data["traceEvents"]
    raise ValueError(f"{path}: unsupported trace shape {type(data).__name__}")


def normalize_event(e: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(e, dict):
        return None
    if "ph" not in e or "name" not in e:
        return None
    # Chrome Trace: metadata (M) events do not carry ts. Keep them (process map).
    if "ts" not in e and e.get("ph") != "M":
        return None
    out: dict[str, Any] = {"ph": e["ph"], "name": e["name"]}
    out["pid"] = e.get("pid", 0)
    out["tid"] = e.get("tid", 0)
    # torch_npu writes ts as string with microsecond fractions; keep string form
    # matching the reference trace_view.json sample.
    if "ts" in e:
        ts = e["ts"]
        out["ts"] = ts if isinstance(ts, str) else _fmt_ts(ts)
    else:
        out["ts"] = 0
    if "dur" in e and e["dur"] is not None:
        out["dur"] = float(e["dur"])
    if e.get("cat") is not None:
        out["cat"] = e["cat"]
    for k in ("id", "bp", "scope", "pid2", "tid2", "cat2"):
        if k in e:
            out[k] = e[k]
    if "args" in e and e["args"] is not None:
        out["args"] = e["args"]
    return out


def _fmt_ts(ts: Any) -> str:
    try:
        return f"{float(ts):.3f}"
    except (TypeError, ValueError):
        return str(ts)


def _ts_num(e: dict[str, Any]) -> float:
    try:
        return float(e.get("ts", 0))
    except (TypeError, ValueError):
        return 0.0


def merge_and_sort(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for e in events:
        n = normalize_event(e)
        if n is not None:
            norm.append(n)
    norm.sort(key=_ts_num)
    return norm


def write_trace_view(events: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False)


def _rank_id_of(path: Path) -> str | None:
    """Derive the rank id from an ..._ascend_pt dir name (rankN)."""
    import re

    for part in path.parts:
        m = re.search(r"rank(\d+)", part)
        if m and part.endswith("_ascend_pt"):
            return m.group(1)
    return None


def write_per_rank(files: list[Path], out_path: Path) -> list[Path]:
    """Emit one normalized trace per rank source next to out_path.

    Only when more than one *_ascend_pt source exists. Returns the written
    per-rank files (trace_view.rank<N>.json).
    """
    ranked = [(_rank_id_of(p) or str(i), p) for i, p in enumerate(files)]
    distinct = {rid for rid, _ in ranked}
    if len(distinct) < 2:
        return []
    stem = out_path.stem
    written: list[Path] = []
    for rid, p in sorted(ranked, key=lambda x: (int(x[0]) if x[0].isdigit() else 999, str(x[1]))):
        evs = load_trace(p)
        merged = merge_and_sort(evs)
        if not merged:
            continue
        rp = out_path.parent / f"{stem}.rank{rid}.json"
        write_trace_view(merged, rp)
        written.append(rp)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", required=True, help="directory holding raw profiler json files")
    ap.add_argument("--out", default="trace_view.json", help="output path; multi-rank sources emit trace_view.rank<N>.json next to it instead of a merged file")
    ap.add_argument(
        "--input-file",
        default="",
        help="explicit single input file (skips discovery); may be a chrome trace json",
    )
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if args.input_file:
        files = [Path(args.input_file).expanduser().resolve()]
    else:
        if not raw_dir.is_dir():
            print(json.dumps({"error": f"raw-dir not a directory: {raw_dir}"}), file=sys.stderr)
            return 2
        files = find_trace_files(raw_dir, out_path)

    if not files:
        print(json.dumps({"error": f"no trace json found under {raw_dir}"}), file=sys.stderr)
        return 1

    events: list[dict[str, Any]] = []
    sources: list[str] = []
    errors: list[str] = []
    for fp in files:
        try:
            evs = load_trace(fp)
            events.extend(evs)
            sources.append(str(fp))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fp}: {e}")

    merged = merge_and_sort(events)
    if not merged:
        print(
            json.dumps({"error": "no valid events after normalize", "sources": sources, "load_errors": errors}),
            file=sys.stderr,
        )
        return 1

    rank_files = write_per_rank(
        [p for p in files if _under_ascend_pt(p)], out_path
    )
    if rank_files:
        # multi-rank: per-rank files are the product; no merged whole-machine
        # output (a stale one from an older run would mislead consumers)
        if out_path.exists():
            out_path.unlink()
        out_written: Path | None = None
    else:
        write_trace_view(merged, out_path)
        out_written = out_path
    msprof_sidecar = None
    if not args.input_file:
        import msprof_summary

        summary = msprof_summary.build_summary(raw_dir)
        if summary:
            msprof_sidecar = out_path.parent / "msprof_summary.json"
            msprof_sidecar.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print(
        json.dumps(
            {
                "out": str(out_written) if out_written else None,
                "merged_written": out_written is not None,
                "sources": sources,
                "event_count": len(merged),
                "load_errors": errors,
                "per_rank": [str(p) for p in rank_files],
                "msprof_summary": str(msprof_sidecar) if msprof_sidecar else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
