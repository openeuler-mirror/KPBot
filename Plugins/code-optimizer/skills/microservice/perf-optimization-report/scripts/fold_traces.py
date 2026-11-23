#!/usr/bin/env python3
"""fold_traces.py -- fold `go tool pprof -traces` output into FlameGraph collapsed format.

Input block format (leaf frame first, weight on the first line):
  -----------+-------------------------------------------------------
       460ms   runtime.stealWork
                runtime.findRunnable
                runtime.schedule
  -----------+-------------------------------------------------------
Collapsed output is root-first, so each block is reversed.
Weights are normalized to milliseconds (handles both "NNNms" and "N.NNs").
Inline suffixes "(inline)" are stripped from frame names.

CLI contract:
  python3 fold_traces.py <traces.txt> <out.collapsed>
  exit 0  success (validation summary on stdout)
  exit 1  input parsed but no trace blocks found (no silent success)
  exit 2  usage error; destination is a directory; destination already exists
  (refuse overwrite); input file not found; destination parent directory missing

Stdlib only; read-only on the input; never executes profile data.
"""
import io
import os
import re
import sys

SEP = re.compile(r"^-+")
HEAD = re.compile(r"^\s*([\d.]+)(ms|s)\s+(\S.*?)\s*$")
FRAME = re.compile(r"^\s{12,}(\S.*?)\s*$")
INLINE = re.compile(r"\s+\(inline\)$")


def parse(src):
    out = {}
    stack = []
    weight = 0.0

    def emit():
        nonlocal stack, weight
        if stack and weight > 0:
            key = ";".join(reversed(stack))
            out[key] = out.get(key, 0) + weight
        stack = []
        weight = 0.0

    with io.open(src, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n").rstrip("\r")
            if SEP.match(line):
                emit()
                continue
            m = HEAD.match(line)
            if m:
                emit()  # close any unterminated block
                val, unit, name = float(m.group(1)), m.group(2), m.group(3)
                if unit == "s":
                    val *= 1000.0
                weight = val
                stack = [INLINE.sub("", name)]
                continue
            m = FRAME.match(line)
            if m:
                stack.append(INLINE.sub("", m.group(1)))

    emit()
    return out


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: python3 fold_traces.py <traces.txt> <out.collapsed>\n")
        return 2
    src, dst = argv[1], argv[2]
    if os.path.isdir(dst):
        sys.stderr.write("destination is a directory: %s\n" % dst)
        return 2
    if os.path.exists(dst):
        sys.stderr.write("refusing to overwrite existing file: %s\n" % dst)
        return 2
    if not os.path.isfile(src):
        sys.stderr.write("input file not found: %s\n" % src)
        return 2
    dst_dir = os.path.dirname(os.path.abspath(dst))
    if not os.path.isdir(dst_dir):
        sys.stderr.write("destination directory does not exist: %s\n" % dst_dir)
        return 2

    out = parse(src)
    if not out:
        sys.stderr.write(
            "no trace blocks parsed from %r "
            "(expected `go tool pprof -traces` output with dashed block separators)\n" % src
        )
        return 1

    with io.open(dst, "w", encoding="utf-8") as f:
        for k, v in sorted(out.items(), key=lambda x: -x[1]):
            f.write("%s %d\n" % (k, round(v)))

    # compute from actually-written (rounded) values, not pre-round floats
    total = 0
    for k, v in out.items():
        total += round(v)
    flat = {}
    for k, v in out.items():
        # flat = self time: weight goes to the LAST frame (leaf) only,
        # matching `pprof -top` flat column semantics
        leaf = k.split(";")[-1]
        flat[leaf] = flat.get(leaf, 0) + v
    print("collapsed stacks: %d, total weight: %.0f ms" % (len(out), total))
    print("top-10 flat frames (ms):")
    for name, w in sorted(flat.items(), key=lambda x: -x[1])[:10]:
        print("  %8.0f  %s" % (w, name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
