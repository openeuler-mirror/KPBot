#!/usr/bin/env python3
"""Check which shared libraries a process actually loads, extract their
versions, and match them against a sourced evidence table of
"optimization introduced in version X" entries.

Read-only diagnosis for the hot-library-version-gap skill: it answers
"the hotspot lives in libcrypto - is the version we actually load
missing known upstream improvements?" It never upgrades anything and
never claims a performance gain.

Inputs (mutually exclusive): --pid reads /proc/<PID>/maps online;
--maps reads an exported maps file offline. --root prefixes the .so
paths (default /) so an exported filesystem tree can be inspected;
realpath chains are resolved to extract trailing version segments.

The mapped dev/inode from maps is verified against the file currently
sitting at the path **only when maps and --root share the same
filesystem view**: on by default for --pid (live process), and opt-in
via --verify-inode for --maps. An exported maps file combined with a
copied/backup/chroot root is a different filesystem view whose inodes
never match, so offline mode defaults to no verification - the entry
carries inode_verified=false instead of a stale downgrade, and the
gap/no-gap verdicts keep working. When verification is on and the file
was upgraded underneath the process or unlinked ("(deleted)" suffix),
the version falls back to the name recorded at mmap time with
``version_stale=true``, ``version_source="mapped-name"`` and only the
knowledge *status* is downgraded - the sourced table fields
(library/min_version/improvement/source) stay visible for evidence.

Version extraction is honest about what it can know: a soname like
libcrypto.so.3 yields only the major version, which is reported as
"insufficient-version-detail" instead of being compared against a
detailed min_version. Unknown libraries get "no-entry", pointing at
the upstream-search methodology, never a fabricated "probably
optimized in X".

Output: JSON {"loaded_libraries": [{soname, realpath, version,
version_method, knowledge_match}], "notes": [...]} to stdout or
--output (UTF-8). Exit 0 on a completed check; 2 on bad arguments or
unreadable input. Python 3 standard library only.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_KNOWLEDGE = Path(__file__).resolve().parent.parent \
    / "references" / "library-versions.json"

SO_NAME_RE = re.compile(r"\.so(\.[0-9]+)*$")
SO_PATH_RE = re.compile(r"\.so(?:\.|$)")


def parse_version(text):
    """'3.1.4' -> (3, 1, 4); '3' -> (3,); None -> None."""
    if not text:
        return None
    parts = text.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def extract_version_from_name(basename):
    """Return (version_string or None, method) from a .so filename."""
    match = SO_NAME_RE.search(basename)
    if not match:
        return None, "no-version-in-name"
    trailing = match.group(0)[len(".so"):]
    if not trailing:
        return None, "no-version-in-name"
    version = trailing.lstrip(".")
    if parse_version(version) is None:
        return None, "no-version-in-name"
    method = ("realpath-suffix" if len(parse_version(version)) > 1
              else "soname-major-only")
    return version, method


def load_maps_lines(args):
    if args.pid:
        maps_path = "/proc/%s/maps" % args.pid
        try:
            with open(maps_path, "r", encoding="utf-8",
                      errors="replace") as handle:
                return handle.read().splitlines(), "pid"
        except OSError as exc:
            print("error: cannot read %s: %s" % (maps_path, exc),
                  file=sys.stderr)
            sys.exit(2)
    try:
        with open(args.maps, "r", encoding="utf-8",
                  errors="replace") as handle:
            return handle.read().splitlines(), "maps-file"
    except OSError as exc:
        print("error: cannot read %s: %s" % (args.maps, exc),
              file=sys.stderr)
        sys.exit(2)


def so_paths_from_maps(lines):
    """Extract ordered, deduplicated (path, dev, inode) tuples from maps.

    The mapped dev ("fd:01" hex major:minor) and inode are kept so the
    caller can verify that the file currently sitting at that path is
    still the one the process actually mapped - a library upgraded
    underneath a running process (dnf update without restart) or an
    unlinked mapping must never be reported as "what the process
    actually loads" from today's disk state alone.
    """
    seen = set()
    ordered = []
    for line in lines:
        fields = line.split(None, 5)
        if len(fields) < 6 or not fields[5].startswith("/"):
            continue
        path = fields[5].strip()
        deleted = path.endswith(" (deleted)")
        if deleted:
            path = path[:-len(" (deleted)")]
        if not SO_PATH_RE.search(path):
            continue
        key = (path, fields[3], fields[4])
        if key not in seen:
            seen.add(key)
            ordered.append((path, fields[3], fields[4], deleted))
    return ordered


def _stat_matches(root, path, dev, inode):
    """True when the file at root+path is still the mapped inode.

    The inode is the primary, platform-independent key; the mapped dev
    ("fd:01" hex major:minor) is a Linux-only secondary check - when
    os.makedev is unavailable (non-Unix host doing offline --maps
    analysis) the inode match alone decides.
    """
    joined = os.path.join(root, path.lstrip("/"))
    try:
        st = os.stat(joined)
    except OSError:
        return False
    try:
        mapped_inode = int(inode)
    except ValueError:
        return False
    if st.st_ino != mapped_inode:
        return False
    major_hex, _, minor_hex = dev.partition(":")
    try:
        mapped_dev = os.makedev(int(major_hex, 16), int(minor_hex, 16))
    except (ValueError, AttributeError):
        return True  # dev unverifiable on this platform; inode matched
    return st.st_dev == mapped_dev


def match_knowledge(soname, version, entries):
    """Return a knowledge_match dict for one loaded library."""
    base = os.path.basename(soname)
    matched = None
    for entry in entries:
        for token in entry.get("match", []):
            # prefix semantics: "libcrypto.so" matches libcrypto.so.3 but
            # never a library merely containing the substring elsewhere
            if base.startswith(token):
                matched = entry
                break
        if matched:
            break
    if matched is None:
        return {
            "status": "no-entry",
            "note": "no sourced entry in the evidence table; follow the "
                    "upstream-search methodology in SKILL.md, or accept "
                    "unknown - never fabricate an optimization version",
        }
    min_version = matched.get("min_version", "")
    current = parse_version(version)
    minimum = parse_version(min_version)
    result = {
        "status": None,
        "library": matched.get("library"),
        "min_version": min_version,
        "improvement": matched.get("improvement"),
        "source": matched.get("source"),
        "arch_scope": matched.get("arch_scope"),
        "confidence": matched.get("confidence"),
        "notes": matched.get("notes"),
    }
    if current is None or minimum is None:
        result["status"] = "unknown-version"
        result["note"] = "current version could not be determined for " \
                         "comparison"
        return result
    if len(current) == 1:
        result["status"] = "insufficient-version-detail"
        result["note"] = ("soname only carries the major version (%s); "
                          "the detailed version decides - resolve it via "
                          "the package manager, then re-compare"
                          % version)
        return result
    result["current_version"] = version
    result["status"] = ("gap" if current < minimum else "no-gap")
    if current < minimum:
        result["note"] = ("loaded version is older than the sourced "
                          "improvement version; verify by upgrading and "
                          "re-running an A/B - this finding alone is not "
                          "a performance gain")
    else:
        result["note"] = ("loaded version is not older than the sourced "
                          "improvement version; distro backports can "
                          "decouple package and upstream versions")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check actually-loaded library versions against a "
                    "sourced evidence table.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pid",
                        help="online: read /proc/<PID>/maps")
    source.add_argument("--maps",
                        help="offline: read an exported maps file")
    parser.add_argument("--root", default="/",
                        help="filesystem root prefixing .so paths "
                             "(default /; use a chroot/backup root to "
                             "inspect offline trees)")
    parser.add_argument("--lib", action="append", default=[],
                        help="case-sensitive substring filter on library "
                             "names (repeatable)")
    parser.add_argument("--knowledge",
                        help="evidence table json (default: bundled "
                             "references/library-versions.json)")
    parser.add_argument("--verify-inode", action="store_true",
                        help="verify the mapped dev/inode against the "
                              "file currently at the path. On by default "
                              "for --pid (same filesystem view); off by "
                              "default for --maps, because an exported "
                              "maps file plus a copied/chroot/backup "
                              "root belongs to a DIFFERENT filesystem "
                              "view whose inodes never match - enabling "
                              "it there degrades every library to stale")
    parser.add_argument("--output",
                        help="write result json to this file as UTF-8")
    args = parser.parse_args(argv)

    knowledge_path = Path(args.knowledge) if args.knowledge \
        else DEFAULT_KNOWLEDGE
    try:
        with open(knowledge_path, encoding="utf-8") as handle:
            knowledge = json.load(handle)
    except (OSError, ValueError) as exc:
        print("error: cannot read knowledge table %s: %s"
              % (knowledge_path, exc), file=sys.stderr)
        return 2
    entries = knowledge.get("entries") or []

    lines, source_mode = load_maps_lines(args)
    paths = so_paths_from_maps(lines)
    if args.lib:
        paths = [p for p in paths
                 if any(token in os.path.basename(p[0])
                        for token in args.lib)]

    # the property that matters is whether maps and --root share the
    # same inode space, not the input source: --pid on the live root
    # shares it, an exported maps file may or may not. Verification is
    # always computed; whether a mismatch DOWNGRADES (stale) or merely
    # WARNS (unverified) depends on the mode (review !115 P2 round 3)
    pid_default_root = (source_mode == "pid" and args.root == "/")
    force_verify = (args.verify_inode
                    or (source_mode == "pid" and pid_default_root))

    loaded = []
    for path, mapped_dev, mapped_inode, deleted in paths:
        joined = os.path.join(args.root, path.lstrip("/"))
        real = os.path.realpath(joined)
        exists = os.path.exists(real)
        if deleted or not exists:
            # a deleted mapping or a file absent from the tree: the only
            # trustworthy name is the one recorded at mmap time, and
            # calling it "disk" would contradict realpath=null /
            # exists_in_root=false in the same entry
            version, method = extract_version_from_name(
                os.path.basename(path))
            source = "mapped-name"
            stale = True
            inode_verified = (not deleted
                              and _stat_matches(args.root, path,
                                                mapped_dev, mapped_inode))
        else:
            matched = _stat_matches(args.root, path, mapped_dev,
                                    mapped_inode)
            inode_verified = matched
            version, method = extract_version_from_name(
                os.path.basename(real))
            if matched:
                source = "disk(verified)"
                stale = False
            else:
                version, method = extract_version_from_name(
                    os.path.basename(real))
                if force_verify:
                    # same inode space: a mismatch means the file was
                    # upgraded underneath the process - downgrade
                    version, method = extract_version_from_name(
                        os.path.basename(path))
                    source = "mapped-name"
                    stale = True
                else:
                    # unknown inode space (cross-machine copy or same-host
                    # upgrade): keep the disk-name verdict, warn loudly
                    source = "disk(unverified)"
                    stale = False
        entry = {
            "soname": os.path.basename(path),
            "mapped_path": path,
            "mapped_dev": mapped_dev,
            "mapped_inode": mapped_inode,
            "mapped_deleted": deleted,
            "realpath": (None if (stale or not exists) else real),
            "exists_in_root": exists,
            "inode_verified": inode_verified,
            "version": version,
            "version_method": method,
            "version_source": source,
            "version_stale": stale,
        }
        if source == "disk(unverified)":
            entry["version_confidence"] = "low"
            entry["unverified_note"] = (
                "the tree file does not match the mapped inode: either "
                "maps/root come from different machines (inodes never "
                "match across copies), or the library was upgraded in "
                "place - for same-host analysis rerun with --verify-inode "
                "to get a stale downgrade")
        match = match_knowledge(os.path.basename(path), version, entries)
        if stale:
            entry["disk_version"] = (extract_version_from_name(
                os.path.basename(real))[0] if exists else None)
            entry["stale_reason"] = (
                "mapped file was unlinked" if deleted else
                ("file not present in --root tree" if not exists else
                 "file at this path no longer matches the mapped inode "
                 "(library upgraded underneath the running process?)"))
            # keep the sourced table fields (library/min_version/
            # improvement/source) for downstream evidence - only the
            # status is downgraded, the table hit itself is not dropped
            match["status"] = "unknown-version"
            match["stale_note"] = ("version trust is downgraded: the "
                                   "process executes the inode recorded "
                                   "in maps, which differs from the "
                                   "current disk state - upgrade or "
                                   "restart before concluding anything")
        entry["knowledge_match"] = match
        loaded.append(entry)

    verdict = {
        "source_mode": source_mode,
        "root": args.root,
        "inode_verification": ("downgrade-on-mismatch" if force_verify
                               else "warn-on-mismatch (mismatch alone does "
                                    "not distinguish a cross-machine copy "
                                    "from an in-place upgrade; rerun with "
                                    "--verify-inode on the same host to "
                                    "get the stale downgrade)"),
        "loaded_libraries": loaded,
        "notes": [
            "versions come from the process maps and realpath suffixes, "
            "never from a global command like 'openssl version'",
            "a 'gap' status is a diagnosis, not a performance gain: "
            "upgrade and re-run an A/B to claim improvement",
            "no-entry means the snapshot table lacks this library; "
            "search upstream sources before concluding anything",
        ],
    }

    payload = json.dumps(verdict, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
