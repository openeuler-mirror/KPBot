#!/usr/bin/env python3
"""逐位比较基线与 SIMD 的输出。

默认**精确比较**：向量化的第一原则是不改结果，容差会掩盖真正的 bug。浮点归约顺序
无法保持时才用 `--mode tolerance`，且必须在报告里写明理由和误差界。

用法:
    python3 compare_outputs.py --ref out/ref.bin --simd out/simd.bin
    python3 compare_outputs.py --ref out/ref/ --simd out/simd/
    python3 compare_outputs.py --ref a.bin --simd b.bin --mode tolerance \
        --atol 1e-5 --rtol 1e-6 --dtype f32

退出码: 0 一致，1 不一致，2 参数/IO 错误
"""

import argparse
import json
import os
import struct
import sys

DTYPES = {"u8": ("B", 1), "i8": ("b", 1), "u16": ("H", 2), "i16": ("h", 2),
          "u32": ("I", 4), "i32": ("i", 4), "u64": ("Q", 8), "i64": ("q", 8),
          "f32": ("f", 4), "f64": ("d", 8)}


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def compare_exact(a, b, limit=8):
    diffs = []
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            diffs.append({"offset": i, "ref": a[i], "simd": b[i]})
            if len(diffs) >= limit:
                break
    return diffs


def compare_tol(a, b, fmt, size, atol, rtol, limit=8):
    diffs = []
    count = min(len(a), len(b)) // size
    worst = 0.0
    for i in range(count):
        va = struct.unpack_from("<" + fmt, a, i * size)[0]
        vb = struct.unpack_from("<" + fmt, b, i * size)[0]
        err = abs(va - vb)
        bound = atol + rtol * abs(va)
        if err > worst:
            worst = err
        if err > bound:
            diffs.append({"index": i, "ref": va, "simd": vb,
                          "abs_err": err, "bound": bound})
            if len(diffs) >= limit:
                break
    return diffs, worst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--simd", required=True)
    ap.add_argument("--mode", choices=["exact", "tolerance"], default="exact")
    ap.add_argument("--dtype", default="f32", choices=sorted(DTYPES))
    ap.add_argument("--atol", type=float, default=0.0)
    ap.add_argument("--rtol", type=float, default=0.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.ref):
        print(json.dumps({"success": False, "error_message": "ref_not_found",
                          "path": args.ref}, ensure_ascii=False, indent=2))
        return 2
    if not os.path.exists(args.simd):
        print(json.dumps({"success": False, "error_message": "simd_not_found",
                          "path": args.simd}, ensure_ascii=False, indent=2))
        return 2

    pairs = []
    if os.path.isdir(args.ref) and os.path.isdir(args.simd):
        names = sorted(os.listdir(args.ref))
        for name in names:
            rp = os.path.join(args.ref, name)
            sp = os.path.join(args.simd, name)
            if os.path.isfile(rp):
                if not os.path.isfile(sp):
                    print(json.dumps({"success": False,
                                      "error_message": "missing_counterpart",
                                      "file": name}, ensure_ascii=False, indent=2))
                    return 1
                pairs.append((name, rp, sp))
        if not pairs:
            print(json.dumps({"success": False, "error_message": "no_files_in_ref_dir",
                              "path": args.ref}, ensure_ascii=False, indent=2))
            return 2
    else:
        pairs.append((os.path.basename(args.ref), args.ref, args.simd))

    results = []
    all_ok = True
    for name, rp, sp in pairs:
        a, b = read_bytes(rp), read_bytes(sp)
        size_mismatch = len(a) != len(b)
        if args.mode == "exact":
            diffs = compare_exact(a, b)
            worst = None
            ok = not diffs and not size_mismatch
        else:
            fmt, size = DTYPES[args.dtype]
            diffs, worst = compare_tol(a, b, fmt, size, args.atol, args.rtol)
            ok = not diffs and not size_mismatch
        all_ok = all_ok and ok
        results.append({
            "file": name,
            "ok": ok,
            "ref_bytes": len(a),
            "simd_bytes": len(b),
            "size_mismatch": size_mismatch,
            "diff_count_shown": len(diffs),
            "first_diffs": diffs,
            "max_abs_err": worst,
        })

    payload = {
        "success": all_ok,
        "mode": args.mode,
        "tolerance": ({"atol": args.atol, "rtol": args.rtol, "dtype": args.dtype}
                      if args.mode == "tolerance" else None),
        "files": results,
        "verdict": "identical" if all_ok else "different",
        "error_message": "" if all_ok else "outputs_differ",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
