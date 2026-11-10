#!/usr/bin/env python3
"""Size-class audit for allocation-path optimisation.

Given the requested sizes observed at one allocation site, compute how much the
allocator helper and the allocator's size classes inflate the request, and
report bytes per operation and a peak-live estimate.  The output decides
whether ``exact_size_allocation`` is worth generating and how large the win
ceiling is.

The model is deliberately simple and explicit: a generic helper may apply a
minimum size ("floor") to every request, and the allocator rounds the resulting
request up to a size class.  Both steps are reported separately so the audit
does not hide which layer causes the inflation.

Usage
-----
  python3 alloc_size_audit.py --sizes sizes.txt --floor 1024 --op-count 200000
  python3 alloc_size_audit.py --sizes sizes.json --allocator jemalloc --json
  python3 alloc_size_audit.py --sizes sizes.txt --floor 1024 \
      --actual-bytes-per-op 1280.04 --actual-peak-live 5249160
"""

import argparse
import json
import sys
from collections import Counter

# glibc: 16-byte alignment with an 8-byte header, minimum usable 24 -> chunk 32.
GLIBC_MIN = 32
# Requests at or above the mmap threshold (glibc default 128 KiB) are served by
# mmap and come back page-aligned; below it they are ordinary 16-byte chunks.
GLIBC_MMAP_THRESHOLD = 128 * 1024
# jemalloc small size classes up to 8 KiB, then power-of-two-ish large classes.
JEMALLOC_CLASSES = [
    8, 16, 32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448,
    512, 640, 768, 896, 1024, 1280, 1536, 1792, 2048, 2560, 3072, 3584,
    4096, 5120, 6144, 7168, 8192, 10240, 12288, 14336, 16384,
]


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def glibc_chunk(request):
    if request <= 0:
        return GLIBC_MIN
    if request >= GLIBC_MMAP_THRESHOLD:
        return align_up(request + 16, 4096)
    return max(GLIBC_MIN, align_up(request + 8, 16))


def jemalloc_chunk(request):
    if request <= 0:
        return JEMALLOC_CLASSES[0]
    for size in JEMALLOC_CLASSES:
        if request <= size:
            return size
    return align_up(request, 4096)


def size_class(request, allocator):
    if allocator == "jemalloc":
        return jemalloc_chunk(request)
    return glibc_chunk(request)


def load_sizes(path):
    lowered = path.lower()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        if lowered.endswith(".json"):
            payload = json.load(handle)
            if isinstance(payload, dict):
                if "sizes" in payload:
                    payload = payload["sizes"]
                else:
                    return Counter({int(float(k)): int(v) for k, v in payload.items()})
            return Counter(int(float(v)) for v in payload)
        sizes = []
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            sizes.append(int(float(stripped)))
        return Counter(sizes)


def audit(counter, floor, allocator, op_count, actual_bytes_per_op, actual_peak):
    if not counter:
        return {"ok": False, "error": "no sizes"}

    requested_bytes = 0
    effective_bytes = 0
    rows = []
    for request in sorted(counter):
        count = counter[request]
        effective_request = max(request, floor) if floor else request
        chunk = size_class(effective_request, allocator)
        requested_bytes += request * count
        effective_bytes += effective_request * count
        rows.append({
            "requested": request,
            "count": count,
            "effective_request": effective_request,
            "allocator_chunk": chunk,
            "floor_applied": bool(floor and request < floor),
            "inflation": (effective_request / request) if request else None,
        })

    total = sum(counter.values())
    floor_hits = sum(r["count"] for r in rows if r["floor_applied"])
    inflation = (effective_bytes / requested_bytes) if requested_bytes else 0.0
    worst = max(
        (r for r in rows if r["inflation"] is not None),
        key=lambda r: r["inflation"],
        default=None,
    )

    result = {
        "ok": True,
        "allocator": allocator,
        "floor": floor,
        "total_requests": total,
        "requested_bytes": requested_bytes,
        "effective_bytes": effective_bytes,
        "inflation_ratio": inflation,
        "floor_hit_share": floor_hits / total if total else 0.0,
        "compact_bytes": sum(r["requested"] * r["count"] for r in rows),
        "histogram": rows,
        "worst_case": worst,
        "op_count": op_count,
    }

    if op_count:
        result["requested_bytes_per_op"] = requested_bytes / op_count
        result["effective_bytes_per_op"] = effective_bytes / op_count

    if actual_bytes_per_op is not None:
        result["measured_bytes_per_op"] = actual_bytes_per_op
        if result.get("effective_bytes_per_op"):
            result["model_error"] = abs(
                actual_bytes_per_op - result["effective_bytes_per_op"]
            ) / actual_bytes_per_op
    if actual_peak is not None:
        result["measured_peak_live"] = actual_peak

    if floor_hits and inflation >= 1.5:
        result["recommendation"] = "exact_size_allocation"
        result["reason"] = (
            f"{floor_hits}/{total} 次请求被分配下限抬高，整体通胀 {inflation:.2f}x；"
            "这些对象若仅承载元数据，应在调用点按精确尺寸申请"
        )
    elif floor and floor_hits:
        result["recommendation"] = "reject"
        result["reason"] = (
            f"存在下限命中但整体通胀仅 {inflation:.2f}x，收益上界不足"
        )
    else:
        result["recommendation"] = "no_floor_inflation"
        result["reason"] = "未观测到分配下限带来的通胀；若分配成本仍高，应转向次数或抖动类配方"

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", required=True, help="observed requested sizes")
    parser.add_argument("--floor", type=int, default=0,
                        help="minimum size applied by the generic helper (default 0)")
    parser.add_argument("--allocator", default="glibc",
                        choices=["glibc", "jemalloc", "other"])
    parser.add_argument("--op-count", type=int, default=None,
                        help="operations covered by the size sample")
    parser.add_argument("--actual-bytes-per-op", type=float, default=None,
                        help="measured bytes per op for cross-checking the model")
    parser.add_argument("--actual-peak-live", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = audit(
        load_sizes(args.sizes), args.floor, args.allocator,
        args.op_count, args.actual_bytes_per_op, args.actual_peak_live,
    )
    if args.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 1

    print(f"allocator={result['allocator']} floor={result['floor']} "
          f"requests={result['total_requests']}")
    print(f"{'request':>8s} {'count':>8s} {'effective':>10s} {'chunk':>8s} {'infl':>7s}")
    for row in result["histogram"][:12]:
        infl = f"{row['inflation']:.2f}" if row["inflation"] else "-"
        print(f"{row['requested']:>8d} {row['count']:>8d} "
              f"{row['effective_request']:>10d} {row['allocator_chunk']:>8d} {infl:>7s}")
    print(f"inflation={result['inflation_ratio']:.2f}x "
          f"floor_hit_share={result['floor_hit_share']:.2f}")
    if "requested_bytes_per_op" in result:
        print(f"requested_bytes_per_op={result['requested_bytes_per_op']:.2f} "
              f"effective_bytes_per_op={result['effective_bytes_per_op']:.2f}")
    if "model_error" in result:
        print(f"model_error={result['model_error']:.3f} "
              f"measured_bytes_per_op={result['measured_bytes_per_op']:.2f}")
    print(f"recommendation={result['recommendation']} ({result['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
