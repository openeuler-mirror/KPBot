#!/usr/bin/env python3
"""Parse flamegraph SVG, verify self-percentage integrity, aggregate library frames.

Reads a flamegraph.pl-style SVG (each frame = <rect> + <title> "name (N samples, P%)"),
rebuilds the frame tree by geometric covering, and enforces the integrity rule
before any number is trusted: sum(self) must equal the root total (100.000%).

Library-frame aggregation supports the de-nest discipline: for each root-to-leaf
call stack, a frame name is counted once — the de-nested share of a name is the
sum of samples of stacks that CONTAIN that name. Recursive/template-instantiated
frames appear many times on the tree; naive summation inflates their share by
counting recursion depth (measured case: 9 instances / 5 recursive -> naive
10.85% vs de-nested ~3.0%).

Pure stdlib; never executes anything; read-only on inputs.

Usage:
    python3 parse_flamegraph_svg.py graph.svg [--lib PATTERN] [--denest]
                                              [--top N] [--json OUT]
Exit codes: 0 ok; 1 usage/IO error (including argparse usage errors, so that
2 stays exclusive); 2 parse/integrity failure (numbers unusable).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

RECT_RE = re.compile(
    r"<rect[^>]*?\bx=\"([\d.eE+-]+)\"[^>]*?\by=\"([\d.eE+-]+)\""
    r"[^>]*?\bwidth=\"([\d.eE+-]+)\"[^>]*?\bheight=\"([\d.eE+-]+)\"",
)
# A frame = a <title> "name (N samples, P%)" immediately followed by its
# <rect>. Matching title-then-rect pairs directly (instead of scanning <g>
# blocks) tolerates arbitrary <g> nesting/wrappers in flamegraph.pl output.
FRAME_BLOCK_RE = re.compile(
    r"<title>(.*?) \(([\d,.]+) samples?, ([\d.]+)%\)</title>\s*<rect\b", re.S
)

MIN_HEIGHT = 0.5  # frames thinner than this are separators/detail, not frames

# Float-boundary epsilon for pixel-covering comparisons: adjacent-frame
# edges accumulate drift (x + w == 1189.8000000000002 vs a sibling's
# literal 1189.8), which once flipped the binary-search go-right decision
# and attached a child to its grandparent. Sub-print-precision, so it
# cannot change any visual attachment decision -- only guard arithmetic.
COV_EPS = 1e-6
# Print-precision tolerance for SVG coordinate drift in merged production
# graphs (0.1px granularity). Currently disabled (=0): enabling it (0.5)
# created boundary ambiguity and MORE violations than it fixed -- see
# checks.py regression history. Kept as a documented dead-end.
COV_TOL_PX = 0.0
# Right-edge drift allowance for the linear fallback in _find_covering:
# merged production graphs let adjacent frames overlap by up to ~0.1px
# (print precision; _assert_no_overlap tolerates it), which derails the
# strict binary search exactly at those boundaries and once dropped whole
# thread stacks onto the root. Left edge stays strict in the fallback.
COV_DRIFT = 0.2

# flamegraph.pl's merged-thread marker, exactly as the tool emits it:
# "<name> (N threads merged)" in the stack-root region. Gate strength must
# NOT be decided by loose substring matching on arbitrary frame names -- a
# random frame containing "merged)" would silently downgrade the negative-
# self gate while the output still reads OK.
MERGED_MARKER = re.compile(r"^(.*) \((\d+) threads merged\)$")


class ParseError(Exception):
    pass


def _frame_name(raw_title):
    """Decode a frame title to the plain function name.

    flamegraph.pl XML-escapes template angle brackets (&lt;/&gt;) etc. in
    <title>; C++ template-heavy names (MapMergeFrom<..., AttrValue>) are the
    norm for this skill's targets, so titles must be HTML-unescaped. Stripping
    raw tags instead would silently corrupt un-escaped template names.
    """
    return html.unescape(raw_title).strip()


def _num(text):
    return float(text)


def _samples(text):
    return int(text.replace(",", ""))


def parse_frames(svg_text):
    """Extract (name, samples, x, y, w, h) per frame block.

    A frame = a <title> "name (N samples, P%)" immediately followed by its
    <rect>. Matching title-then-rect pairs directly (instead of scanning <g>
    blocks) tolerates arbitrary <g> nesting/wrappers in flamegraph.pl output;
    sample counts may carry thousands separators (perf scales to ns, e.g.
    "1,570,780,153,186").
    """
    frames = []
    pos = 0
    while True:
        m = FRAME_BLOCK_RE.search(svg_text, pos)
        if not m:
            break
        # the frame's <rect> starts where the title block ended (at "<rect")
        rect = RECT_RE.match(svg_text, m.end() - len("<rect"))
        if not rect:
            pos = m.end()
            continue
        name = _frame_name(m.group(1))
        samples = _samples(m.group(2))
        x, y, w, h = (_num(rect.group(i)) for i in (1, 2, 3, 4))
        if h >= MIN_HEIGHT and w > 0:
            frames.append({"name": name, "samples": samples, "x": x, "y": y, "w": w, "h": h})
        pos = rect.end()
    if not frames:
        raise ParseError("no <title>+<rect> frame blocks found (not a flamegraph SVG?)")
    return frames


def detect_orientation(frames):
    """Return the root's geometric position in the SVG.

    "root-top"  = root frame at smallest y (icicle; flamegraph.pl --inverted).
    "root-bottom" = root frame at largest y (flame graph; flamegraph.pl default
    output and most merged-tool products). SVG y=0 is the top of the image.
    The label describes geometry only — never assume it from the tool name.
    """
    root = max(frames, key=lambda f: f["samples"])
    median_y = sorted(f["y"] for f in frames)[len(frames) // 2]
    return "root-bottom" if root["y"] > median_y else "root-top"


def _assert_no_overlap(sorted_layer, y):
    """Frames within a layer must not overlap — the covering algorithm's
    premise. Violation (corrupted SVG / foreign tools) would silently attach
    children to the wrong parent while integrity still passes; refuse instead.
    Sub-pixel boundary crossings (< 1px) are tolerated: merged-graph tools
    produce float-rounding-level adjacencies at frame boundaries.
    """
    for a, b in zip(sorted_layer, sorted_layer[1:]):
        overlap = a["x"] + a["w"] - b["x"]
        if overlap > 1.0:
            raise ParseError(
                "layer y=%s: frames %r and %r overlap by %.2fpx — premise of "
                "the covering algorithm violated" % (y, a["name"], b["name"], overlap)
            )


def build_tree(frames):
    """Attach children by geometric covering; returns (root, total, orientation).

    Within a layer frames do not overlap (asserted); a child's x-range is
    covered by its parent's x-range. Orientation is decided first because it
    determines which side the root layer is on.
    """
    orientation = detect_orientation(frames)
    # Layer key: y rounded (SVG y is quantized per depth level).
    layers = {}
    for f in frames:
        layers.setdefault(round(f["y"], 1), []).append(f)
    for y in layers:
        lay = sorted(layers[y], key=lambda f: f["x"])
        _assert_no_overlap(lay, y)
    ys = sorted(layers.keys(), reverse=(orientation == "root-bottom"))
    # ys[0] is the root-most layer under this ordering:
    # root-bottom graphs walk from the largest y downward; root-top upward.

    total = max(f["samples"] for f in frames)
    roots = [f for f in layers[ys[0]] if f["samples"] == total]
    if len(roots) != 1:
        raise ParseError(
            "root frame not unique at layer y=%s (found %d)" % (ys[0], len(roots))
        )
    root = roots[0]
    for f in frames:
        f["children"] = []
        f["parent"] = None

    # For each layer (root-side out), a frame's parent is the covering frame in
    # ANY shallower layer — merged flamegraphs ("(N threads merged)" frames)
    # expand only some children per merged group, so a grandchild frame may
    # appear one or more layers below its covering ancestor.
    def _find_covering(prev, f):
        # Strict binary search first (the common path).
        lo, hi = 0, len(prev) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            p = prev[mid]
            if p["x"] - COV_EPS <= f["x"] and f["x"] + f["w"] <= p["x"] + p["w"] + COV_EPS:
                return p
            if p["x"] + p["w"] <= f["x"] + COV_EPS:
                lo = mid + 1
            else:
                hi = mid - 1
        # Print-drift fallback: merged production graphs overlap adjacent
        # frames by up to ~0.1px (print precision; _assert_no_overlap
        # tolerates it), which derails the strict ordering assumption exactly
        # at those boundaries and once dropped whole thread stacks onto the
        # root (negative-self / sibling-overlap structure violations). Only
        # boundary frames reach this scan; LEFT edge stays strict.
        for p in prev:
            if p["x"] - COV_EPS <= f["x"] and f["x"] + f["w"] <= p["x"] + p["w"] + COV_DRIFT:
                return p
        return None

    prev_layers = [sorted(layers[ys[0]], key=lambda f: f["x"])]
    for y in ys[1:]:
        cur = sorted(layers[y], key=lambda f: f["x"])
        for f in cur:
            parent = None
            for prev in reversed(prev_layers):  # nearest layer first
                parent = _find_covering(prev, f)
                if parent is not None:
                    break
            if parent is None:
                raise ParseError(
                    "frame %r (y=%s) has no covering parent — layout not a tree" % (f["name"], y)
                )
            f["parent"] = parent
            parent["children"].append(f)
        prev_layers.append(cur)
    return root, total, orientation


def self_samples(frame):
    return frame["samples"] - sum(c["samples"] for c in frame["children"])


def _depth(frame):
    d = 0
    while frame.get("parent") is not None:
        frame = frame["parent"]
        d += 1
    return d


def is_merged_graph(frames):
    """Classify a graph as merged-thread by root-region markers only.

    Real markers (flamegraph.pl merge-threads output) sit at tree depth 0-1:
    the thread-root layer itself, or one below a synthetic 'all' root
    (verified on production merged graphs). The strict marker pattern plus
    the depth bound keep the downgrade decision anchored to the tool's real
    output shape: a deep frame (or a loose substring) never downgrades
    gate strength.
    """
    return any(
        _depth(f) <= 1 and MERGED_MARKER.match(f["name"]) for f in frames
    )


def structure_check(frames, tol_px=1.0):
    """Structural gates that are sensitive to wrong-parent attachment.

    sum(self)==total holds identically for ANY parent assignment (build_tree
    gives every non-root frame exactly one parent, so re-parenting only moves
    self between frames -- the sum gate is a defense-in-depth guard against
    tree-builder regressions, NOT against mis-attachment). These gates catch
    what the sum gate cannot:

    Gate A (negative self): children carrying more samples than the frame
        itself can only arise from double-counted attachment (a descendant
        counted under two siblings). Hard failure on non-merged graphs; on
        merged graphs (a strict "(N threads merged)" marker in the root
        region, see is_merged_graph) sub-pixel quantization legitimately
        produces such frames (0.1px print granularity makes a 0.2px parent
        hold two 0.1px children whose exact sample counts sum past the
        parent's own), so there it is returned as warnings, not
        violations.
    Gate B (sibling overlap): frames sharing one parent partition its span.
        A child that overflows its visual parent by print-precision drift
        gets attached to an ancestor and then overlaps its own visual
        parent, which is a sibling under that ancestor. Hard for all
        graphs -- it is what catches whole stacks dropped onto the root.

    Returns (violations, warnings), both lists of strings (empty == pass).
    """
    merged = is_merged_graph(frames)
    violations = []
    warnings = []
    for f in frames:
        self_n = self_samples(f)
        if self_n < -1:
            msg = "negative self: %r self=%d samples" % (f["name"], self_n)
            (warnings if merged else violations).append(msg)
    for f in frames:
        kids = f["children"]
        if len(kids) < 2:
            continue
        kids = sorted(kids, key=lambda c: c["x"])
        for a, b in zip(kids, kids[1:]):
            overlap = a["x"] + a["w"] - b["x"]
            if overlap > tol_px:
                violations.append(
                    "sibling overlap %.2fpx: %r overlaps %r under %r"
                    % (overlap, a["name"], b["name"], f["name"])
                )
    return violations, warnings


def integrity_check(frames, total):
    """Sum of self over all frames must equal the total samples."""
    s = sum(self_samples(f) for f in frames)
    return s, total, abs(s - total) <= max(1, total * 1e-6)


def iter_stacks(frame, stack):
    """Yield (stack tuple, samples) per root-to-leaf path.

    Interior-frame self time rides the frame's own name: an interior frame with
    self samples yields a leaf stack ending at that same name (not a synthetic
    marker), so per-stack count-once aggregation attributes its self time to
    the real frame name exactly once.
    """
    stack = stack + [frame["name"]]
    if not frame["children"]:
        yield stack, frame["samples"]
        return
    child_sum = sum(c["samples"] for c in frame["children"])
    if frame["samples"] > child_sum:
        yield stack, frame["samples"] - child_sum
    for c in frame["children"]:
        yield from iter_stacks(c, stack)


def lib_denested_pct(root, pattern):
    """De-nested library share: samples of stacks that contain >=1 lib frame.

    Per stack, all lib frames count once collectively (a stack either runs
    library code or not) — nested lib-in-lib frames (recursion inside the
    library) do not add up. Naive summation of lib frame instances can exceed
    100% for the same reason single-name summation can. Returns raw sample
    hits; divide by the effective total (see denest_share).
    """
    pat = pattern.lower()
    hits = 0
    for stack, samples in iter_stacks(root, []):
        if any(pat in name.split("(", 1)[0].lower() for name in stack):
            hits += samples
    return hits


def find_root(frames):
    for f in frames:
        if f["parent"] is None:
            return f
    raise ParseError("root frame not found")


def denest_share(root):
    """De-nested share per frame name: stacks containing the name, counted once.

    Returns (share, effective_total). effective_total = sum of yielded stack
    samples; it exceeds the root total when the graph has inconsistent sample
    counts (merged-thread graphs: children sum > parent samples under
    "(N threads merged)" frames). Shares are fractions of effective_total so
    they stay <= 100%; callers must report the inconsistency when the two
    totals differ.
    """
    share = {}
    effective_total = 0
    for stack, samples in iter_stacks(root, []):
        effective_total += samples
        for name in set(stack):  # each name counts once per stack
            share[name] = share.get(name, 0) + samples
    return share, effective_total


def inconsistent_frames(frames):
    """Frames whose children sample sum exceeds their own (merged-graph artifact)."""
    return [
        f for f in frames
        if f["children"] and sum(c["samples"] for c in f["children"]) > f["samples"]
    ]


def naive_share(frames):
    """Naive share: sum samples over ALL frame instances of a name.

    This is the deliberately-wrong baseline the de-nest discipline guards
    against: summing instances counts recursion depth and call multiplicity
    into the share (a name can exceed 100%).
    """
    share = {}
    for f in frames:
        share[f["name"]] = share.get(f["name"], 0) + f["samples"]
    return share


def classify(name):
    lowered = name.lower()
    for key, words in (
        ("dtor", ("~", "destroy", "clear", "delete", "dtor")),
        ("ctor", ("create", "construct", "ctor", "new")),
        ("copy", ("copy", "clone", "merge", "assign")),
        ("serialize", ("serialize", "serialize", "parse", "decode", "encode", "write", "read")),
        ("lookup", ("find", "lookup", "search", "hash")),
        ("alloc", ("alloc", "malloc", "arena", "free", "reserve")),
    ):
        if any(w in lowered for w in words):
            return key
    return "other"


def lib_frames(frames, pattern):
    """Frames whose (pre-paren) name contains the library pattern."""
    out = []
    pat = pattern.lower()
    for f in frames:
        head = f["name"].split("(", 1)[0]
        if pat in head.lower():
            out.append(f)
    return out


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1 (not argparse's default 2), keeping exit code 2
    exclusive to "numbers untrustworthy" (parse/integrity failure)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("ERROR: %s\n" % message)
        sys.exit(1)


def main(argv=None):
    ap = _Parser(description=__doc__.splitlines()[0])
    ap.add_argument("svg", type=Path, help="flamegraph SVG file")
    ap.add_argument("--lib", help="library name pattern (substring, matched before first '(')")
    ap.add_argument("--denest", action="store_true",
                    help="report de-nested shares (per-stack count-once) instead of naive")
    ap.add_argument("--top", type=int, default=20, help="top-N functions to print (default 20)")
    ap.add_argument("--json", type=Path, help="optional JSON output path (must not exist)")
    args = ap.parse_args(argv)

    try:
        svg_text = args.svg.read_text(encoding="utf-8", errors="replace")
        frames = parse_frames(svg_text)
        root, total, orientation = build_tree(frames)
        s, t, ok = integrity_check(frames, total)
    except (OSError, ParseError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2 if isinstance(exc, ParseError) else 1

    print("orientation=%s frames=%d total_samples=%d" % (orientation, len(frames), total))
    if not ok:
        print(
            "INTEGRITY FAILURE: sum(self)=%d != total=%d — tree construction is wrong; "
            "do NOT trust any number from this graph" % (s, t),
            file=sys.stderr,
        )
        return 2
    structural, warnings = structure_check(frames)
    if structural:
        print(
            "STRUCTURE FAILURE: %d violation(s) — frames attached to the wrong "
            "ancestor; do NOT trust any number from this graph" % len(structural),
            file=sys.stderr,
        )
        for v in structural[:10]:
            print("  - %s" % v, file=sys.stderr)
        return 2
    if warnings:
        print(
            "structure: ADVISORY — %d negative-self frame(s) tolerated as "
            "merged-graph sub-pixel artifacts (strict root-region marker "
            "verified); NOT verified clean" % len(warnings),
            file=sys.stderr,
        )
        for v in warnings[:5]:
            print("  ~ %s" % v, file=sys.stderr)
    print("integrity: sum(self)=%d == total (%.3f%%) OK" % (s, 100.0 * s / t))
    if warnings:
        print(
            "structure: ADVISORY — negative self downgraded for merged graph "
            "(details on stderr); sibling-overlap gate passed"
        )
    else:
        print("structure: no-negative-self, sibling-disjoint OK")

    # De-nested aggregates are needed for --denest ranking AND for the lib
    # de-nested percentage; both are normalized by the effective stack total
    # (sum of stack samples), which exceeds the root total on merged-thread
    # graphs — without this normalization the de-nested figure could silently
    # exceed 100% while being labeled "de-nested".
    eff_total = total
    denest = None
    inconsistent = []
    if args.denest or args.lib:
        denest, eff_total = denest_share(root)
        inconsistent = inconsistent_frames(frames)
        if inconsistent and eff_total != total:
            print(
                "WARNING: %d frame(s) have children sum > parent samples (merged-graph "
                "artifact, e.g. \"(N threads merged)\"); de-nested shares are normalized "
                "to effective total %d (root total %d)" % (len(inconsistent), eff_total, total),
                file=sys.stderr,
            )

    if args.denest:
        share = denest
    else:
        share = naive_share(frames)
    ranked = sorted(share.items(), key=lambda kv: -kv[1])[: args.top]
    denom = eff_total if args.denest else total

    if args.lib:
        libf = lib_frames(frames, args.lib)
        lib_naive = sum(f["samples"] for f in libf)
        lib_denest = lib_denested_pct(root, args.lib)
        print("library %r: naive=%.2f%% de-nested=%.2f%% frames=%d"
              % (args.lib, lib_naive * 100.0 / total, lib_denest * 100.0 / eff_total, len(libf)))
        cats = {}
        for f in libf:
            cats[classify(f["name"])] = cats.get(classify(f["name"]), 0) + self_samples(f)
        neg = sum(1 for f in libf if self_samples(f) < 0)
        if neg:
            print("  (note: %d lib frame(s) have negative self — merged-graph "
                  "artifact, see warning above)" % neg)
        cat_total = sum(cats.values())
        for cat in sorted(cats, key=lambda c: -cats[c]):
            print("  category %-10s self=%d (%.2f%% of lib)"
                  % (cat, cats[cat], cats[cat] * 100.0 / cat_total if cat_total else 0))

    print("top functions (%s):" % ("de-nested" if args.denest else "naive"))
    for name, v in ranked:
        print("  %8.3f%%  %s" % (v * 100.0 / denom, name))

    if args.json:
        if args.json.exists():
            print("ERROR: output exists, refusing to overwrite: %s" % args.json, file=sys.stderr)
            return 1
        payload = {
            "source": str(args.svg),
            "orientation": orientation,
            "frames": len(frames),
            "total_samples": total,
            "integrity_ok": True,
            "mode": "denest" if args.denest else "naive",
            "effective_total": eff_total,
            "inconsistent_frames": len(inconsistent),
            "top": [{"name": n, "samples": v, "pct": v * 100.0 / denom} for n, v in ranked],
        }
        if args.lib:
            payload["library"] = {
                "pattern": args.lib,
                "naive_pct": lib_naive * 100.0 / total,
                "denested_pct": lib_denest * 100.0 / eff_total,
                "categories": cats,
            }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print("json -> %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
