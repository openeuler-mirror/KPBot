#!/usr/bin/env python3
"""Tier-A integrity checks for the go-protobuf-fixed-slice-bulk-copy skill.

Validates that the skill stays well-formed and self-consistent as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description present
      and within the 1024-character limit
  2. every references/*.md link and the cases/skill-integrity dir in SKILL.md
      resolves on disk
  3. key methodology invariants stay documented (byte-probe endian gate, the
      NativeEndian comparison pitfall in both spellings, minimum Go 1.20,
      generated-code/generate-types template rule, malformed-payload fallback,
      exact preallocation, dest-side reinterpretation discipline, NaN payload
      preservation incl. Float64bits, error semantics, honest speedup claims)
  4. forbidden anti-patterns absent (interface comparison as the endian gate
      in every spelling: `=`, `:=`, parenthesized, and the bare
      NativeEndian/LittleEndian equality comparison)
  5. LE wire semantics and size math recomputed from first principles, with
      the varint length formula cross-checked against a reference encoder and
      a hard-coded protowire expectation table, and the host byte order
      asserted through a host-dependent probe model (sys.byteorder)
  6. seven negative self-tests: mutations must be caught (checks are
      non-vacuous) — frontmatter rename, anchor removal, three endian-gate
      spellings, bare NativeEndian comparison, probe index mutation
  7. no sensitive content (private IPs, credentials, personal home paths for
      Linux and macOS); scans SKILL.md, references/*.md and every case
      script under cases/

Writes results/result.json; exits non-zero on any failure.
"""

import json
import re
import struct
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
CASE_DIR = SKILL_DIR / "cases" / "skill-integrity"
CASES_ROOT = SKILL_DIR / "cases"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

MAX_DESCRIPTION = 1024

# Methodology anchors that must stay in the corpus (SKILL.md + references/*.md).
ANCHORS = [
    "(*[4]byte)(unsafe.Pointer(&x))[0] == 1",  # byte-probe endian gate
    "binary.NativeEndian",                     # the comparison pitfall stays documented
    "unsafe.SliceData",                        # bulk reinterpretation primitive
    "growFloat32Slice",                        # exact preallocation is part of the template
    "目的侧",                                  # dest-side reinterpretation discipline
    "errDecode",                               # malformed-length semantics preservation
    "errUnknown",                              # wire-type semantics preservation
    "NaN payload",                             # bit-exactness requirement
    "Float64bits",                             # fixed64-family 64-bit NaN payload verification
    "1.3~3.4",                                 # honest unmarshal speedup claim
    "12.5x",                                   # measured marshal speedup evidence
    "1.20",                                    # minimum Go version gate (unsafe.SliceData)
    "go.mod",                                  # the version gate is checked on go.mod
    "DO NOT EDIT",                             # generated-code detection marker
    "generate-types",                          # generator template must be patched in sync
    "畸形载荷",                                 # malformed payloads must fall back to the original path
]

# Anti-patterns that must never appear in the corpus. The always-false endian
# gate can be spelled many ways; each rule below covers one spelling family:
#   - assignment of a `binary.*` expression to the gate, covering `=` and `:=`
#     plus optional parentheses after the operator
#   - a bare NativeEndian/LittleEndian equality comparison (in an if gate or
#     wrapped by any other expression)
FORBIDDEN = [
    (re.compile(r"isLittleEndian\s*:?\=\s*\(?\s*binary\."),
     "binary.* expression assigned to the endian gate"),
    (re.compile(r"binary\.NativeEndian\s*==\s*binary\.LittleEndian"),
     "bare NativeEndian/LittleEndian equality comparison as endian gate"),
]

# Sensitive content guards (same policy as the sibling skill-integrity case).
# The personal-home rules cover both Linux (/home) and macOS (/Users) shapes.
SENSITIVE = [
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b10\.\d+\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "personal home path"),
    (re.compile(r"(?i)/users/[a-z][a-z0-9_.-]+/"), "personal home path (macOS)"),
    (re.compile(r"[Cc]:\\Users\\[a-z][a-z0-9_.-]+\\"), "personal home path"),
]


