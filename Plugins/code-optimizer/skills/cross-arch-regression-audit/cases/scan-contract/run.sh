#!/bin/bash
# Tier-A case runner: drive arch_diff_scan.py against a mock tree with
# planted arch divergences, then write results/result.json. Exit 0 only if
# every group is failure-free.
#
# Groups:
#   static  python3 -m py_compile syntax check
#   scan    enumeration contract on a mock tree: exact rust-cfg/c-ifdef
#           counts (incl. cfg! macro and any() nesting), keyword hits,
#           doc-file keyword exclusion (+ opt-in), clean-file zero hit,
#           .git skip, --output utf-8 file, exit codes
#
# Usage: bash cases/scan-contract/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"
TMP="$(mktemp -d)"
trap '[ "${BASH_SUBSHELL:-0}" -eq 0 ] && rm -rf "$TMP"' EXIT

SCAN="$SKILL_DIR/scripts/arch_diff_scan.py"

declare -i SCAN_RAN=0 SCAN_FAIL=0 STATIC_RAN=0 STATIC_FAIL=0

log() { echo "[case] $*"; }

# check <group> <desc> <rc>
check() {
    local group="$1" desc="$2" rc="$3"
    case "$group" in
        static) STATIC_RAN+=1; [ "$rc" -ne 0 ] && STATIC_FAIL+=1 ;;
        scan)   SCAN_RAN+=1;   [ "$rc" -ne 0 ] && SCAN_FAIL+=1 ;;
    esac
    if [ "$rc" -ne 0 ]; then log "FAIL: $desc"; else log "ok: $desc"; fi
}

# ---------------- static checks ----------------
python3 -m py_compile "$SCAN"
check static "arch_diff_scan.py py_compile" $?

# ---------------- mock tree with planted divergences ----------------
MOCK="$TMP/mock"
mkdir -p "$MOCK/src" "$MOCK/include" "$MOCK/.git"

cat > "$MOCK/src/console.rs" <<'EOF'
// mock: default console per arch
pub fn default_cmdline() -> String {
    #[cfg(target_arch = "x86_64")]
    { "console=hvc0".to_string() }
    #[cfg(target_arch = "aarch64")]
    { "console=ttyAMA0,115200".to_string() }
}

pub fn is_x86() -> bool {
    cfg!(target_arch = "x86_64")
}

#[cfg(any(target_arch = "aarch64", target_arch = "riscv64"))]
fn wide_portable() {}

#[cfg(all(target_os = "linux", target_arch = "aarch64"))]
fn linux_arm_only() {}

#[cfg(all(any(target_pointer_width = "64"), target_arch = "aarch64"))]
fn deep_nesting() {}
EOF

cat > "$MOCK/include/arch.h" <<'EOF'
/* mock arch defaults */
#ifdef __x86_64__
#define DEFAULT_CONSOLE_HZ 250
#endif
#if defined(__aarch64__)
#define DEFAULT_CONSOLE_HZ 100
#endif
#ifdef __arm__
#define DEFAULT_CONSOLE_HZ 128
#endif
#if defined(__x86_64__) || defined(__aarch64__)
#define MULTI_ARCH_GUARD 1
#endif
EOF

cat > "$MOCK/src/clean.rs" <<'EOF'
pub fn add(a: u32, b: u32) -> u32 {
    a.wrapping_add(b)
}
EOF

# pure doc file: keyword must be excluded by default, included with --include-docs
cat > "$MOCK/NOTES.md" <<'EOF'
# mock notes
The default console differs per arch in this mock tree.
EOF

# must be skipped by SKIP_DIRS
cat > "$MOCK/.git/hidden.rs" <<'EOF'
#[cfg(target_arch = "x86_64")]
fn hidden() {}
EOF

# root-ancestor named like a build dir: the whole tree must NOT be skipped
ANCESTOR="$TMP/build/ancestor-mock"
mkdir -p "$ANCESTOR/src"
cat > "$ANCESTOR/src/lib.rs" <<'EOF'
#[cfg(target_arch = "aarch64")]
fn arm_path() {}
EOF

# empty tree: must exit 3 with a warning instead of a silent empty success
EMPTY="$TMP/empty-tree"
mkdir -p "$EMPTY"

# ---------------- scan contract ----------------
# All numeric assertions run in one python block reading the scanner output;
# each assertion outcome is reported back so check() can count it.
while IFS= read -r line; do
    rc="${line%% *}"
    desc="${line#* }"
    check scan "$desc" "$rc"
