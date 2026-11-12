#!/usr/bin/env python3
"""Generate a self-contained HTML report from trace_view.json (+ optional metrics)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


def ts_num(e: dict[str, Any]) -> float:
    try:
        return float(e.get("ts", 0))
    except (TypeError, ValueError):
        return 0.0


def build_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    cats = Counter(e.get("cat") or "hardware/other" for e in events)
    phases = Counter(e.get("ph") for e in events)
    proc_names: dict[str, str] = {}
    proc_labels: dict[str, str] = {}
    for e in events:
        if e.get("ph") == "M":
            args = e.get("args") or {}
            pid = str(e.get("pid"))
            if e.get("name") == "process_name":
                proc_names[pid] = args.get("name", "")
            elif e.get("name") == "process_labels":
                proc_labels[pid] = args.get("labels", "")

    span_us = 0.0
    t0 = 0.0
    timed = [e for e in events if e.get("ph") != "M" and ts_num(e) != 0]
    if timed:
        t0 = min(ts_num(e) for e in timed)
        t1 = max(ts_num(e) for e in timed)
        span_us = t1 - t0

    node_launch = [e for e in events if e.get("name") == "Node@launch"]
    unique_kernels = {
        (e.get("args") or {}).get("item_id", "?") for e in node_launch
    }

    return {
        "event_count": len(events),
        "span_ms": span_us / 1000.0,
        "categories": dict(cats.most_common()),
        "phases": dict(phases),
        "processes": [
            {
                "pid": pid,
                "name": proc_names.get(pid, ""),
                "label": proc_labels.get(pid, ""),
            }
            for pid in sorted(proc_names, key=lambda x: int(x) if x.isdigit() else x)
        ],
        "node_launch_count": len(node_launch),
        "unique_kernel_count": len(unique_kernels),
    }


def build_top_kernels(events: list[dict[str, Any]], top_n: int) -> dict[str, Any]:
    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "total_dur": 0.0, "max_dur": 0.0})
    for e in events:
        if e.get("name") != "Node@launch" or e.get("ph") != "X":
            continue
        kid = (e.get("args") or {}).get("item_id", "?")
        dur = float(e.get("dur") or 0.0)
        s = stats[kid]
        s["count"] += 1
        s["total_dur"] += dur
        s["max_dur"] = max(s["max_dur"], dur)

    rows = sorted(
        (
            {
                "kernel": k,
                "count": int(v["count"]),
                "total_dur_us": v["total_dur"],
                "avg_dur_us": v["total_dur"] / v["count"] if v["count"] else 0.0,
                "max_dur_us": v["max_dur"],
            }
            for k, v in stats.items()
        ),
        key=lambda r: r["total_dur_us"],
        reverse=True,
    )
    total_kernel_time = sum(r["total_dur_us"] for r in rows)
    for r in rows:
        r["pct_of_kernel_time"] = (
            100.0 * r["total_dur_us"] / total_kernel_time if total_kernel_time else 0.0
        )

    task_types = Counter()
    task_dur = Counter()
    for e in events:
        if e.get("ph") != "X":
            continue
        a = e.get("args") or {}
        tt = a.get("task type") or a.get("Task Type")
        if tt:
            task_types[tt] += 1
            task_dur[tt] += float(e.get("dur") or 0.0)

    return {
        "top": rows[:top_n],
        "all_count": len(rows),
        "total_kernel_time_us": total_kernel_time,
        "task_types": [
            {"type": t, "count": task_types[t], "total_dur_us": task_dur[t]}
            for t, _ in task_types.most_common(20)
        ],
    }


def build_overlap(events: list[dict[str, Any]]) -> dict[str, Any]:
    proc_names: dict[str, str] = {}
    for e in events:
        if e.get("ph") == "M" and e.get("name") == "process_name":
            proc_names[str(e.get("pid"))] = (e.get("args") or {}).get("name", "")
    overlap_pids = {pid for pid, name in proc_names.items() if name == "Overlap Analysis"}

    total = Counter()
    count = Counter()
    timeline: list[dict[str, Any]] = []
    for e in events:
        if e.get("ph") != "X" or str(e.get("pid")) not in overlap_pids:
            continue
        name = e.get("name", "?")
        dur = float(e.get("dur") or 0.0)
        total[name] += dur
        count[name] += 1
        timeline.append({"track": name, "ts": ts_num(e), "dur": dur})

    grand = sum(total.values()) or 1.0
    tracks = [
        {
            "track": k,
            "count": count[k],
            "total_us": total[k],
            "pct": 100.0 * total[k] / grand,
        }
        for k in ("Computing", "Communication", "Communication(Not Overlapped)", "Free")
        if k in total
    ]
    # any other tracks
    known = {t["track"] for t in tracks}
    for k, v in total.items():
        if k not in known:
            tracks.append(
                {"track": k, "count": count[k], "total_us": v, "pct": 100.0 * v / grand}
            )
    timeline.sort(key=lambda x: x["ts"])
    # compact + downsample for embedding into the HTML payload
    compact = [
        {"track": s["track"], "ts": round(s["ts"], 1), "dur": round(s["dur"], 1)}
        for s in timeline
    ]
    if len(compact) > 6000:
        step = len(compact) / 6000.0
        compact = [compact[int(i * step)] for i in range(6000)]
    return {"tracks": tracks, "total_us": grand, "timeline": compact}


def build_memory_proxy(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Memory activity proxy: Memcpy / empty_tensor / H2D-D2H events over time."""
    buckets: dict[str, Counter] = defaultdict(Counter)
    totals = Counter()
    kinds = {
        "Memcpy": ("Memcpy", "MEMCPY_ASYNC"),
        "empty_tensor": ("empty_tensor",),
        "H2D/D2H": ("acl_memcpy_host_to_device", "acl_memcpy_device_to_host", "MEMCPY_ASYNC"),
    }

    timed = [e for e in events if e.get("ph") != "M" and ts_num(e) != 0]
    if timed:
        t0 = min(ts_num(e) for e in timed)
        t1 = max(ts_num(e) for e in timed)
    else:
        t0 = t1 = 0.0
    span = max(t1 - t0, 1.0)
    # bucket width in true ms, capped at <=720 buckets so clock-skew-inflated
    # spans can't explode the embedded memory series (JS reads bucket_ms as ms)
    bucket_ms = round(max(0.05, (span / 1000.0) / 720.0), 3)
    n_buckets = max(1, int((span / 1000.0) / bucket_ms) + 1)

    samples: dict[str, list[float]] = {k: [0.0] * n_buckets for k in kinds}
    sizes_h2d = 0
    count = Counter()

    for e in events:
        if e.get("ph") != "X":
            continue
        name = e.get("name", "")
        dur = float(e.get("dur") or 0.0)
        idx = min(n_buckets - 1, int((ts_num(e) - t0) / 1000.0 / bucket_ms))
        if name in kinds["Memcpy"]:
            samples["Memcpy"][idx] += dur
            totals["Memcpy"] += dur
            count["Memcpy"] += 1
        elif name in kinds["empty_tensor"]:
            samples["empty_tensor"][idx] += 1.0
            totals["empty_tensor"] += 1.0
            count["empty_tensor"] += 1
        elif name in kinds["H2D/D2H"]:
            samples["H2D/D2H"][idx] += dur
            totals["H2D/D2H"] += dur
            count["H2D/D2H"] += 1
            a = e.get("args") or {}
            if a.get("size(Byte)"):
                sizes_h2d += int(a["size(Byte)"])

    return {
        "bucket_ms": bucket_ms,
        "series": [
            {"name": k, "points": samples[k], "unit": "count" if k == "empty_tensor" else "us"}
            for k in kinds
        ],
        "totals": {k: totals[k] for k in kinds},
        "counts": dict(count),
        "h2d_d2h_bytes": sizes_h2d,
        "note": "Proxy derived from Memcpy / empty_tensor / H2D-D2H events (no HBM counter in trace).",
    }


