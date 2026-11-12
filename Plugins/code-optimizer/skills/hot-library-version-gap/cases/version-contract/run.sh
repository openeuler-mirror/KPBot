#!/bin/bash
# Tier-A case runner: drive check_library_version.py against a mock fs root,
# an exported maps file and a mock evidence table, then write
# results/result.json. Exit 0 only if every group is failure-free.
#
# Groups:
#   static  python3 -m py_compile syntax check
#   check   full verdict matrix (gap / no-gap / insufficient-version-detail
#           / no-entry), non-so filtering, --lib filter, custom-path
#           realpath, --output file, default knowledge wiring, exit codes
#
# Usage: bash cases/version-contract/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"
TMP="$(mktemp -d)"
trap '[ "${BASH_SUBSHELL:-0}" -eq 0 ] && rm -rf "$TMP"' EXIT

CHECK="$SKILL_DIR/scripts/check_library_version.py"

declare -i CHK_RAN=0 CHK_FAIL=0 STATIC_RAN=0 STATIC_FAIL=0

log() { echo "[case] $*"; }

check() {
    local group="$1" desc="$2" rc="$3"
    case "$group" in
        static) STATIC_RAN+=1; [ "$rc" -ne 0 ] && STATIC_FAIL+=1 ;;
        chk)    CHK_RAN+=1;     [ "$rc" -ne 0 ] && CHK_FAIL+=1 ;;
    esac
    if [ "$rc" -ne 0 ]; then log "FAIL: $desc"; else log "ok: $desc"; fi
}

# ---------------- static checks ----------------
python3 -m py_compile "$CHECK"
check static "check_library_version.py py_compile" $?

# ---------------- mock fs root + maps + knowledge ----------------
ROOT="$TMP/root"
mkdir -p "$ROOT/usr/lib64" "$ROOT/opt/vendor"

# soname-only file: major version is all realpath can reveal
: > "$ROOT/usr/lib64/libcrypto.so.3"
: > "$ROOT/opt/vendor/libssl.so.3"
# real files whose names carry the detailed version
: > "$ROOT/usr/lib64/libz.so.1.2.13"
: > "$ROOT/usr/lib64/libxml2.so.2.9.9"
# library with no entry in the mock table (nor the bundled one)
: > "$ROOT/usr/lib64/libstdc++.so.6"
# defensive shapes: no version suffix at all / non-numeric rc suffix /
# lookalike library that must NOT match the libz token by substring
: > "$ROOT/usr/lib64/libfake.so"
: > "$ROOT/usr/lib64/libfake2.so.1.2.3rc1"
: > "$ROOT/usr/lib64/libzip.so.2"
# in-place upgrade shape: disk file exists, maps records a different
# (stale) inode -> verdict must downgrade, never trust today's disk
: > "$ROOT/usr/lib64/libupgraded.so.2.0.0"

# two maps files:
#  - native: dev/inode generated from real stat() of this ROOT (same
#    filesystem view) -> exercises the verified paths under
#    --verify-inode, including poisoned upgrade/unlink rows
#  - cross: every row carries a foreign inode (maps exported on
#    another machine, root is a copied tree - the standard offline
#    analysis shape) -> default offline mode must NOT degrade these
#    to stale; gap/no-gap verdicts must keep working
python3 - "$ROOT" "$TMP/proc_maps_native.txt" "$TMP/proc_maps_cross.txt" <<'PYEOF'
import os
import sys

root, native_path, cross_path = sys.argv[1], sys.argv[2], sys.argv[3]
counter = [0]


