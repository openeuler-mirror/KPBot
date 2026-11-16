#!/usr/bin/env python3
"""Run native Rust DiskANN memory-graph search with deterministic data and exact GT."""

import argparse
import copy
import hashlib
import heapq
import json
import math
import os
import platform
import random
import statistics
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(source):
    """Digest local source/build inputs, excluding generated build output."""
    digest = hashlib.sha256()
    entries = []
    for folder, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "target", ".opencode", ".claude"})
        for name in sorted(names):
            path = Path(folder) / name
            if path.suffix in {".rs", ".toml", ".lock", ".c", ".cpp", ".h", ".sh"}:
                relative = path.relative_to(source).as_posix()
                value = sha256(path)
                entries.append({"path": relative, "sha256": value})
                digest.update((relative + "\0" + value + "\n").encode("utf-8"))
    return {"digest": "sha256:" + digest.hexdigest(), "files": entries,
            "scope": "local rs/toml/lock/c/cpp/h/sh files, excluding generated target"}


def make_data(folder, n, nq, dim, k, seed):
    """Use exactly representable f32 dyadic inputs; compute GT independently in Python."""
    rng = random.Random(seed)
    rows = [[rng.randrange(-4096, 4097) / 4096 for _ in range(dim)]
            for _ in range(n + nq)]
    base, queries = rows[:n], rows[n:]
    for filename, matrix in (("base.fbin", base), ("queries.fbin", queries)):
        with (folder / filename).open("wb") as handle:
            handle.write(struct.pack("<II", len(matrix), dim))
            for row in matrix:
                handle.write(struct.pack(f"<{dim}f", *row))
    with (folder / "truth.ibin").open("wb") as handle:
        handle.write(struct.pack("<II", nq, k))
        for query in queries:
            nearest = heapq.nsmallest(k, (
                (sum((x - y) ** 2 for x, y in zip(row, query)), i)
                for i, row in enumerate(base)
            ))
            handle.write(struct.pack(f"<{k}I", *(i for _, i in nearest)))


def number(value, name, minimum=0, positive=False):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric, not boolean")
    if value < minimum or (positive and value <= minimum):
        raise ValueError(f"{name} is out of range")
    return value


def parse_result(raw, args):
    """Explicit 0.54 native schema; do not recursively guess similarly named metrics."""
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ValueError("expected exactly one native result job")
    result = raw[0].get("results")
    if not isinstance(result, dict) or not isinstance(result.get("search"), dict):
        raise ValueError("missing native results.search")
    rows = result["search"].get("Topk")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("expected exactly one Topk result")
    row = rows[0]
    for name, expected in (("search_n", args.k), ("search_l", args.search_l),
                           ("num_tasks", args.threads)):
        if type(row.get(name)) is not int or row[name] != expected:
            raise ValueError(f"effective parameter mismatch: {name}")
    values = {}
    for name in ("qps", "mean_latencies", "p99_latencies"):
        vector = row.get(name)
        if not isinstance(vector, list) or len(vector) != 1:
            raise ValueError(f"{name} requires one sample per invocation")
        values[name] = number(vector[0], name, positive=name == "qps")
    recall = row.get("recall")
    if not isinstance(recall, dict):
        raise ValueError("missing recall metrics")
    for name, expected in (("num_queries", args.queries), ("recall_k", args.k), ("recall_n", args.k)):
        if type(recall.get(name)) is not int or recall[name] != expected:
            raise ValueError(f"recall definition mismatch: {name}")
    quality = number(recall.get("average"), "recall")
    if quality > 1:
        raise ValueError("native recall must be in [0,1]")
    values.update({"recall": quality, "mean_cmps": number(row.get("mean_cmps"), "mean_cmps"),
                   "mean_hops": number(row.get("mean_hops"), "mean_hops")})
    return values


def execute(command, out, label, timeout, affinity):
    actual = (["taskset", "-c", affinity] if affinity else []) + command
    dump(out / f"{label}.command.json", actual)
    with (out / f"{label}.stdout.txt").open("wb") as stdout, (
        out / f"{label}.stderr.txt"
    ).open("wb") as stderr:
        completed = subprocess.run(actual, stdout=stdout, stderr=stderr, timeout=timeout,
                                   check=False, cwd=out)
    dump(out / f"{label}.exit.json", {"exit_code": completed.returncode})
    if completed.returncode:
        raise ValueError(f"native command failed ({label}, exit={completed.returncode}); see logs")


