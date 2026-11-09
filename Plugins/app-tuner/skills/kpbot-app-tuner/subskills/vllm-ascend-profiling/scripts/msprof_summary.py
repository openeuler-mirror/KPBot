#!/usr/bin/env python3
"""Aggregate msprof summary CSV/JSON files from *_ascend_pt export dirs.

Sources (per rank, inside <rank>_ascend_pt/):
  ASCEND_PROFILER_OUTPUT/op_statistic.csv          — per-OP-type statistics (Top-N)
  ASCEND_PROFILER_OUTPUT/api_statistic.csv         — host API statistics
  ASCEND_PROFILER_OUTPUT/step_trace_time.csv       — per-step overlap breakdown
  ASCEND_PROFILER_OUTPUT/communication.json        — per comm-op wait/sync/idle/transit
  ASCEND_PROFILER_OUTPUT/communication_matrix.json — rank-pair transit size/time/bandwidth
  profiler_info_0.json / profiler_metadata.json    — versions, rank, parallel groups

Multi-rank values are aggregated (op/api/matrix summed, steps averaged) into the
msprof_summary.json sidecar that generate_report.py picks up next to trace_view.json.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _f(v: Any) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def find_ascend_pt_dirs(raw_dir: Path) -> list[Path]:
    return sorted({p.resolve() for p in raw_dir.rglob("*_ascend_pt") if p.is_dir()})


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def parse_op_statistic(path: Path) -> list[dict[str, Any]]:
    rows = []
    for r in _read_csv(path):
        if not r.get("OP Type"):
            continue
        rows.append(
            {
                "op_type": r["OP Type"],
                "core_type": r.get("Core Type", ""),
                "count": _i(r.get("Count")),
                "total_us": _f(r.get("Total Time(us)")),
                "min_us": _f(r.get("Min Time(us)")),
                "max_us": _f(r.get("Max Time(us)")),
            }
        )
    return rows


def parse_api_statistic(path: Path) -> list[dict[str, Any]]:
    rows = []
    for r in _read_csv(path):
        if not r.get("API Name"):
            continue
        rows.append(
            {
                "level": r.get("Level", ""),
                "name": r["API Name"],
                "time_us": _f(r.get("Time(us)")),
                "count": _i(r.get("Count")),
                "min_us": _f(r.get("Min(us)")),
                "max_us": _f(r.get("Max(us)")),
            }
        )
    return rows


def parse_step_trace_time(path: Path) -> list[dict[str, Any]]:
    rows = []
    for r in _read_csv(path):
        if not r.get("Step") and not r.get("Computing"):
            continue
        rows.append(
            {
                "step": _i(r.get("Step")),
                "computing_us": _f(r.get("Computing")),
                "comm_not_overlapped_us": _f(r.get("Communication(Not Overlapped)")),
                "overlapped_us": _f(r.get("Overlapped")),
                "communication_us": _f(r.get("Communication")),
                "free_us": _f(r.get("Free")),
                "stage_us": _f(r.get("Stage")),
                "bubble_us": _f(r.get("Bubble")),
            }
        )
    return rows


def parse_communication(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    ops: dict[str, dict[str, Any]] = {}
    for step in data.values():
        if not isinstance(step, dict):
            continue
        for kind in ("p2p", "collective"):
            for full, d in (step.get(kind) or {}).items():
                if not isinstance(d, dict):
                    continue
                name = str(full).split("@")[0]
                if name == "Total Op Info":  # msprof per-step aggregate pseudo-op
                    continue
                ti = d.get("Communication Time Info") or {}
                a = ops.setdefault(
                    name,
                    {"op": name, "count": 0, "elapse_ms": 0.0, "wait_ms": 0.0,
                     "sync_ms": 0.0, "idle_ms": 0.0, "transit_ms": 0.0},
                )
                a["count"] += 1
                a["elapse_ms"] += _f(ti.get("Elapse Time(ms)"))
                a["wait_ms"] += _f(ti.get("Wait Time(ms)"))
                a["sync_ms"] += _f(ti.get("Synchronization Time(ms)"))
                a["idle_ms"] += _f(ti.get("Idle Time(ms)"))
                a["transit_ms"] += _f(ti.get("Transit Time(ms)"))
    return sorted(ops.values(), key=lambda x: -x["elapse_ms"])


def parse_matrix(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    pairs: dict[str, dict[str, Any]] = {}
    for step in data.values():
        if not isinstance(step, dict):
            continue
        for kind in ("p2p", "collective"):
            for full, prs in (step.get(kind) or {}).items():
                if not isinstance(prs, dict):
                    continue
                for pair, d in prs.items():
                    if not isinstance(d, dict):
                        continue
                    a = pairs.setdefault(
                        str(pair), {"pair": str(pair), "size_mb": 0.0, "time_ms": 0.0, "ops": 0, "transports": set()},
                    )
                    a["size_mb"] += _f(d.get("Transit Size(MB)"))
                    a["time_ms"] += _f(d.get("Transit Time(ms)"))
                    a["ops"] += 1
                    t = str(d.get("Transport Type") or "").strip()
                    if t:
                        a["transports"].add(t)
    rows = []
    for a in pairs.values():
        bw = a["size_mb"] / a["time_ms"] if a["time_ms"] > 0 else 0.0  # MB/ms == GB/s
        rows.append(
            {
                "pair": a["pair"],
                "transport": "+".join(sorted(a["transports"])) or "N/A",
                "size_mb": round(a["size_mb"], 3),
                "time_ms": round(a["time_ms"], 3),
                "bandwidth_gbs": round(bw, 3),
                "ops": a["ops"],
            }
        )
    return sorted(rows, key=lambda x: -x["size_mb"])


def parse_meta(rank_dir: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    info = rank_dir / "profiler_info_0.json"
    if info.is_file():
        try:
            d = json.loads(info.read_text(encoding="utf-8", errors="replace"))
            meta["torch_npu_version"] = d.get("torch_npu_version", "")
            meta["cann_version"] = d.get("cann_version", "")
            meta["profiler_config"] = d.get("config", {}).get("common_config", {})
        except (json.JSONDecodeError, OSError):
            pass
    md = rank_dir / "profiler_metadata.json"
    if md.is_file():
        try:
            d = json.loads(md.read_text(encoding="utf-8", errors="replace"))
            meta["parallel_groups"] = {
                g.get("group_name", k): g.get("global_ranks", [])
                for k, g in (d.get("parallel_group_info") or {}).items()
                if isinstance(g, dict)
            }
            meta["trace_id"] = d.get("trace_id", "")
        except (json.JSONDecodeError, OSError):
            pass
    return meta


def _merge_op_statistic(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in lists:
        for r in rows:
            k = (r["op_type"], r.get("core_type", ""))
            a = agg.setdefault(
                k, {"op_type": r["op_type"], "core_type": r.get("core_type", ""),
                    "count": 0, "total_us": 0.0, "min_us": float("inf"), "max_us": 0.0},
            )
            a["count"] += r["count"]
            a["total_us"] += r["total_us"]
            a["min_us"] = min(a["min_us"], r["min_us"])
            a["max_us"] = max(a["max_us"], r["max_us"])
    out = []
    total_all = sum(a["total_us"] for a in agg.values()) or 1.0
    for a in agg.values():
        if a["min_us"] == float("inf"):
            a["min_us"] = 0.0
        a["avg_us"] = a["total_us"] / a["count"] if a["count"] else 0.0
        a["pct"] = a["total_us"] / total_all * 100.0
        out.append(a)
    return sorted(out, key=lambda x: -x["total_us"])


def _merge_api_statistic(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in lists:
        for r in rows:
            k = (r["level"], r["name"])
            a = agg.setdefault(
                k, {"level": r["level"], "name": r["name"],
                    "time_us": 0.0, "count": 0, "min_us": float("inf"), "max_us": 0.0},
            )
            a["time_us"] += r["time_us"]
            a["count"] += r["count"]
            a["min_us"] = min(a["min_us"], r["min_us"])
            a["max_us"] = max(a["max_us"], r["max_us"])
    out = []
    for a in agg.values():
        if a["min_us"] == float("inf"):
            a["min_us"] = 0.0
        a["avg_us"] = a["time_us"] / a["count"] if a["count"] else 0.0
        out.append(a)
    return sorted(out, key=lambda x: -x["time_us"])


def _merge_steps(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    per_step: dict[int, list[dict[str, Any]]] = {}
    for rows in lists:
        for r in rows:
            per_step.setdefault(r["step"], []).append(r)
    keys = ("computing_us", "comm_not_overlapped_us", "overlapped_us",
            "communication_us", "free_us", "stage_us", "bubble_us")
    out = []
    for step in sorted(per_step):
        rs = per_step[step]
        row = {"step": step}
        for k in keys:
            row[k] = sum(r[k] for r in rs) / len(rs)
        out.append(row)
    return out


def _merge_comm_ops(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for rows in lists:
        for r in rows:
            a = agg.setdefault(
                r["op"], {"op": r["op"], "count": 0, "elapse_ms": 0.0, "wait_ms": 0.0,
                          "sync_ms": 0.0, "idle_ms": 0.0, "transit_ms": 0.0},
            )
            for k in ("count",):
                a[k] += r[k]
            for k in ("elapse_ms", "wait_ms", "sync_ms", "idle_ms", "transit_ms"):
                a[k] += r[k]
    return sorted(agg.values(), key=lambda x: -x["elapse_ms"])


def _merge_matrix(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for rows in lists:
        for r in rows:
            a = agg.setdefault(
                r["pair"], {"pair": r["pair"], "transport": r["transport"], "size_mb": 0.0,
                            "time_ms": 0.0, "ops": 0, "bandwidth_gbs": 0.0},
            )
            a["size_mb"] += r["size_mb"]
            a["time_ms"] += r["time_ms"]
            a["ops"] += r["ops"]
    for a in agg.values():
        a["bandwidth_gbs"] = round(a["size_mb"] / a["time_ms"], 3) if a["time_ms"] > 0 else 0.0
        a["size_mb"] = round(a["size_mb"], 3)
        a["time_ms"] = round(a["time_ms"], 3)
    return sorted(agg.values(), key=lambda x: -x["size_mb"])


def build_summary(raw_dir: Path) -> dict[str, Any] | None:
    """Aggregate msprof summary files under raw_dir; None when nothing found."""
    rank_dirs = find_ascend_pt_dirs(raw_dir)
    if not rank_dirs:
        return None
    op_lists, api_lists, step_lists, comm_lists, matrix_lists = [], [], [], [], []
    meta: dict[str, Any] = {}
    rank_ids: list[Any] = []
    for d in rank_dirs:
        out_dir = d / "ASCEND_PROFILER_OUTPUT"
        if (out_dir / "op_statistic.csv").is_file():
            op_lists.append(parse_op_statistic(out_dir / "op_statistic.csv"))
        if (out_dir / "api_statistic.csv").is_file():
            api_lists.append(parse_api_statistic(out_dir / "api_statistic.csv"))
        if (out_dir / "step_trace_time.csv").is_file():
            step_lists.append(parse_step_trace_time(out_dir / "step_trace_time.csv"))
        if (out_dir / "communication.json").is_file():
            comm_lists.append(parse_communication(out_dir / "communication.json"))
        if (out_dir / "communication_matrix.json").is_file():
            matrix_lists.append(parse_matrix(out_dir / "communication_matrix.json"))
        if not meta:
            meta = parse_meta(d)
    if not (op_lists or api_lists or step_lists or comm_lists or matrix_lists):
        return None
    comm_ops = _merge_comm_ops(comm_lists)
    totals = {}
    if comm_ops:
        totals = {
            k: round(sum(r[k] for r in comm_ops), 3)
            for k in ("elapse_ms", "wait_ms", "sync_ms", "idle_ms", "transit_ms")
        }
    return {
        "available": True,
        "ranks": len(rank_dirs),
        "op_statistic": _merge_op_statistic(op_lists),
        "api_statistic": _merge_api_statistic(api_lists),
        "step_trace_time": _merge_steps(step_lists),
        "communication": {"ops": comm_ops, "totals": totals} if comm_ops else {},
        "communication_matrix": _merge_matrix(matrix_lists),
        "meta": meta,
    }