def check_frontmatter(text, dir_name):
    """Return a list of problems with the frontmatter of `text`."""
    problems = []
    fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not fm_match:
        return ["SKILL.md has no frontmatter block"]
    fm = fm_match.group(1)
    name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
    if not name_match:
        problems.append("frontmatter: missing name")
    elif name_match.group(1) != dir_name:
        problems.append(f"frontmatter: name {name_match.group(1)!r} != dir {dir_name!r}")
    desc_match = re.search(r"^description:\s*(.+?)(?=\n\w+:|\Z)", fm, re.MULTILINE | re.DOTALL)
    if not desc_match:
        problems.append("frontmatter: missing/empty description")
    else:
        desc = desc_match.group(1).strip()
        if not desc:
            problems.append("frontmatter: missing/empty description")
        elif len(desc) > MAX_DESCRIPTION:
            problems.append(f"frontmatter: description {len(desc)} chars exceeds {MAX_DESCRIPTION}")
    return problems


def corpus_of(text, refs_dir):
    parts = [text]
    for p in sorted(refs_dir.glob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    return text + "\n" + "\n".join(parts)


def check_anchors(corpus, anchors):
    return [f"methodology anchor missing from corpus: {a!r}" for a in anchors if a not in corpus]


def check_forbidden(corpus, forbidden):
    findings = []
    for pattern, kind in forbidden:
        m = pattern.search(corpus)
        if m:
            findings.append(f"forbidden anti-pattern ({kind}): {m.group(0)!r}")
    return findings


def case_files(cases_root):
    """Case scripts under cases/ (case.yaml, run.sh, *.py, *.go, go.mod) — the
    files most likely to embed IPs/accounts/endpoints, and therefore part of
    the sensitive-content scan scope."""
    suffixes = {".yaml", ".yml", ".sh", ".py", ".go", ".mod"}
    return sorted(p for p in cases_root.rglob("*")
                  if p.is_file() and p.suffix in suffixes)


def check_sensitive(paths):
    findings = []
    for src in paths:
        body = src.read_text(encoding="utf-8")
        for pattern, kind in SENSITIVE:
            for m in pattern.finditer(body):
                findings.append(f"{src.name}: {kind}: {m.group(0)!r}")
    return findings


def _varint_size(x):
    """Reference varint length: 7 bits per byte, high bit = continuation."""
    n = 1
    while x >= 128:
        x >>= 7
        n += 1
    return n


# protowire.SizeVarint expectations for packed fixed32 payloads of 4*n bytes
# (n = 1, 16, 256, 4096, 65536). Hard-coded so a broken formula or a broken
# reference implementation cannot self-confirm.
EXPECTED_VARINT = {4: 1, 64: 1, 1024: 2, 16384: 3, 262144: 3}


def check_numeric():
    """Recompute the LE wire semantics and size math the skill relies on."""
    problems = []
    # fixed32 wire order is little-endian: 0x01020304 -> 04 03 02 01
    if struct.pack("<I", 0x01020304) != b"\x04\x03\x02\x01":
        problems.append("numeric: fixed32 little-endian byte order violated")
    # float32 1.5 has bit pattern 0x3FC00000, wire bytes 00 00 C0 3F
    if struct.pack("<f", 1.5) != b"\x00\x00\xc0\x3f":
        problems.append("numeric: float32 1.5 LE encoding mismatch")
    # packed payload size = 4*n bytes, varint length prefix = ceil(bits/7) bytes.
    # The formula must agree with BOTH the reference encoder and the table.
    for n in (1, 16, 256, 4096, 65536):
        payload = 4 * n
        varint_len = (payload.bit_length() + 6) // 7
        if payload % 4 != 0:
            problems.append(f"numeric: packed payload {payload} not 4-byte aligned for n={n}")
        if _varint_size(payload) != EXPECTED_VARINT[payload]:
            problems.append(
                f"numeric: varint reference {_varint_size(payload)} != "
                f"protowire expectation {EXPECTED_VARINT[payload]} for n={n}")
        if varint_len != _varint_size(payload):
            problems.append(
                f"numeric: varint formula {varint_len} != reference "
                f"{_varint_size(payload)} for n={n}")
    # byte-probe semantics, modeled host-dependently: the first in-memory byte
    # of uint32 1 (what the Go probe (*[4]byte)(unsafe.Pointer(&x))[0] would
    # read on this host) is 0x01 exactly on little-endian machines. sys.byteorder
    # ties the model to the host instead of the constant "little", so a
    # big-endian host reports a finding instead of a vacuous pass. The probe's
    # textual shape is separately guarded by the probe anchor + N5.
    if int(1).to_bytes(4, sys.byteorder)[0] != 1:
        problems.append("numeric: probe model says this host is big-endian; LE fast path inapplicable")
    return problems


def check_links(text, skill_dir):
    problems = []
    checked = 0
    for target in re.findall(r"\]\((references/[^)]+)\)", text):
        checked += 1
        if not (skill_dir / target).is_file():
            problems.append(f"broken reference link: {target}")
    if checked < 2:
        problems.append(f"expected at least 2 reference links, found {checked}")
    for needed in ("case.yaml", "run.sh", "checks.py"):
        if not (skill_dir / "cases" / "skill-integrity" / needed).is_file():
            problems.append(f"case file missing: cases/skill-integrity/{needed}")
    return problems, checked


# N3 spellings of the forbidden always-false endian gate. Equivalent Go forms
# that the original single-shape guard missed; every one of them must be caught.
N3_VARIANTS = [
    "\nisLittleEndian = binary.LittleEndian\n",                      # classic `=`
    "\nisLittleEndian := binary.LittleEndian\n",                     # short declaration
    "\nisLittleEndian = (binary.NativeEndian == binary.LittleEndian)\n",  # parenthesized
]
# N4: the bare comparison outside an assignment (e.g. inside an if gate).
N4_VARIANT = "\nif binary.NativeEndian == binary.LittleEndian {\n"


def run_negative_selftest():
    """Seven mutations must be caught; a miss means a check is vacuous."""
    caught = []
    good_text = SKILL_MD.read_text(encoding="utf-8")
    corpus = corpus_of(good_text, REFS_DIR)
    # N1: rename frontmatter name away from the directory name
    mutated = re.sub(r"^name: go-protobuf-fixed-slice-bulk-copy$",
                     "name: go-protobuf-bulk-copy", good_text, count=1, flags=re.MULTILINE)
    if check_frontmatter(mutated, SKILL_DIR.name):
        caught.append("N1 frontmatter rename")
    # N2: drop the dest-side reinterpretation anchor from the corpus
    if check_anchors(corpus.replace("目的侧", "源侧"), ANCHORS):
        caught.append("N2 anchor removal")
    # N3: forbidden endian gate in three equivalent spellings — all must be caught
    n3_misses = [v for v in N3_VARIANTS if not check_forbidden(corpus + v, FORBIDDEN)]
    if not n3_misses:
        caught.append("N3 forbidden gate injection (3 spellings)")
    # N4: bare NativeEndian/LittleEndian comparison in an if gate
    if check_forbidden(corpus + N4_VARIANT, FORBIDDEN):
        caught.append("N4 bare NativeEndian comparison")
    # N5: probe index mutation ([0] -> [3]) must break the probe anchor
    bad_probe = corpus.replace("(*[4]byte)(unsafe.Pointer(&x))[0] == 1",
                               "(*[4]byte)(unsafe.Pointer(&x))[3] == 1")
    if check_anchors(bad_probe, ANCHORS):
        caught.append("N5 probe index mutation")
    return caught, 5


def main():
    failures = []

    text = SKILL_MD.read_text(encoding="utf-8")
    failures += check_frontmatter(text, SKILL_DIR.name)

    link_problems, links_checked = check_links(text, SKILL_DIR)
    failures += link_problems

    corpus = corpus_of(text, REFS_DIR)
    anchors_found = sum(1 for a in ANCHORS if a in corpus)
    failures += check_anchors(corpus, ANCHORS)

    forbidden_findings = check_forbidden(corpus, FORBIDDEN)
    failures += forbidden_findings

    numeric_problems = check_numeric()
    failures += numeric_problems

    sensitive_findings = check_sensitive([SKILL_MD, *sorted(REFS_DIR.glob("*.md")), *case_files(CASES_ROOT)])
    if sensitive_findings:
        failures.append(f"sensitive content found ({len(sensitive_findings)} hits)")

    negative_caught, negative_total = run_negative_selftest()
    if len(negative_caught) < negative_total:
        failures.append(
            f"negative selftest: only {len(negative_caught)}/{negative_total} mutations caught "
            f"({', '.join(negative_caught)}) — a check is vacuous")

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "skill": SKILL_DIR.name,
        "checks": {
            "frontmatter": "ok" if not any("frontmatter" in f for f in failures) else "fail",
            "reference_links": {"checked": links_checked,
                                "status": "ok" if not link_problems else "fail"},
            "methodology_anchors": {"found": anchors_found, "total": len(ANCHORS)},
            "forbidden_patterns": {"hits": len(forbidden_findings)},
            "numeric_recompute": "ok" if not numeric_problems else "fail",
            "negative_selftest": {"caught": negative_caught,
                                  "total": negative_total},
            "sensitive_content": {"hits": len(sensitive_findings)},
        },
        "failures": failures,
        "sensitive_findings": sensitive_findings,
        "passed": not failures,
    }
    RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
    if failures:
        print(f"FAILED: {len(failures)} issue(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    print("skill-integrity: all checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
