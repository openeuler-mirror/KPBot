#!/usr/bin/env python3
"""Tier-A integrity checks for the flamegraph-libopt skill.

Two layers:
  A. knowledge-card integrity (same contract as sibling skills):
     frontmatter, reference links, methodology anchors, sensitive-content guard
  B. parser functional tests on synthetic SVG fixtures: integrity self-check
     (pass + fail paths), orientation detection, de-nest vs naive share,
     paren-truncation, thousands separators, orphan frame detection, and
     the gate-downgrade trust boundary (advisory downgrade only for a
     strict "(N threads merged)" marker in the root region; deep or loose
     frame names keep the negative-self gate hard).

Writes results/result.json; exits non-zero on any failure.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
PARSER = SKILL_DIR / "scripts" / "parse_flamegraph_svg.py"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

# Declared parser test count; must match the case.yaml expectation.
# checks.py cross-checks this (see contract sync) so the two cannot drift.
PARSER_TESTS_TOTAL = 15

failures = []
links_checked = 0
anchors_found = 0
parser_tests_passed = 0


def fail(msg):
    failures.append(msg)


# ---- 1. frontmatter ----
text = SKILL_MD.read_text(encoding="utf-8")
fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
if not fm_match:
    fail("SKILL.md has no frontmatter block")
else:
    fm = fm_match.group(1)
    name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
    if not name_match:
        fail("frontmatter: missing name")
    elif name_match.group(1) != SKILL_DIR.name:
        fail(f"frontmatter: name {name_match.group(1)!r} != dir {SKILL_DIR.name!r}")
    if not re.search(r"^description:\s*\S", fm, re.MULTILINE | re.DOTALL):
        fail("frontmatter: missing/empty description")

# ---- 2. reference links resolve (source layout; install layout symlinks
#         references/ + scripts/ into the skill dir, so the same paths hold) ----
for target in re.findall(r"\]\((references/[^)]+)\)", text):
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken reference link: {target}")
if links_checked < 2:
    fail(f"expected at least 2 reference links, found {links_checked}")
if not PARSER.is_file():
    fail(f"parser script missing: {PARSER}")

# ---- 3. methodology invariants stay documented ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = [
    "Σ(self)",          # integrity self-check before trusting numbers
    "去嵌套",           # de-nest discipline for recursive frames
    "链接指纹",         # link-fingerprint verification before attribution
    "上游",             # upstream-diff-first rule
    "形态前提",         # shape-premise check before porting
    "self 口径",        # self-share definition and its limits
]
for anchor in ANCHORS:
    if anchor in corpus:
        anchors_found += 1
    else:
        fail(f"methodology anchor missing from corpus: {anchor!r}")

# ---- 4. no sensitive content ----
SENSITIVE = [
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b10\.\d+\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "personal home path"),
    (re.compile(r"[A-Z]:[/\\]Users[/\\]"), "personal windows path"),
]
sensitive_findings = []
for pattern, label in SENSITIVE:
    for m in pattern.finditer(corpus):
        sensitive_findings.append(f"{label}: {m.group(0)[:40]}")
if sensitive_findings:
    fail(f"sensitive content found: {sensitive_findings}")

# ---- 5. parser functional tests (synthetic fixtures) ----
spec = importlib.util.spec_from_file_location("flg_parser", PARSER)
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


def frame(name, samples, x, y, w, h=10.0):
    pct = samples / 10.0
    return ('<g><title>%s (%d samples, %.2f%%)</title>'
            '<rect x="%s" y="%s" width="%s" height="%s" rx="1"/></g>'
            % (name, samples, pct, x, y, w, h))


# 5.1 integrity pass + orientation + naive-vs-denest on a recursive shape
svg = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("main", 1000, 0, 10, 1000),
    frame("lib::parse_loop", 600, 0, 20, 600),
    frame("lib::parse_loop", 300, 0, 30, 300),      # recursion under path 1
    frame("lib::leaf", 150, 0, 40, 150),
    frame("lib::other", 150, 150, 40, 150),
    frame("lib::parse_loop", 300, 600, 20, 300),    # second call path
    frame("lib::leaf2", 150, 600, 30, 150),
    "</svg>",
])
try:
    frames = parser.parse_frames(svg)
    root, total, orient = parser.build_tree(frames)
    s, t, ok = parser.integrity_check(frames, total)
    naive = parser.naive_share(frames)
    dn, eff = parser.denest_share(root)
    # root at y=0 (top) -> icicle geometry -> "root-top"
    assert orient == "root-top" and total == 1000 and ok and s == 1000
    assert naive["lib::parse_loop"] == 1200        # naive: 120% — inflated
    assert dn["lib::parse_loop"] == 900 and eff == 1000
    assert not parser.inconsistent_frames(frames)
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001 — test harness reports any failure
    fail(f"parser test 5.1 (integrity/orientation/de-nest) raised: {exc}")

# 5.1b HTML-escaped template names decode (&lt;/&gt; -> </>)
svg_esc = ('<svg xmlns="http://www.w3.org/2000/svg">'
           '<g><title>MapMergeFrom&lt;..., AttrValue&gt; (100 samples, 100.00%)</title>'
           '<rect x="0" y="0" width="1000" height="10"/></g></svg>')
try:
    frames_esc = parser.parse_frames(svg_esc)
    assert frames_esc[0]["name"] == "MapMergeFrom<..., AttrValue>", frames_esc[0]["name"]
    # --lib matching must work on the decoded name
    assert len(parser.lib_frames(frames_esc, "MapMergeFrom<")) == 1
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.1b (HTML entity decode) raised: {exc}")

# 5.2 orientation detection: root at y-max -> "root-bottom" (flamegraph.pl default)
svg2 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 40, 1000),
    frame("main", 1000, 0, 30, 1000),
    frame("lib::f", 500, 0, 20, 500),
    frame("other", 500, 500, 20, 500),
    frame("leaf", 250, 0, 10, 250),
    "</svg>",
])
try:
    frames2 = parser.parse_frames(svg2)
    root2, total2, orient2 = parser.build_tree(frames2)
    _, _, ok2 = parser.integrity_check(frames2, total2)
    assert orient2 == "root-bottom" and ok2
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.2 (root-bottom orientation) raised: {exc}")

# 5.3 integrity FAILURE path: the checker must reject mismatched totals.
#     (On any tree that build_tree accepts, sum(self) == root samples holds
#     identically — the checker is a defense-in-depth guard against tree-builder
#     regressions, so its failure path is tested directly at unit level.)
svg3 = '<svg>' + frame("all", 1000, 0, 0, 1000) + frame("main", 400, 0, 10, 400) + '</svg>'
try:
    frames3 = parser.parse_frames(svg3)
    root3, total3, _ = parser.build_tree(frames3)
    _, _, ok_true = parser.integrity_check(frames3, total3)
    _, _, ok_false = parser.integrity_check(frames3, total3 + 500)
    assert ok_true and not ok_false
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.3 (integrity failure path) raised: {exc}")

# 5.4 paren truncation: library name only in an argument position must NOT match
svg4 = '<svg>' + frame("user::fn(lib::Type)", 100, 0, 0, 100) + '</svg>'
try:
    frames4 = parser.parse_frames(svg4)
    assert len(parser.lib_frames(frames4, "lib::")) == 0
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.4 (paren truncation) raised: {exc}")

# 5.5 thousands separators in sample counts
svg5 = ('<svg><g><title>all (1,570,780 samples, 100.00%)</title>'
        '<rect x="0" y="0" width="1000" height="10"/></g></svg>')
try:
    frames5 = parser.parse_frames(svg5)
    assert frames5[0]["samples"] == 1570780
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.5 (thousands separators) raised: {exc}")

# 5.6 orphan frame (covered by no ancestor) must raise ParseError
svg6 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("orphan", 400, 1100, 20, 400),  # beyond root's x-range
    "</svg>",
])
try:
    parser.build_tree(parser.parse_frames(svg6))
    fail("parser test 5.6: orphan frame was not detected")
except parser.ParseError:
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.6 (orphan detection) raised unexpected: {exc}")

# 5.7 within-layer overlap must be rejected (premise assertion), even though
#     integrity would still pass — wrong-tree-with-green-gate is the trap
svg7 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("A", 600, 0, 10, 600),
    frame("B", 600, 300, 10, 600),   # overlaps A in the same layer
    "</svg>",
])
try:
    parser.build_tree(parser.parse_frames(svg7))
    fail("parser test 5.7: within-layer overlap was not detected")
except parser.ParseError:
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.7 (overlap rejection) raised unexpected: {exc}")

# 5.8 CLI exit-code contract: argparse usage errors exit 1 (not 2), keeping
#     exit 2 exclusive to "numbers untrustworthy"
try:
    import subprocess as _sp
    rc = _sp.run([sys.executable, str(PARSER), "--top", "abc"],
                 capture_output=True).returncode
    assert rc == 1, f"usage error exits {rc}, expected 1"
    rc_bad = _sp.run([sys.executable, str(PARSER), "no-such-file.svg"],
                     capture_output=True).returncode
    assert rc_bad in (1, 2), f"missing file exits {rc_bad}"
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.8 (exit-code contract) raised: {exc}")

# 5.9 wrong-parent attachment must trip the STRUCTURE gates while the sum
#     gate stays green: build a correct tree, then deliberately re-parent a
#     child to its grandparent and assert structure_check fails.
#     (sum(self)==total is an identity over ANY parent assignment -- this is
#     exactly the trap the structure gates exist to catch.)
svg9 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("P", 400, 0, 10, 400),
    frame("C", 300, 0, 20, 300),          # visually inside P
    "</svg>",
])
try:
    frames9 = parser.parse_frames(svg9)
    root9, total9, _ = parser.build_tree(frames9)
    c9 = [f for f in frames9 if f["name"] == "C"][0]
    p9 = [f for f in frames9 if f["name"] == "P"][0]
    all9 = root9
    # simulate the mis-attachment: move C from P to the grandparent 'all'
    p9["children"].remove(c9)
    all9["children"].append(c9)
    c9["parent"] = all9
    _, _, ok9 = parser.integrity_check(frames9, total9)
    assert ok9, "sum gate should stay green under re-parenting (identity)"
    viol9, _warn9 = parser.structure_check(frames9)
    assert viol9, "structure gates must fail on wrong-parent attachment"
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.9 (re-parent detection) raised: {exc}")

# 5.10 float-boundary regression (production geometry): adjacent frames
#      1129.0+59.4 -> 1188.4+1.4 -> 1189.8+0.2 -- the cumulative sums drift
#      (1188.4+1.4 == 1189.8000000000002 != 1189.8) and once flipped the
#      binary-search go-right decision, attaching the deepest frame to the
#      top-level frame instead of its true parent.
svg10 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1190, 0, 0, 1190),
    frame("F1", 546, 1129.0, 10, 59.4),
    frame("F2", 1.4, 1188.4, 10, 1.4),
    frame("F3", 0.2, 1189.8, 20, 0.2),
    "</svg>",
])
try:
    frames10 = parser.parse_frames(svg10)
    root10, total10, _ = parser.build_tree(frames10)
    f3 = [f for f in frames10 if f["name"] == "F3"][0]
    assert f3["parent"]["name"] == "F2", (
        "F3 must attach to F2 (nearest covering); float-boundary drift "
        f"attached it to {f3['parent']['name']}"
    )
    viol10, _ = parser.structure_check(frames10)
    assert not viol10, f"structure violations: {viol10[:2]}"
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.10 (float-boundary parent) raised: {exc}")

# 5.11 merged-graph print-drift regression (production geometry): swapper
#      ends at 1050.0 while the child starts at 1049.9 (0.1px print overlap,
#      tolerated by _assert_no_overlap); the strict binary search once went
#      LEFT there and dropped the whole thread stack onto the root.
svg11 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1190, 0, 0, 1190),
    frame("swapper", 32.9, 1017.1, 10, 32.9),       # ends at 1050.0
    frame("worker", 140.1, 1049.9, 10, 140.1),      # starts 0.1px earlier
    frame("thread", 140.1, 1049.9, 20, 140.1),      # child of worker
    "</svg>",
])
try:
    frames11 = parser.parse_frames(svg11)
    root11, total11, _ = parser.build_tree(frames11)
    t11 = [f for f in frames11 if f["name"] == "thread"][0]
    assert t11["parent"]["name"] == "worker", (
        "thread stack must attach to worker (its covering parent); print "
        f"drift dropped it to {t11['parent']['name']}"
    )
    viol11, _ = parser.structure_check(frames11)
    assert not viol11, f"structure violations: {viol11[:2]}"
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.11 (print-drift parent) raised: {exc}")

# 5.12 advisory downgrade requires a STRICT marker in the root region: the
#     negative-self geometry a real merged graph produces (children sample
#     sum past the marker frame), with the marker frame at depth 1.
svg12 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("swapper (2 threads merged)", 400, 0, 10, 400),
    frame("C1", 300, 0, 20, 200),
    frame("C2", 200, 200, 20, 200),
    "</svg>",
])
try:
    frames12 = parser.parse_frames(svg12)
    root12, total12, _ = parser.build_tree(frames12)
    _, _, ok12 = parser.integrity_check(frames12, total12)
    assert ok12, "integrity must stay green (sum self == total)"
    assert parser.is_merged_graph(frames12), "strict root-region marker must classify merged"
    viol12, warn12 = parser.structure_check(frames12)
    assert not viol12 and warn12, (
        f"expected advisory warnings, got viol={viol12[:1]} warn={warn12[:1]}"
    )
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.12 (strict-marker advisory downgrade) raised: {exc}")

# 5.13 frame-name attack, strict-form marker at DEEP depth: the depth bound
#     keeps Gate A hard -- a deep "(N threads merged)" name is ordinary
#     call-stack text, not a merged-root marker, and must NOT downgrade.
svg13 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("P", 400, 0, 10, 400),
    frame("C1", 300, 0, 20, 200),
    frame("deep (4 threads merged)", 200, 200, 20, 200),
    "</svg>",
])
try:
    frames13 = parser.parse_frames(svg13)
    root13, total13, _ = parser.build_tree(frames13)
    assert not parser.is_merged_graph(frames13), "deep strict-form name must NOT classify merged"
    viol13, warn13 = parser.structure_check(frames13)
    assert viol13 and not warn13, (
        f"deep strict-form name must hard-fail, got viol={viol13[:1]} warn={warn13[:1]}"
    )
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.13 (deep marker attack) raised: {exc}")

# 5.14 frame-name attack, loose substring in the root region: "merged)" as
#     a bare substring never downgrades (pins down the pre-fix behavior
#     mxj1314 flagged: gate strength must not follow frame text).
svg14 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("P(merged) sort", 400, 0, 10, 400),
    frame("C1", 300, 0, 20, 200),
    frame("C2", 200, 200, 20, 200),
    "</svg>",
])
try:
    frames14 = parser.parse_frames(svg14)
    root14, total14, _ = parser.build_tree(frames14)
    assert not parser.is_merged_graph(frames14), "loose substring must NOT classify merged"
    viol14, warn14 = parser.structure_check(frames14)
    assert viol14 and not warn14, (
        f"loose substring must hard-fail, got viol={viol14[:1]} warn={warn14[:1]}"
    )
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.14 (loose-substring attack) raised: {exc}")

# ---- 6. contract sync: case.yaml must track the parser test count ----
# The tier-A contract asserts parser_tests_passed == N in case.yaml; adding
# parser tests without updating it makes every run fail (the exact P2
# filed in review). Fail loudly here, at the source of the drift.
case_yaml_text = (Path(__file__).resolve().parent / "case.yaml").read_text(
    encoding="utf-8")
m_case = re.search(
    r"field:\s*parser_tests_passed\s*\n\s*expect:\s*(\d+)", case_yaml_text)
if not m_case:
    failures.append("case.yaml: parser_tests_passed expectation not found")
elif int(m_case.group(1)) != PARSER_TESTS_TOTAL:
    failures.append(
        "case.yaml expects %s parser tests but checks.py declares %d — "
        "keep the case.yaml contract in sync when tests are added/removed"
        % (m_case.group(1), PARSER_TESTS_TOTAL)
    )

# ---- report ----
result = {
    "success": not failures,
    "failures": failures,
    "links_checked": links_checked,
    "anchors_found": anchors_found,
    "anchors_total": len(ANCHORS),
    "sensitive_findings": sensitive_findings,
    "parser_tests_passed": parser_tests_passed,
    "parser_tests_total": PARSER_TESTS_TOTAL,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if not failures else 1)