def line(path, fake_inode=None, deleted=False, cross=False):
    counter[0] += 1
    joined = os.path.join(root, path.lstrip("/"))
    try:
        st = os.stat(joined)
        try:
            dev = "%x:%x" % (os.major(st.st_dev), os.minor(st.st_dev))
        except AttributeError:
            dev = "%x" % st.st_dev  # non-Unix: raw st_dev, format-only
        inode = st.st_ino
    except OSError:
        dev, inode = "00:00", 999999
    if fake_inode is not None:
        inode = fake_inode
    elif cross:
        inode += 500000  # foreign inode: copied-tree shape
    suffix = " (deleted)" if deleted else ""
    return "7f%012x-7f%012x r--p 00000000 %s %d %s%s" % (
        counter[0] * 0x10000, counter[0] * 0x10000 + 0x1000,
        dev, inode, path, suffix)


def rows(cross):
    counter[0] = 0

    def l(path, fake_inode=None, deleted=False):
        return line(path, fake_inode=fake_inode, deleted=deleted,
                    cross=cross)

    return [
        l("/usr/bin/somebin"),
        l("/usr/lib64/libcrypto.so.3"),
        l("/usr/lib64/libz.so.1.2.13"),
        l("/usr/lib64/libxml2.so.2.9.9"),
        l("/usr/lib64/libstdc++.so.6"),
        l("/opt/vendor/libssl.so.3"),
        l("/usr/lib64/libfake.so"),
        l("/usr/lib64/libfake2.so.1.2.3rc1"),
        l("/usr/lib64/libzip.so.2"),
        l("/opt/missing/libghost.so.1.5"),
        l("/usr/lib64/libupgraded.so.2.0.0", fake_inode=987654321),
        l("/opt/gone/libgone.so.1.5", deleted=True),
    ]


with open(native_path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(rows(cross=False)) + "\n")
with open(cross_path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(rows(cross=True)) + "\n")
PYEOF
MAPS="$TMP/proc_maps_native.txt"
MAPS_CROSS="$TMP/proc_maps_cross.txt"

KNOW="$TMP/mock_knowledge.json"
cat > "$KNOW" <<'EOF'
{
  "schema_version": "kpbot-library-versions.v1",
  "entries": [
    {"library": "openssl", "match": ["libcrypto.so", "libssl.so"],
     "arch_scope": ["aarch64"], "min_version": "3.2.0",
     "improvement": "mock entry", "source": "mock CHANGES",
     "confidence": "high", "notes": "mock"},
    {"library": "zlib", "match": ["libz.so"], "arch_scope": ["aarch64"],
     "min_version": "1.2.12", "improvement": "mock entry",
     "source": "mock ChangeLog", "confidence": "medium", "notes": "mock"},
    {"library": "libxml2", "match": ["libxml2.so"], "arch_scope": ["aarch64"],
     "min_version": "2.12.0", "improvement": "mock entry",
     "source": "mock NEWS", "confidence": "medium", "notes": "mock"},
    {"library": "libfake", "match": ["libfake.so"], "arch_scope": ["aarch64"],
     "min_version": "1.0.0", "improvement": "mock entry",
     "source": "mock", "confidence": "medium", "notes": "mock"},
    {"library": "libfake2", "match": ["libfake2.so"], "arch_scope": ["aarch64"],
     "min_version": "1.0.0", "improvement": "mock entry",
     "source": "mock", "confidence": "medium", "notes": "mock"}
  ]
}
EOF

# ---------------- check contract ----------------
while IFS= read -r line; do
    rc="${line%% *}"
    desc="${line#* }"
    check chk "$desc" "$rc"