def build_comm(events: list[dict[str, Any]]) -> dict[str, Any]:
    sizes = []
    bws = []
    types = Counter()
    bytes_by_type: Counter = Counter()
    for e in events:
        if e.get("ph") != "X":
            continue
        a = e.get("args") or {}
        if "bandwidth(GB/s)" not in a and "size(Byte)" not in a:
            continue
        tt = a.get("task type", "?")
        types[tt] += 1
        if a.get("size(Byte)"):
            sizes.append(float(a["size(Byte)"]))
            bytes_by_type[tt] += float(a["size(Byte)"])
        if a.get("bandwidth(GB/s)") is not None:
            bws.append(float(a["bandwidth(GB/s)"]))
    return {
        "event_count": len(sizes),
        "total_bytes": sum(sizes),
        "avg_bandwidth_gbs": (sum(bws) / len(bws)) if bws else 0.0,
        "max_bandwidth_gbs": max(bws) if bws else 0.0,
        "by_type": [
            {"type": t, "count": types[t], "bytes": bytes_by_type[t]}
            for t, _ in types.most_common()
        ],
    }


def load_json(path: str | None) -> Any:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def completeness_sibling(tp: Path) -> str:
    """trace_view.rank3.json -> completeness_report.rank3.json next to it."""
    m = re.search(r"(rank\d+)\.json$", tp.name)
    name = f"completeness_report.{m.group(1)}.json" if m else "completeness_report.json"
    return str(tp.parent / name)


