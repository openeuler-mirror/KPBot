#!/usr/bin/env python3
"""Tier-A integrity checks for the flamegraph-parsing skill.

Groups:
  static    frontmatter / link resolution (references + scripts) / methodology
            anchors / full-scope sensitive scan / BOM guard
  parser    functional tests on synthetic SVG fixtures: integrity self-check
            (pass + fail paths), orientation detection (incl. the two-frame
            standard layout), de-nest vs naive share, paren-truncation,
            thousands separators, orphan frame detection, print-drift and
            float-boundary regressions, merged-graph trust boundary, and the
            negative-self gate (any negative in a normal graph is a
            violation, including -1).
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
PARSER_TESTS_TOTAL = 17

failures = []
checks = 0
parser_tests_passed = 0


def ok():
    global checks
    checks += 1


def fail(msg):
    failures.append(msg)


def redact(s):
    # 键值形态：保留键名、值按长度占位（防值原文进入 failures/CI 日志）
    s = re.sub(r"(?i)(password|token|secret|api_key|client_secret)[\s]*[:=][\s]*(\S+)",
               lambda m: f"{m.group(1)}=<redacted len={len(m.group(2))}>", s)
    s = re.sub(r"(?i)(password|token|secret|api_key|client_secret)[^,\s]*",
              r"\1=***", s)
    # Authorization 头 / 私钥块 / JWT：整串掩码（只留前缀标识）
    s = re.sub(r"(?i)authorization\s*[:=]\s*(bearer|basic)\s+\S+",
              r"authorization: \1 <redacted>", s)
    s = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
              "<redacted private key block>", s)
    return s


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
    else:
        ok()
    if re.search(r"^description:\s*\S", fm, re.MULTILINE | re.DOTALL):
        ok()
    else:
        fail("frontmatter: missing/empty description")

# ---- 2. link resolution（references/ 与 scripts/ 链接均须可解析且 >=2）----
links = re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", text)
if len(links) < 2:
    fail(f"expected >=2 references/scripts links, found {len(links)}")
else:
    ok()
for target in links:
    if (SKILL_DIR / target).is_file():
        ok()
    else:
        fail(f"broken link: {target}")
for required in ("references/parsing-guide.md", "scripts/parse_flamegraph_svg.py",
                 "scripts/README.md"):
    if (SKILL_DIR / required).is_file():
        ok()
    else:
        fail(f"missing: {required}")

# ---- 3. methodology invariants ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = ["sum(self)", "去嵌套", "方向", "结构门禁", "self 口径"]
for anchor in ANCHORS:
    if anchor in corpus:
        ok()
    else:
        fail(f"methodology anchor missing: {anchor!r}")

# ---- 4. sensitive scan（SKILL.md + references + scripts + evals 全覆盖）----
SENSITIVE = [
    (re.compile(r"\b(10|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b169\.254\.\d+\.\d+\b"), "link-local IP"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b"), "CGNAT IP"),
    (re.compile(r"\b(?:f[cd][0-9a-f]{2}|fe[89ab])[0-9a-f:]+\b", re.I), "private IPv6"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"(?i)(api_?key|secret_?key|client_secret|access_token)\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"(?i)authorization\s*[:=]\s*(bearer|basic)\s+\S+"), "auth header"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "home path"),
    (re.compile(r"/root/[a-z][a-z0-9_-]*/"), "root path"),
    (re.compile(r"[A-Z]:[/\\]Users[/\\]"), "Windows user path"),
    (re.compile(r"(?i)\bsshpass\b"), "sshpass"),
]
scan_targets = [SKILL_MD]
for sub in ("references", "scripts", "evals"):
    d = SKILL_DIR / sub
    if d.is_dir():
        scan_targets += sorted(d.iterdir())
sensitive_hits = 0
for path in scan_targets:
    if not path.is_file():
        continue
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"not utf-8: {path.name}")
        continue
    for pat, label in SENSITIVE:
        for m in pat.finditer(body):
            fail(f"{label}: {path.name}: {redact(m.group(0))!r}")
            sensitive_hits += 1
if sensitive_hits == 0:
    ok()
if len(scan_targets) >= 3:
    ok()
else:
    fail(f"sensitive scan scope too small: {len(scan_targets)} files")

# BOM 检查（run.sh 的 shebang 会被 BOM 破坏、json.loads 对带 BOM 文件报错）——
# 扫描面含用例自身（run.sh/checks.py/case.yaml）：BOM 回归恰好在这些文件上发生过
bom_targets = scan_targets + [Path(__file__).resolve().parent / f
                              for f in ("run.sh", "checks.py", "case.yaml")]
bom_bad = [p.name for p in bom_targets if p.is_file()
           and p.read_bytes().startswith(b"\xef\xbb\xbf")]
if bom_bad:
    fail(f"BOM present: {bom_bad}")
else:
    ok()

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
    frame("lib::parse_loop", 300, 0, 30, 300),
    frame("lib::leaf", 150, 0, 40, 150),
    frame("lib::other", 150, 150, 40, 150),
    frame("lib::parse_loop", 300, 600, 20, 300),
    frame("lib::leaf2", 150, 600, 30, 150),
    "</svg>",
])
try:
    frames = parser.parse_frames(svg)
    root, total, orient = parser.build_tree(frames)
    s, t, ok_i = parser.integrity_check(frames, total)
    naive = parser.naive_share(frames)
    dn, eff = parser.denest_share(root)
    assert orient == "root-top" and total == 1000 and ok_i and s == 1000
    assert naive["lib::parse_loop"] == 1200
    assert dn["lib::parse_loop"] == 900 and eff == 1000
    assert not parser.inconsistent_frames(frames)
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.1 (integrity/orientation/de-nest) raised: {exc}")

# 5.1b HTML-escaped template names decode
svg_esc = ('<svg xmlns="http://www.w3.org/2000/svg">'
           '<g><title>MapMergeFrom&lt;..., AttrValue&gt; (100 samples, 100.00%)</title>'
           '<rect x="0" y="0" width="1000" height="10"/></g></svg>')
try:
    frames_esc = parser.parse_frames(svg_esc)
    assert frames_esc[0]["name"] == "MapMergeFrom<..., AttrValue>", frames_esc[0]["name"]
    assert len(parser.lib_frames(frames_esc, "MapMergeFrom<")) == 1
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.1b (HTML entity decode) raised: {exc}")

# 5.2 orientation detection: root at y-max -> "root-bottom"
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

# 5.2b two-frame STANDARD graph: root y=32 (100), child y=16 (60) — the
#     median-y equals the root's y, the old strict-> comparison misjudged
#     "root-top" and then failed to find the root. Boundary-layer judgment
#     must classify it root-bottom and build the tree cleanly.
svg2b = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 100, 0, 32, 100),
    frame("main", 60, 0, 16, 60),
    "</svg>",
])
try:
    frames2b = parser.parse_frames(svg2b)
    root2b, total2b, orient2b = parser.build_tree(frames2b)
    _, _, ok2b = parser.integrity_check(frames2b, total2b)
    assert orient2b == "root-bottom" and ok2b and total2b == 100
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.2b (two-frame standard orientation) raised: {exc}")

# 5.3 integrity FAILURE path
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

# 5.6 orphan frame must raise ParseError
svg6 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("orphan", 400, 1100, 20, 400),
    "</svg>",
])
try:
    parser.build_tree(parser.parse_frames(svg6))
    fail("parser test 5.6: orphan frame was not detected")
except parser.ParseError:
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.6 (orphan detection) raised unexpected: {exc}")

# 5.7 within-layer overlap must be rejected
svg7 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("A", 600, 0, 10, 600),
    frame("B", 600, 300, 10, 600),
    "</svg>",
])
try:
    parser.build_tree(parser.parse_frames(svg7))
    fail("parser test 5.7: within-layer overlap was not detected")
except parser.ParseError:
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.7 (overlap rejection) raised unexpected: {exc}")

# 5.8 CLI exit-code contract
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

# 5.9 wrong-parent attachment must trip STRUCTURE gates while sum stays green
svg9 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("P", 400, 0, 10, 400),
    frame("C", 300, 0, 20, 300),
    "</svg>",
])
try:
    frames9 = parser.parse_frames(svg9)
    root9, total9, _ = parser.build_tree(frames9)
    c9 = [f for f in frames9 if f["name"] == "C"][0]
    p9 = [f for f in frames9 if f["name"] == "P"][0]
    all9 = root9
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

# 5.10 float-boundary regression
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
        "F3 must attach to F2; drift attached it to %s" % f3["parent"]["name"])
    viol10, _ = parser.structure_check(frames10)
    assert not viol10, f"structure violations: {viol10[:2]}"
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.10 (float-boundary parent) raised: {exc}")

# 5.11 merged-graph print-drift regression
svg11 = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1190, 0, 0, 1190),
    frame("swapper", 32.9, 1017.1, 10, 32.9),
    frame("worker", 140.1, 1049.9, 10, 140.1),
    frame("thread", 140.1, 1049.9, 20, 140.1),
    "</svg>",
])
try:
    frames11 = parser.parse_frames(svg11)
    root11, total11, _ = parser.build_tree(frames11)
    t11 = [f for f in frames11 if f["name"] == "thread"][0]
    assert t11["parent"]["name"] == "worker", (
        "thread must attach to worker; drift dropped it to %s" % t11["parent"]["name"])
    viol11, _ = parser.structure_check(frames11)
    assert not viol11, f"structure violations: {viol11[:2]}"
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.11 (print-drift parent) raised: {exc}")

# 5.11b negative self of -1 in a NORMAL graph is a violation (children sample
#      sum one past the parent): the old gate (< -1) let it pass and printed
#      no-negative-self; any negative must gate on non-merged graphs.
svg11b = "".join([
    '<svg xmlns="http://www.w3.org/2000/svg">',
    frame("all", 1000, 0, 0, 1000),
    frame("P", 10, 0, 10, 100),
    frame("C1", 6, 0, 20, 60),
    frame("C2", 5, 60, 20, 40),   # 区间 60-100 落回 P（0-100）内；6+5 == 11 > 10 -> self(P) == -1
    # 注：宽度必须 40（50 会越出 P 右边界 10px，C2 被挂到根而 self(P) 变 +4——
    # 该 fixture 曾因此假绿：断言由 sibling-overlap 满足而非负 self 门禁）
    "</svg>",
])
try:
    frames11b = parser.parse_frames(svg11b)
    root11b, total11b, _ = parser.build_tree(frames11b)
    assert not parser.is_merged_graph(frames11b), "fixture must be non-merged"
    viol11b, warn11b = parser.structure_check(frames11b)
    assert viol11b and not warn11b, (
        f"self==-1 must be a violation on normal graphs; "
        f"viol={viol11b[:1]} warn={warn11b[:1]}")
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.11b (self=-1 gate) raised: {exc}")

# 5.12 advisory downgrade requires a STRICT marker in the root region
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
    assert ok12, "integrity must stay green"
    assert parser.is_merged_graph(frames12), "strict root-region marker must classify merged"
    viol12, warn12 = parser.structure_check(frames12)
    assert not viol12 and warn12, (
        f"expected advisory warnings, got viol={viol12[:1]} warn={warn12[:1]}")
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.12 (strict-marker advisory downgrade) raised: {exc}")

# 5.13 deep strict-form marker must NOT downgrade
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
        f"deep strict-form name must hard-fail, got viol={viol13[:1]} warn={warn13[:1]}")
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.13 (deep marker attack) raised: {exc}")

# 5.14 loose substring in the root region must NOT downgrade
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
        f"loose substring must hard-fail, got viol={viol14[:1]} warn={warn14[:1]}")
    parser_tests_passed += 1
except Exception as exc:  # noqa: BLE001
    fail(f"parser test 5.14 (loose-substring attack) raised: {exc}")

# ---- 6. contract sync: case.yaml must track the parser test count ----
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
        % (m_case.group(1), PARSER_TESTS_TOTAL))
if parser_tests_passed != PARSER_TESTS_TOTAL:
    failures.append(
        "parser tests passed %d != declared total %d"
        % (parser_tests_passed, PARSER_TESTS_TOTAL))

# ---- report ----
result = {
    "skill": SKILL_DIR.name,
    "case": "skill-integrity",
    "success": not failures,
    "checks": checks,
    "failures": failures,
    "parser_tests_passed": parser_tests_passed,
    "parser_tests_total": PARSER_TESTS_TOTAL,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if not failures else 1)
