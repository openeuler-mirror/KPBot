#!/usr/bin/env python3
"""Prepare raw KV-cache data files for OpenZL unitBench scenarios.

Converts a torch .pt tensor (bf16 / float8_e4m3fn / float8_e5m2) into raw
little-endian byte streams, optionally truncates repeat-padding, emits a
quick-verification slice, and prints the exponent distribution statistics
that drive entropy-codec selection (top-k coverage, marginal & pairwise
joint entropy).

Adapted from OpenZL unitBench KV data preparation workflow (BSD-3-Clause).

Usage:
  python3 prepare_kvdata.py <input.pt> [--out-dir DIR] [--dtype auto]
        [--n-true N] [--slice-mb 8] [--no-slice]

Examples:
  # BF16 KV cache with 113,541,120 real elements followed by repeat padding
  python3 prepare_kvdata.py qwen_test_kv.pt --n-true 113541120

  # FP8 E4M3, full tensor, no slice
  python3 prepare_kvdata.py qwen_test_kv_e4m3.pt --dtype e4m3 --no-slice

Output files (per dtype): <stem>.bin (full) and <stem>_8MB.bin (slice).
"""

import argparse
import math
import os

# exponent field extraction per format: (bit_offset, bit_width)
EXP_LAYOUT = {
    "bf16": (7, 8),    # [sign:1][exp:8][frac:7], 2 bytes/elt
    "e4m3": (3, 4),    # [sign:1][exp:4][frac:3], 1 byte/elt
    "e5m2": (2, 5),    # [sign:1][exp:5][frac:2], 1 byte/elt
}
BYTES_PER_ELT = {"bf16": 2, "e4m3": 1, "e5m2": 1}

# histogram chunk length: keeps per-chunk temporary copies bounded (~32MB)
_BINCOUNT_CHUNK = 1 << 22

# minimum torch version for .pt loading: before 2.6 torch.load defaults to
# weights_only=False (full pickle), so the safe behavior would depend on the
# torch version installed on the machine; refuse older releases explicitly
MIN_TORCH = (2, 6)


def _torch_version(torch):
    """(major, minor) from torch.__version__ ('2.6.0+cu124' -> (2, 6))."""
    ver = []
    for part in torch.__version__.split(".")[:2]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        ver.append(int(digits) if digits else 0)
    return tuple(ver)


def load_input(path):
    """Load a pure-tensor .pt through the restricted weights_only unpickler.

    .pt is pickle-based: without weights_only, a crafted file executes
    arbitrary code at load time. weights_only=True restricts deserialization
    to tensors and primitives, which is all this script consumes. torch < 2.6
    is refused because its torch.load defaults to weights_only=False and its
    restricted loader lacks later hardening.
    """
    import torch
    if _torch_version(torch) < MIN_TORCH:
        raise SystemExit(
            f"ERROR: torch {torch.__version__} < {MIN_TORCH[0]}.{MIN_TORCH[1]}: "
            "this script only loads .pt via torch.load(weights_only=True), "
            "whose safe defaults are not guaranteed on older torch. "
            f"Upgrade torch (pip install -U 'torch>={MIN_TORCH[0]}.{MIN_TORCH[1]}') and retry.")
    return torch.load(path, map_location="cpu", weights_only=True)


def detect_dtype(tensor):
    t = str(tensor.dtype)
    if "bfloat16" in t:
        return "bf16"
    if "e4m3" in t:
        return "e4m3"
    if "e5m2" in t:
        return "e5m2"
    raise SystemExit(f"ERROR: unsupported dtype {t}; use --dtype bf16|e4m3|e5m2")


def to_raw_bytes(x, dtype):
    import torch
    if dtype == "bf16":
        return x.to(torch.bfloat16).contiguous().view(torch.int16).numpy().tobytes()
    view_dtype = torch.uint8
    return x.contiguous().view(view_dtype).numpy().tobytes()


def exponent_stream(raw, dtype):
    import numpy as np
    off, width = EXP_LAYOUT[dtype]
    if dtype == "bf16":
        u = np.frombuffer(raw, dtype="<u2")
    else:
        u = np.frombuffer(raw, dtype=np.uint8)
    return ((u >> off) & ((1 << width) - 1)).astype(np.uint16)