done < <(python3 - "$SCAN" "$MOCK" "$TMP" <<'PYEOF'
import json
import subprocess
import sys

scan, mock, tmp = sys.argv[1], sys.argv[2], sys.argv[3]
outcomes = []


def record(desc, ok):
    outcomes.append((desc, 0 if ok else 1))


def run_scan(root, extra=None):
    return subprocess.run(
        [sys.executable, scan, "--root", root] + (extra or []),
        capture_output=True, text=True)


def by_file_check(findings):
    names = {}
    for item in findings:
        names[item["file"]] = names.get(item["file"], 0) + 1
    return names


proc = run_scan(mock, ["--keyword", "console"])
record("scan exits 0 on mock tree", proc.returncode == 0)
try:
    data = json.loads(proc.stdout)
except ValueError:
    data = None
record("scan emits parseable json", data is not None)
if data is not None:
    findings = data["findings"]
    by_kind = {}
    by_arch = {}
    by_snippet = []
    for item in findings:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_arch[item["arch"]] = by_arch.get(item["arch"], 0) + 1
        by_snippet.append(item["snippet"])
    record("files_scanned counts 4 (mock files, .git skipped)",
           data["files_scanned"] == 4)
    record("rust-cfg findings == 7 (attrs + cfg! + any per-arch + all + deep nest)",
           by_kind.get("rust-cfg", 0) == 7)
    record("c-ifdef findings == 5 (three singles + one dual-arch line)",
           by_kind.get("c-ifdef", 0) == 5)
    dual = [f for f in findings
            if f["kind"] == "c-ifdef" and "||" in f["snippet"]]
    record("multi-arch conditional line reports both sides (search dropped the second)",
           len(dual) == 2
           and {d["arch"] for d in dual} == {"x86_64", "aarch64"}
           and dual[0]["file"] == "include/arch.h")
    record("keyword findings == 3 (docs excluded by default)",
           by_kind.get("keyword", 0) == 3)
    record("arch tally x86_64==4 aarch64==6 arm==1 riscv==1",
           by_arch.get("x86_64") == 4 and by_arch.get("aarch64") == 6
           and by_arch.get("arm") == 1 and by_arch.get("riscv") == 1)
    record("any() emits one finding per listed arch (riscv counted)",
           by_arch.get("riscv") == 1)
    record("deep nesting with closing paren before target_arch captured",
           any("target_pointer_width" in s for s in by_snippet))
    record("clean file yields zero findings", "src/clean.rs" not in by_file_check(findings))
    record(".git/hidden.rs skipped by SKIP_DIRS",
           ".git/hidden.rs" not in by_file_check(findings))
    record("NOTES.md yields no keyword hit by default",
           "NOTES.md" not in by_file_check(findings))
    record("every finding carries file/line/kind/arch/snippet",
           all(set(item) >= {"file", "line", "kind", "arch", "snippet"}
               for item in findings))

proc = run_scan(mock, ["--keyword", "console", "--include-docs"])
try:
    data_docs = json.loads(proc.stdout)
except ValueError:
    data_docs = None
record("--include-docs lifts keyword hits to 4 (NOTES.md counted)",
       data_docs is not None
       and sum(1 for f in data_docs["findings"] if f["kind"] == "keyword") == 4)

out_json = tmp + "/out.json"
proc = run_scan(mock, ["--output", out_json])
try:
    with open(out_json, encoding="utf-8") as handle:
        data_file = json.load(handle)
except (OSError, ValueError):
    data_file = None
record("--output writes parseable utf-8 json with same scan result",
       data_file is not None and data_file.get("files_scanned") == 4)

out_tsv = tmp + "/out.tsv"
proc = run_scan(mock, ["--format", "tsv", "--output", out_tsv])
try:
    with open(out_tsv, encoding="utf-8") as handle:
        tsv_lines = handle.read().splitlines()
except OSError:
    tsv_lines = []
record("tsv --output writes header + findings",
       len(tsv_lines) >= 5 and tsv_lines[0] == "file\tline\tkind\tarch\tsnippet")

# root whose ANCESTOR is named like a build dir: tree must still be scanned
ancestor = tmp + "/build/ancestor-mock"
proc = run_scan(ancestor)
try:
    data_anc = json.loads(proc.stdout)
except ValueError:
    data_anc = None
record("root ancestor named 'build' does not skip the tree (rel-path SKIP_DIRS)",
       data_anc is not None and data_anc["files_scanned"] == 1
       and len(data_anc["findings"]) == 1
       and data_anc["findings"][0]["arch"] == "aarch64")

# empty tree: exit 3 with an explicit warning, never a silent empty success
proc = run_scan(tmp + "/empty-tree")
record("empty tree exits 3 with warning (no silent 'no divergence')",
       proc.returncode == 3 and "0 files scanned" in proc.stderr)

proc = run_scan(mock + "/no-such-dir")
record("bad --root exits non-zero", proc.returncode != 0)

proc = subprocess.run([sys.executable, scan, "--help"],
                      capture_output=True, text=True)
record("--help exits 0", proc.returncode == 0)

for desc, rc in outcomes:
    print("%d %s" % (rc, desc))
PYEOF
)

# ---------------- result.json ----------------
SUCCESS=false
if [ "$SCAN_FAIL" -eq 0 ] && [ "$STATIC_FAIL" -eq 0 ] && [ "$SCAN_RAN" -gt 0 ]; then
    SUCCESS=true
fi

python3 - "$RESULTS_DIR/result.json" "$SUCCESS" "$SCAN_RAN" "$SCAN_FAIL" \
    "$STATIC_RAN" "$STATIC_FAIL" <<'EOF'
import json
import sys

(out_path, success, scan_ran, scan_fail, static_ran, static_fail) = sys.argv[1:]
result = {
    "success": success == "true",
    "scan_ran": int(scan_ran),
    "scan_failures": int(scan_fail),
    "static_ran": int(static_ran),
    "static_failures": int(static_fail),
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
EOF

log "result: $RESULTS_DIR/result.json"
if [ "$SUCCESS" = true ]; then
    exit 0
fi
exit 1