def is_loadable_rank_name(name: str) -> bool:
    """加载口径唯一权威：rank\\d+.json（发现口径 glob 星号可空，更宽）。"""
    return re.search(r"rank(\d+)\.json$", name) is not None


def build_machine_context(
    label: str, metrics_path: str, msprof_path: str, rank_runs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Machine-level data for the report's top-level section (multi-rank machines)."""
    metrics = load_json(metrics_path) or {}
    msprof = load_json(msprof_path) or {}
    steps = msprof.get("step_trace_time") or []
    ttft_breakdown = metrics.get("ttft_breakdown") or None
    return {
        "label": label,
        "ranks": len(rank_runs),
        "event_count": sum(r["summary"]["event_count"] for r in rank_runs),
        "span_ms": max((r["summary"]["span_ms"] for r in rank_runs), default=0.0),
        "ttft": metrics.get("ttft_ms"),
        "ttft_breakdown": ttft_breakdown,
        "metrics": metrics,
        "msprof": msprof,
        "step_display": steps[:200],
        "step_total": len(steps),
        "chart": {
            "ttft_comp": ttft_breakdown["composition"] if ttft_breakdown else [],
            "comm_stack": [
                {k: r[k] for k in ("op", "wait_ms", "sync_ms", "idle_ms", "transit_ms")}
                for r in (msprof.get("communication", {}).get("ops") or [])[:12]
            ],
        },
    }


def build_run_context(
    trace_path: Path,
    metrics_path: str | None,
    completeness_path: str | None,
    msprof_path: str | None,
    label: str,
    top_n: int,
) -> dict[str, Any]:
    events = json.loads(trace_path.read_text(encoding="utf-8"))
    if not isinstance(events, list):
        raise ValueError(f"trace_view must be a JSON array: {trace_path}")

    metrics = load_json(metrics_path) or {}
    completeness = load_json(completeness_path) or {}
    msprof = load_json(msprof_path) or {}

    summary = build_summary(events)
    top = build_top_kernels(events, top_n)
    top_source = "trace:Node@launch"
    if msprof.get("op_statistic"):
        ops = msprof["op_statistic"]
        top["top"] = [
            {
                "kernel": r["op_type"],
                "count": int(r["count"]),
                "total_dur_us": r["total_us"],
                "avg_dur_us": r.get("avg_us", 0.0),
                "max_dur_us": r.get("max_us", 0.0),
                "pct_of_kernel_time": r.get("pct", 0.0),
            }
            for r in ops[:top_n]
        ]
        top["all_count"] = len(ops)
        top["total_kernel_time_us"] = sum(r["total_us"] for r in ops)
        top_source = "op_statistic.csv"
    overlap = build_overlap(events)
    mem = build_memory_proxy(events)
    comm = build_comm(events)

    ttft = metrics.get("ttft_ms")
    ttft_breakdown = metrics.get("ttft_breakdown") or None

    steps = msprof.get("step_trace_time") or []
    return {
        "label": label,
        "summary": summary,
        "top": top,
        "top_source": top_source,
        "overlap": overlap,
        "mem": mem,
        "comm": comm,
        "metrics": metrics,
        "ttft": ttft,
        "ttft_breakdown": ttft_breakdown,
        "completeness": completeness,
        "msprof": msprof,
        "source": str(trace_path),
        "step_display": steps[:200],
        "step_total": len(steps),
        "chart": {
            "top_kernels": top["top"],
            "task_types": [t for t in top["task_types"] if t["count"] > 0][:12],
            "overlap": overlap["tracks"],
            "overlap_timeline": overlap["timeline"],
            "memory": mem,
            "ttft_comp": ttft_breakdown["composition"] if ttft_breakdown else [],
            "comm_stack": [
                {k: r[k] for k in ("op", "wait_ms", "sync_ms", "idle_ms", "transit_ms")}
                for r in (msprof.get("communication", {}).get("ops") or [])[:12]
            ],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--runs",
        nargs="+",
        default=[],
        metavar="RUN_DIR",
        help="one or more run dirs, each containing trace_view.json "
        "(+ optional metrics.json / completeness_report.json); multi-machine tabbed report",
    )
    ap.add_argument("--labels", nargs="+", default=[], help="machine labels for --runs (default: dir basenames)")
    ap.add_argument("--trace-view", default="trace_view.json", help="legacy single-run mode: trace_view.json path")
    ap.add_argument("--metrics", default="", help="legacy single-run mode: optional metrics.json")
    ap.add_argument("--completeness", default="", help="legacy single-run mode: optional completeness_report.json")
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--title", default="vllm-ascend Profiling Report")
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument(
        "--template-dir",
        default=str(Path(__file__).resolve().parent.parent / "templates"),
    )
    args = ap.parse_args()

    # 意见3.0-1：specs 携带点名 trace 文件本身（而非仅其父目录），
    # --trace-view 指定的文件必须参与数据加载，不再允许静默换文件。
    specs: list[tuple[Path, Path | None, str, str, str, str]] = []  # (run dir, 点名 trace 文件或 None, metrics, msprof, label, completeness)
    if args.runs:
        labels = args.labels if args.labels else [Path(r).name or f"run{i}" for i, r in enumerate(args.runs)]
        if len(labels) != len(args.runs):
            print(json.dumps({"error": "--labels count must match --runs count"}), file=sys.stderr)
            return 2
        for r, lab in zip(args.runs, labels):
            d = Path(r).expanduser().resolve()
            specs.append((d, None, str(d / "metrics.json"), str(d / "msprof_summary.json"), lab, ""))
    else:
        tv = Path(args.trace_view).expanduser().resolve()
        specs.append(
            (
                tv.parent,
                tv,
                args.metrics,
                str(tv.parent / "msprof_summary.json"),
                "run",
                args.completeness or completeness_sibling(tv),
            )
        )

    # a run dir is valid with trace_view.json (single-rank / legacy merged) OR
    # trace_view.rank<N>.json (multi-rank, no merged output); discovery and
    # loading share the same rank regex (意见3.0-1a'：口径统一，伪命中文件
    # 如 trace_view.rank.json 不再“可发现、不可加载”）；legacy single-run
    # args keep requiring the --trace-view file itself
    if args.runs:
        invalid = [
            str(d)
            for d, *_ in specs
            if not (d / "trace_view.json").is_file()
            and not any(is_loadable_rank_name(p.name) for p in d.glob("trace_view.rank*.json"))
        ]
    else:
        invalid = [] if Path(args.trace_view).expanduser().resolve().is_file() else [args.trace_view]
    if invalid:
        print(
            json.dumps({"error": "no trace_view.json / trace_view.rank*.json found", "missing": invalid}),
            file=sys.stderr,
        )
        return 2

    runs: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []

    def _add_run(kind: str, g: dict[str, Any], rctx: dict[str, Any]) -> None:
        rctx["idx"] = len(runs)
        rctx["kind"] = kind
        runs.append(rctx)
        g["panes"].append(rctx)

    try:
        for d, tf, mp, msp, lab, cp_override in specs:
            if tf is not None and tf.name != "trace_view.json" and not is_loadable_rank_name(tf.name):
                # 意见3.0-1a：非标准命名的 trace 文件点名直连——用户点名什么
                # 就加载什么（单 merged/head 页），绝不静默换用同目录的
                # trace_view.json / rank 文件
                run = build_run_context(tf, mp, cp_override, msp, tf.stem, args.top_n)
                g: dict[str, Any] = {"idx": len(groups), "label": lab, "machine_ctx": None, "panes": []}
                _add_run("head", g, run)
                groups.append(g)
                continue
            tp = d / "trace_view.json"
            has_merged = tp.is_file()
            g = {"idx": len(groups), "label": lab, "machine_ctx": None, "panes": []}
            if has_merged:
                # single-rank machine (or legacy merged data): the whole-machine
                # pane is the group's fixed head content (case A/B)
                run = build_run_context(tp, mp, cp_override or completeness_sibling(tp), msp, lab, args.top_n)
                _add_run("head", g, run)
            rank_runs: list[dict[str, Any]] = []
            for rp in sorted(d.glob("trace_view.rank*.json")):
                if not is_loadable_rank_name(rp.name):
                    continue
                m = re.search(r"rank(\d+)\.json$", rp.name)
                rctx = build_run_context(rp, mp, completeness_sibling(rp), msp, f"rank{m.group(1)}", args.top_n)
                _add_run("rank", g, rctx)
                rank_runs.append(rctx)
            if not has_merged and rank_runs:
                # multi-rank machine: rank panes + machine-level data section
                g["machine_ctx"] = build_machine_context(lab, mp, msp, rank_runs)
            for i, r in enumerate(rank_runs):
                r["tab_hidden"] = len(rank_runs) > 1 and i > 0
            groups.append(g)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2

    # 意见3.0-1b：runs 兜底守卫——任何路径下空 runs 不得再流向渲染，
    # 杜绝非捕获的 UndefinedError（输出 JSON 错误并以 2 退出）
    if not runs:
        print(
            json.dumps({"error": "no loadable trace_view file(s), produced 0 runs", "specs": [str(s[0]) for s in specs]}),
            file=sys.stderr,
        )
        return 2

    machine_ctxs = [g["machine_ctx"] for g in groups if g["machine_ctx"]]

    chart_data = {
        "colors": {
            "Computing": "#2ca02c",
            "Communication": "#ff7f0e",
            "Communication(Not Overlapped)": "#d62728",
            "Free": "#9e9e9e",
        },
        "runs": [{"idx": r["idx"], "label": r["label"], **r["chart"]} for r in runs],
        "machines": [
            {
                "idx": g["idx"],
                "label": g["label"],
                "ttft_comp": (g["machine_ctx"] or {}).get("chart", {}).get("ttft_comp", []),
                "comm_stack": (g["machine_ctx"] or {}).get("chart", {}).get("comm_stack", []),
            }
            for g in groups
        ],
    }

    env = Environment(
        loader=FileSystemLoader(args.template_dir),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("report.html.j2")
    vendor_path = Path(args.template_dir) / "vendor" / "echarts.min.js"
    if not vendor_path.is_file():
        print(json.dumps({"error": f"echarts vendor missing: {vendor_path}"}), file=sys.stderr)
        return 2
    echarts_js = vendor_path.read_text(encoding="utf-8")
    html = template.render(
        title=args.title,
        runs=runs,
        groups=groups,
        machine_count=len(groups),
        chart_data=chart_data,
        multi=len(runs) > 1,
        echarts_js=echarts_js,
    )

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out),
                "bytes": out.stat().st_size,
                "machines": [
                    {
                        "label": m["label"],
                        "ranks": m["ranks"],
                        "event_count": m["event_count"],
                        "span_ms": m["span_ms"],
                        "ttft_ms": m["ttft"],
                        "ttft_segments": [x["name"] for x in m["chart"]["ttft_comp"]],
                    }
                    for m in machine_ctxs
                ],
                "panes": [
                    {
                        "label": r["label"],
                        "kind": r["kind"],
                        "source": r["source"],
                        "summary": {k: r["summary"][k] for k in ("event_count", "span_ms", "unique_kernel_count")},
                        "top_source": r["top_source"],
                        "ttft_ms": r["ttft"],
                    }
                    for r in runs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