def input_job(template, args, out, build):
    job = copy.deepcopy(template)
    content = job["content"]
    index_path = str(out / "index" / "memory-index")
    if build:
        source = content["source"]
        source.update({"index-source": "Build", "data_type": "float32",
                       "data": str(out / "base.fbin"), "distance": "squared_l2",
                       "max_degree": args.degree, "l_build": args.build_l,
                       "alpha": 1.2, "backedge_ratio": 1.0, "num_threads": 1,
                       "start_point_strategy": "medoid", "save_path": index_path})
        source.pop("multi_insert", None)
    else:
        content["source"] = {"index-source": "Load", "data_type": "float32",
                             "distance": "squared_l2", "load_path": index_path}
    content["search_phase"] = {
        "search-type": "topk", "queries": str(out / "queries.fbin"),
        "groundtruth": str(out / "truth.ibin"), "reps": 1,
        "num_threads": [args.threads], "runs": [{"search_n": args.k,
            "search_l": [args.search_l], "recall_k": args.k}],
    }
    return {"search_directories": [str(out)], "output_directory": str(out / "index"),
            "jobs": [job]}


def run(args, out):
    source = args.source.resolve()
    required = (source / "Cargo.toml", source / "diskann-benchmark/src/backend/index/result.rs")
    if not all(path.is_file() for path in required):
        raise ValueError("requires Rust DiskANN graph benchmark layout; C++ uses another adapter")
    if source == out or out.is_relative_to(source):
        raise ValueError("output must be outside source to avoid source/result contamination")
    if min(args.vectors, args.queries, args.dim, args.k, args.threads, args.degree) < 1:
        raise ValueError("workload sizes and thread count must be positive")
    if not (args.k <= args.search_l <= args.vectors and args.degree < args.vectors
            and args.build_l >= args.degree):
        raise ValueError("require k<=search_l<=vectors, degree<vectors, build_l>=degree")
    if args.repetitions < 2 or args.warmup < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("require repetitions>=2, warmup>=0 and positive finite timeout")
    number(args.min_recall, "min_recall")
    if args.min_recall > 1:
        raise ValueError("min_recall must be <=1")
    if args.cpus and platform.system() != "Linux":
        raise ValueError("--cpus requires Linux taskset")
    identity = source_identity(source)
    dump(out / "source.json", identity)
    make_data(out, args.vectors, args.queries, args.dim, args.k, args.seed)
    template = json.loads((source / "diskann-benchmark/example/graph-index.json").read_text(
        encoding="utf-8"))["jobs"][0]
    binary = args.binary.resolve() if args.binary else None
    if not args.prepare_only:
        if binary is None or not binary.is_file():
            raise FileNotFoundError("native diskann-benchmark binary required; see performance-case.md")
        execute([str(binary), "--quiet", "inputs", "graph-index-build"], out,
                "capabilities", args.timeout, args.cpus)
        text = (out / "capabilities.stdout.txt").read_text(encoding="utf-8")
        # Native CLI emits a prose header followed by exactly one JSON object.
        start = text.find("{")
        if start == -1:
            raise ValueError("binary does not expose graph-index-build input")
        template = json.loads(text[start:])
    if not isinstance(template, dict) or template.get("type") != "graph-index-build":
        raise ValueError("unsupported graph input schema")
    if not isinstance(template.get("content"), dict) or not isinstance(template["content"].get("source"), dict):
        raise ValueError("unsupported graph source schema")
    (out / "index").mkdir()
    build_input = input_job(template, args, out, True)
    load_input = input_job(template, args, out, False)
    dump(out / "build-input.json", build_input)
    dump(out / "search-input.json", load_input)
    manifest = {
        "case": "rust-memory-search", "source": str(source), "source_identity": "source.json",
        "binary": str(binary) if binary else None,
        "binary_sha256": sha256(binary) if binary and binary.is_file() else None,
        "created_at": datetime.now(timezone.utc).isoformat(), "parameters": vars(args) | {
            "source": str(source), "binary": str(binary) if binary else None, "output": str(out)},
        "data_sha256": {name: sha256(out / name) for name in ("base.fbin", "queries.fbin", "truth.ibin")},
        "environment": {"platform": platform.platform(), "machine": platform.machine(),
                        "cpu_count": os.cpu_count(), "cpus": args.cpus or "inherited/unbound",
                        "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None},
        "policy": "one saved index; each process loads it; warmup invocations excluded; all valid samples retained",
        "qps_formula": "query count / native search end-to-end seconds; excludes index loading and build",
        "units": {"recall": "fraction", "mean_latencies": "us", "p99_latencies": "us"},
        "scope": "synthetic memory graph baseline only; no SSD/PQ/cache claims; no candidate speedup",
    }
    dump(out / "manifest.json", manifest)
    if args.prepare_only:
        return {"success": False, "status": "PREPARED", "performance_executed": False,
                "reason": "data and configuration prepared; no benchmark executed"}
    binary_hash = manifest["binary_sha256"]
    execute([str(binary), "--quiet", "run", "--input-file", str(out / "build-input.json"),
             "--output-file", str(out / "build-native.json")], out, "build", args.timeout, args.cpus)
    parse_result(json.loads((out / "build-native.json").read_text(encoding="utf-8")), args)
    index_files = sorted(p for p in (out / "index").rglob("*") if p.is_file())
    if not index_files:
        raise ValueError("native build did not save index files")
    index_hashes = {str(p.relative_to(out)): sha256(p) for p in index_files}
    manifest["index_sha256"] = index_hashes
    dump(out / "manifest.json", manifest)
    samples = []
    for i in range(args.warmup + args.repetitions):
        label = f"search-{i:03d}"
        raw_path = out / f"{label}.native.json"
        execute([str(binary), "--quiet", "run", "--input-file", str(out / "search-input.json"),
                 "--output-file", str(raw_path)], out, label, args.timeout, args.cpus)
        sample = parse_result(json.loads(raw_path.read_text(encoding="utf-8")), args)
        sample.update({"raw": raw_path.name, "warmup": i < args.warmup})
        samples.append(sample)
        dump(out / "samples.json", samples)
        if sample["recall"] < args.min_recall:
            raise ValueError(f"Recall below declared threshold: {sample['recall']} < {args.min_recall}")
    if sha256(binary) != binary_hash or index_hashes != {
        str(p.relative_to(out)): sha256(p)
        for p in sorted((out / "index").rglob("*")) if p.is_file()
    }:
        raise ValueError("binary or saved index changed during measurement")
    if manifest["data_sha256"] != {name: sha256(out / name) for name in manifest["data_sha256"]}:
        raise ValueError("benchmark inputs changed during measurement")
    if source_identity(source)["digest"] != identity["digest"]:
        raise ValueError("source changed during measurement")
    measured = [s for s in samples if not s["warmup"]]
    qps = [s["qps"] for s in measured]
    median = statistics.median(qps)
    return {"success": True, "status": "PASS", "performance_executed": True,
            "case": "rust-memory-search", "samples": len(measured), "qps_median": median,
            "qps_min": min(qps), "qps_max": max(qps),
            "qps_mad": statistics.median(abs(x - median) for x in qps),
            "mean_latency_us_median": statistics.median(s["mean_latencies"] for s in measured),
            "p99_latency_us_median": statistics.median(s["p99_latencies"] for s in measured),
            "recall_min": min(s["recall"] for s in measured),
            "min_recall_required": args.min_recall, "scope": manifest["scope"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--skip-if-unavailable", action="store_true")
    for name, default in (("vectors", 4096), ("queries", 256), ("dim", 32), ("k", 10),
                          ("search-l", 100), ("build-l", 100), ("degree", 32), ("seed", 1729),
                          ("threads", 1), ("warmup", 1), ("repetitions", 4)):
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--cpus", help="Linux taskset CPU list; default inherits current affinity")
    parser.add_argument("--min-recall", type=float, default=0.95)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    out = args.output.resolve()
    try:
        out.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(f"ERROR: output must be a new writable directory: {exc}", file=sys.stderr)
        return 2
    code = 0
    try:
        result = run(args, out)
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        skip = args.skip_if_unavailable and isinstance(exc, FileNotFoundError)
        code = 77 if skip else 2
        result = {"success": False, "status": "SKIP" if skip else "FAIL",
                  "performance_executed": False, "reason": str(exc)}
    dump(out / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