def entropy(counts):
    """Shannon entropy in bits from an iterable of symbol counts."""
    counts = [c for c in counts if c > 0]
    total = sum(counts)
    return -sum((c / total) * math.log2(c / total) for c in counts) + 0.0


def _bincount_chunked(values, bins):
    """Histogram of a 1-D integer ndarray without full-width temporary copies.

    np.bincount casts its input to intp internally; feeding the whole array at
    once would materialize an 8-bytes/element copy. Chunking bounds the
    temporary to _BINCOUNT_CHUNK elements regardless of input length.
    """
    import numpy as np
    out = np.zeros(bins, dtype=np.int64)
    for i in range(0, len(values), _BINCOUNT_CHUNK):
        out += np.bincount(values[i:i + _BINCOUNT_CHUNK], minlength=bins)
    return out


def _pair_joint_counts(exp, width):
    """Histogram of consecutive symbol pairs, chunked (pairs never split)."""
    import numpy as np
    bins = 1 << (2 * width)
    out = np.zeros(bins, dtype=np.int64)
    n = len(exp)
    i = 0
    while i < n - 1:
        j = min(i + _BINCOUNT_CHUNK, n - 1)   # pair indices [i, j)
        prev = exp[i:j].astype(np.int64)
        cur = exp[i + 1:j + 1].astype(np.int64)
        out += np.bincount((prev << width) | cur, minlength=bins)
        i = j
    return out


def print_stats(exp, dtype):
    import numpy as np
    n = len(exp)
    _, width = EXP_LAYOUT[dtype]
    print(f"[stats] dtype={dtype} elements={n} exp_bits={width}")
    counts = _bincount_chunked(exp, 1 << width)
    n_symbols = int(np.count_nonzero(counts))
    sorted_counts = np.sort(counts)[::-1]
    for k in (8, 16):
        if k <= n_symbols:
            cov = sorted_counts[:k].sum() / n
            print(f"[stats] exp top{k} coverage: {cov:.4f}")
    print(f"[stats] exp marginal entropy: {entropy(counts):.3f} bit")
    if n >= 2:
        jp = entropy(_pair_joint_counts(exp, width))
        print(f"[stats] exp pairwise joint entropy: {jp:.3f} bit/pair "
              f"({jp / 2:.3f} bit/elt; token-pair codecs approach this bound)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="torch .pt tensor file")
    ap.add_argument("--out-dir", default=".", help="output directory (default .)")
    ap.add_argument("--dtype", choices=["bf16", "e4m3", "e5m2"],
                    default="auto", help="override dtype detection")
    ap.add_argument("--n-true", type=int, default=0,
                    help="truncate to first N elements (strip repeat padding); 0 = keep all")
    ap.add_argument("--slice-mb", type=int, default=8,
                    help="quick-verification slice size in MB of raw bytes (default 8)")
    ap.add_argument("--no-slice", action="store_true", help="skip slice emission")
    args = ap.parse_args()

    x = load_input(args.input)
    dtype = args.dtype if args.dtype != "auto" else detect_dtype(x)
    x = x.contiguous().view(-1)
    if args.n_true > 0:
        x = x[: args.n_true]
    raw = to_raw_bytes(x, dtype)

    os.makedirs(args.out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.input))[0]
    full_path = os.path.join(args.out_dir, f"{stem}.bin")
    with open(full_path, "wb") as f:
        f.write(raw)
    print(f"[out] {full_path}  ({len(raw) / 1e6:.1f} MB, {len(raw) // BYTES_PER_ELT[dtype]} elements)")

    if not args.no_slice and args.slice_mb > 0:
        slice_bytes = args.slice_mb * 1024 * 1024
        # element-aligned truncation
        slice_bytes = (slice_bytes // BYTES_PER_ELT[dtype]) * BYTES_PER_ELT[dtype]
        slice_path = os.path.join(args.out_dir, f"{stem}_{args.slice_mb}MB.bin")
        with open(slice_path, "wb") as f:
            f.write(raw[:slice_bytes])
        print(f"[out] {slice_path}  ({slice_bytes / 1e6:.1f} MB)")

    print_stats(exponent_stream(raw, dtype), dtype)


if __name__ == "__main__":
    main()