done < <(python3 - "$CHECK" "$MAPS" "$MAPS_CROSS" "$ROOT" "$KNOW" "$TMP" <<'PYEOF'
import json
import subprocess
import sys

check, maps_native, maps_cross, root, knowledge, tmp = sys.argv[1:7]
outcomes = []


def record(desc, ok):
    outcomes.append((desc, 0 if ok else 1))


def run(maps, extra=None):
    return subprocess.run(
        [sys.executable, check, "--maps", maps, "--root", root,
         "--knowledge", knowledge] + (extra or []),
        capture_output=True, text=True)


# ---- default offline mode on the CROSS maps (foreign inodes) ----
# warn-on-mismatch: verdicts keep working, entries carry
# disk(unverified) + version_confidence=low + rerun hint ----
proc = run(maps_cross)
record("offline cross-machine mode exits 0", proc.returncode == 0)
try:
    cross = json.loads(proc.stdout)
except ValueError:
    cross = None

xlibs = {}
if cross is not None:
    for item in cross["loaded_libraries"]:
        xlibs[item["soname"]] = item

record("offline cross-machine: gap verdict still works (no stale downgrade)",
       xlibs.get("libxml2.so.2.9.9", {}).get("knowledge_match", {})
       .get("status") == "gap"
       and xlibs["libxml2.so.2.9.9"]["version_stale"] is False
       and xlibs["libxml2.so.2.9.9"]["version_source"]
       == "disk(unverified)"
       and xlibs["libxml2.so.2.9.9"].get("version_confidence") == "low"
       and "--verify-inode" in xlibs["libxml2.so.2.9.9"]
       .get("unverified_note", ""))
record("offline cross-machine: no-gap verdict still works",
       xlibs.get("libz.so.1.2.13", {}).get("knowledge_match", {})
       .get("status") == "no-gap"
       and xlibs["libz.so.1.2.13"]["version_source"]
       == "disk(unverified)")
record("offline cross-machine: poisoned upgrade row is unverified+low with hint, not silently trusted",
       xlibs.get("libupgraded.so.2.0.0", {}).get("version_stale") is False
       and xlibs["libupgraded.so.2.0.0"]["version_source"]
       == "disk(unverified)"
       and xlibs["libupgraded.so.2.0.0"].get("version_confidence") == "low"
       and xlibs["libupgraded.so.2.0.0"]["knowledge_match"]["status"]
       != "unknown-version"
       and "upgraded in place" in xlibs["libupgraded.so.2.0.0"]
       .get("unverified_note", ""))
record("offline cross-machine: tree-missing file is honestly mapped-name+stale, never 'disk'",
       xlibs.get("libghost.so.1.5", {}).get("version_stale") is True
       and xlibs["libghost.so.1.5"]["version_source"] == "mapped-name"
       and xlibs["libghost.so.1.5"]["version"] == "1.5"
       and xlibs["libghost.so.1.5"]["realpath"] is None
       and "not present in --root tree" in xlibs["libghost.so.1.5"]
       .get("stale_reason", ""))
record("offline cross-machine: deleted mapping is still stale",
       xlibs.get("libgone.so.1.5", {}).get("version_stale") is True
       and xlibs["libgone.so.1.5"]["knowledge_match"]["status"]
       == "unknown-version")

# ---- default offline mode on the NATIVE maps (same inode space):
# stat-backed rows verify as disk(verified), the poisoned upgrade row
# is unverified+low with the rerun hint (same-host upgrade detectable
# via --verify-inode, never silently swallowed) ----
proc = run(maps_native)
record("native default-offline exits 0", proc.returncode == 0)
try:
    native_off = json.loads(proc.stdout)
except ValueError:
    native_off = None

nlibs = {}
if native_off is not None:
    for item in native_off["loaded_libraries"]:
        nlibs[item["soname"]] = item

record("native same-inode-space offline: healthy rows verify as disk(verified)",
       nlibs.get("libz.so.1.2.13", {}).get("version_source")
       == "disk(verified)"
       and nlibs["libz.so.1.2.13"]["inode_verified"] is True
       and nlibs["libz.so.1.2.13"]["knowledge_match"]["status"]
       == "no-gap")
record("native same-inode-space offline: in-place-upgrade row is unverified with rerun hint (not swallowed)",
       nlibs.get("libupgraded.so.2.0.0", {}).get("version_source")
       == "disk(unverified)"
       and nlibs["libupgraded.so.2.0.0"].get("version_confidence") == "low"
       and "--verify-inode" in nlibs["libupgraded.so.2.0.0"]
       .get("unverified_note", ""))

# ---- --verify-inode on the NATIVE maps: same-host mismatch downgrades ----
proc = run(maps_native, ["--verify-inode"])
record("native + --verify-inode exits 0", proc.returncode == 0)
try:
    data = json.loads(proc.stdout)
except ValueError:
    data = None

record("check emits parseable json", data is not None)

libs = {}
if data is not None:
    for item in data["loaded_libraries"]:
        libs[item["soname"]] = item

record("11 .so entries collected, non-so binary filtered out",
       data is not None and len(libs) == 11
       and "somebin" not in " ".join(libs))
record("libxml2 2.9.9 vs min 2.12.0 is a gap with sourced improvement",
       libs.get("libxml2.so.2.9.9", {}).get("knowledge_match", {})
       .get("status") == "gap"
       and libs["libxml2.so.2.9.9"]["knowledge_match"]["source"]
       == "mock NEWS"
       and libs["libxml2.so.2.9.9"]["version_source"] == "disk(verified)"
       and libs["libxml2.so.2.9.9"]["inode_verified"] is True
       and libs["libxml2.so.2.9.9"]["version_stale"] is False)
record("gap note says the finding is not a performance gain",
       libs.get("libxml2.so.2.9.9", {}).get("knowledge_match", {})
       .get("note", "").find("not") >= 0
       and "A/B" in libs["libxml2.so.2.9.9"]["knowledge_match"]["note"])
record("libz 1.2.13 vs min 1.2.12 is no-gap",
       libs.get("libz.so.1.2.13", {}).get("knowledge_match", {})
       .get("status") == "no-gap")
record("soname-only libcrypto reports insufficient-version-detail",
       libs.get("libcrypto.so.3", {}).get("version") == "3"
       and libs["libcrypto.so.3"]["version_method"]
       == "soname-major-only"
       and libs["libcrypto.so.3"]["knowledge_match"]["status"]
       == "insufficient-version-detail")
record("vendor-path libssl resolves under /opt/vendor, not global dirs",
       libs.get("libssl.so.3", {}).get("realpath", "").replace("\\", "/")
       .startswith(root.replace("\\", "/") + "/opt/vendor/"))
record("libstdc++ with no table entry is no-entry, never fabricated",
       libs.get("libstdc++.so.6", {}).get("knowledge_match", {})
       .get("status") == "no-entry"
       and "fabricate" in libs["libstdc++.so.6"]["knowledge_match"]
       ["note"])
record("version-less soname (libfake.so) yields unknown-version, not a guess",
       libs.get("libfake.so", {}).get("version") is None
       and libs["libfake.so"]["version_method"] == "no-version-in-name"
       and libs["libfake.so"]["knowledge_match"]["status"]
       == "unknown-version")
record("non-numeric rc suffix (1.2.3rc1) is not parsed into a version",
       libs.get("libfake2.so.1.2.3rc1", {}).get("version") is None
       and libs["libfake2.so.1.2.3rc1"]["knowledge_match"]["status"]
       == "unknown-version")
record("libzip does not match the libz token (prefix semantics, no substring hit)",
       libs.get("libzip.so.2", {}).get("knowledge_match", {})
       .get("status") == "no-entry")
record("library missing from root: exists_in_root false, stale, version from mapped name",
       libs.get("libghost.so.1.5", {}).get("exists_in_root") is False
       and libs["libghost.so.1.5"]["realpath"] is None
       and libs["libghost.so.1.5"]["version"] == "1.5"
       and libs["libghost.so.1.5"]["version_source"] == "mapped-name"
       and libs["libghost.so.1.5"]["version_stale"] is True)
record("in-place upgrade (stale inode): status downgraded but table fields survive",
       libs.get("libupgraded.so.2.0.0", {}).get("version_stale") is True
       and libs["libupgraded.so.2.0.0"]["version_source"] == "mapped-name"
       and libs["libupgraded.so.2.0.0"]["version"] == "2.0.0"
       and libs["libupgraded.so.2.0.0"]["disk_version"] == "2.0.0"
       and "upgraded" in libs["libupgraded.so.2.0.0"]["stale_reason"]
       and libs["libupgraded.so.2.0.0"]["knowledge_match"]["status"]
       == "unknown-version"
       and libs["libupgraded.so.2.0.0"]["realpath"] is None)
record("unlinked mapping: '(deleted)' stripped, version from mmap-time name, verdict unknown",
       libs.get("libgone.so.1.5", {}).get("mapped_deleted") is True
       and libs["libgone.so.1.5"]["version"] == "1.5"
       and libs["libgone.so.1.5"]["version_source"] == "mapped-name"
       and libs["libgone.so.1.5"]["version_stale"] is True
       and "unlinked" in libs["libgone.so.1.5"]["stale_reason"]
       and libs["libgone.so.1.5"]["knowledge_match"]["status"]
       == "unknown-version")

proc = run(maps_native, ["--lib", "libcrypto"])
try:
    data_filter = json.loads(proc.stdout)
except ValueError:
    data_filter = None
record("--lib substring filter narrows to the requested library only",
       data_filter is not None
       and len(data_filter["loaded_libraries"]) == 1
       and data_filter["loaded_libraries"][0]["soname"]
       == "libcrypto.so.3")

out_json = tmp + "/verdict.json"
proc = run(maps_native, ["--output", out_json])
try:
    with open(out_json, encoding="utf-8") as handle:
        data_file = json.load(handle)
except (OSError, ValueError):
    data_file = None
record("--output writes parseable utf-8 json",
        data_file is not None and len(data_file["loaded_libraries"]) == 11
       and data_file["notes"])

# default knowledge wiring: no --knowledge -> bundled table answers
# libcrypto with the real openssl entry (native maps, default offline
# mode: foreign inodes are unverified, verdicts still work)
proc = subprocess.run(
    [sys.executable, check, "--maps", maps_native, "--root", root],
    capture_output=True, text=True)
try:
    data_default = json.loads(proc.stdout)
except ValueError:
    data_default = None
default_match = None
if data_default is not None:
    for item in data_default["loaded_libraries"]:
        if item["soname"] == "libcrypto.so.3":
            default_match = item["knowledge_match"]
record("bundled knowledge table wired by default (openssl entry)",
       (default_match or {}).get("library") == "openssl"
       and (default_match or {}).get("min_version") == "3.2.0"
       and (default_match or {}).get("status")
       == "insufficient-version-detail")

proc = subprocess.run([sys.executable, check], capture_output=True,
                      text=True)
record("missing input source exits 2", proc.returncode == 2)

proc = subprocess.run(
    [sys.executable, check, "--maps", tmp + "/no-such-maps"],
    capture_output=True, text=True)
record("unreadable maps exits 2", proc.returncode == 2)

proc = subprocess.run(
    [sys.executable, check, "--maps", maps_native, "--pid", "1"],
    capture_output=True, text=True)
record("--pid and --maps are mutually exclusive (exit 2)",
       proc.returncode == 2)

proc = subprocess.run([sys.executable, check, "--help"],
                      capture_output=True, text=True)
record("--help exits 0", proc.returncode == 0)

for desc, rc in outcomes:
    print("%d %s" % (rc, desc))
PYEOF
)

# ---------------- result.json ----------------
SUCCESS=false
if [ "$CHK_FAIL" -eq 0 ] && [ "$STATIC_FAIL" -eq 0 ] && [ "$CHK_RAN" -gt 0 ]; then
    SUCCESS=true
fi

python3 - "$RESULTS_DIR/result.json" "$SUCCESS" "$CHK_RAN" "$CHK_FAIL" \
    "$STATIC_RAN" "$STATIC_FAIL" <<'EOF'
import json
import sys

(out_path, success, chk_ran, chk_fail, static_ran, static_fail) = sys.argv[1:]
result = {
    "success": success == "true",
    "check_ran": int(chk_ran),
    "check_failures": int(chk_fail),
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
