#!/usr/bin/env python3
"""Heuristic version-delta audit between two engine source trees (CMake-like builds).

Scans CMake options, C/C++ macro inventory and public headers. It is an audit
aid, not proof: every finding must be re-checked with the engine source-analysis skill on
the actual commit.

Typical use:
    python3 scan_version_delta.py --source /path/to/old_engine --target /path/to/new_engine \
      --json version-delta.json
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "build",
    "_build",
    "cmake-build-debug",
    "cmake-build-release",
    "__pycache__",
    "node_modules",
    ".cache",
    ".idea",
    ".vscode",
}

C_EXT = {
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inl",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".cu",
}

OPTION_RE = re.compile(r"option\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s+\"[^\"]*\"(?:\s+(ON|OFF))?\s*\)", re.S)
DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.M)
IFDEF_RE = re.compile(r"^[ \t]*#[ \t]*(?:ifdef|ifndef)[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.M)
# preprocessor lines that may contain defined(...) expressions
IF_LINE_RE = re.compile(r"^[ \t]*#[ \t]*(?:el)?if\b.*$", re.M)
# every defined(NAME) inside such a line is captured, so combined
# conditions like `#if defined(A) && defined(B)` register both macros
DEFINED_RE = re.compile(r"\bdefined\s*\(?\s*([A-Za-z_][A-Za-z0-9_]*)")


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield os.path.join(dirpath, name)


def rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def scan_cmake(root):
    options = {}
    for path in iter_files(root):
        if os.path.basename(path) != "CMakeLists.txt" and not path.endswith(".cmake"):
            continue
        text = read_text(path)
        for match in OPTION_RE.finditer(text):
            name = match.group(1)
            # CMake: option(<var> "<doc>" [value]) — a missing value means OFF
            default = match.group(2) or "OFF"
            options.setdefault(name, {"default": default, "files": []})
            options[name]["files"].append(rel(root, path))
    return options


def scan_macros(root):
    defines = defaultdict(int)
    conditionals = defaultdict(int)
    for path in iter_files(root):
        if os.path.splitext(path)[1].lower() not in C_EXT:
            continue
        text = read_text(path)
        if not text:
            continue
        for name in DEFINE_RE.findall(text):
            defines[name] += 1
        for name in IFDEF_RE.findall(text):
            conditionals[name] += 1
        for line_match in IF_LINE_RE.finditer(text):
            for name in DEFINED_RE.findall(line_match.group(0)):
                conditionals[name] += 1
    return defines, conditionals


def scan_public_headers(root, public_prefix=""):
    headers = {}
    for path in iter_files(root):
        if os.path.splitext(path)[1].lower() not in {".h", ".hpp", ".hh", ".hxx"}:
            continue
        r = rel(root, path)
        if public_prefix and not r.startswith(public_prefix):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        headers[r] = hashlib.sha256(data).hexdigest()
    return headers


def diff_options(src, tgt):
    added = sorted(set(tgt) - set(src))
    removed = sorted(set(src) - set(tgt))
    changed = sorted(
        name for name in set(src) & set(tgt) if src[name]["default"] != tgt[name]["default"]
    )
    return {
        "added": added,
        "removed": removed,
        "default_changed": [
            {"name": name, "source": src[name]["default"], "target": tgt[name]["default"]} for name in changed
        ],
    }


def diff_macros(src, tgt):
    src_all = set(src[0]) | set(src[1])
    tgt_all = set(tgt[0]) | set(tgt[1])
    return {
        "only_in_source": sorted(src_all - tgt_all),
        "only_in_target": sorted(tgt_all - src_all),
        "source_count": len(src_all),
        "target_count": len(tgt_all),
    }


def diff_headers(src, tgt):
    added = sorted(set(tgt) - set(src))
    removed = sorted(set(src) - set(tgt))
    changed = sorted(name for name in set(src) & set(tgt) if src[name] != tgt[name])
    return {"added": added, "removed": removed, "content_changed": changed}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="old engine source tree")
    ap.add_argument("--target", required=True, help="new engine source tree")
    ap.add_argument("--json", help="write machine-readable report to this path")
    ap.add_argument(
        "--public-prefix",
        default="",
        help="only hash headers under this relative prefix (e.g. faiss/); default: all headers",
    )
    ap.add_argument("--max-list", type=int, default=100, help="cap printed list length (default 100)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    for path in (args.source, args.target):
        if not os.path.isdir(path):
            print("error: not a directory: %s" % path, file=sys.stderr)
            return 2

    src_opts, tgt_opts = scan_cmake(args.source), scan_cmake(args.target)
    src_macros, tgt_macros = scan_macros(args.source), scan_macros(args.target)
    src_headers = scan_public_headers(args.source, args.public_prefix)
    tgt_headers = scan_public_headers(args.target, args.public_prefix)

    result = {
        "source": os.path.abspath(args.source),
        "target": os.path.abspath(args.target),
        "cmake_options": diff_options(src_opts, tgt_opts),
        "macros": diff_macros(src_macros, tgt_macros),
        "public_headers": diff_headers(src_headers, tgt_headers),
        "counts": {
            "source_cmake_options": len(src_opts),
            "target_cmake_options": len(tgt_opts),
            "source_public_headers": len(src_headers),
            "target_public_headers": len(tgt_headers),
        },
    }

    opts = result["cmake_options"]
    macros = result["macros"]
    headers = result["public_headers"]

    print(
        "cmake_options: added=%d removed=%d default_changed=%d"
        % (len(opts["added"]), len(opts["removed"]), len(opts["default_changed"]))
    )
    print(
        "macros: only_source=%d only_target=%d (source_total=%d target_total=%d)"
        % (len(macros["only_in_source"]), len(macros["only_in_target"]), macros["source_count"], macros["target_count"])
    )
    print(
        "public_headers: added=%d removed=%d content_changed=%d"
        % (len(headers["added"]), len(headers["removed"]), len(headers["content_changed"]))
    )

    if not args.quiet:
        def show(title, items):
            if not items:
                return
            print("  %s:" % title)
            for item in items[: args.max_list]:
                print("    %s" % item)
            if len(items) > args.max_list:
                print("    ... %d more" % (len(items) - args.max_list))

        show("cmake added", opts["added"])
        show("cmake removed", opts["removed"])
        show("cmake default changed", ["%s (%s -> %s)" % (x["name"], x["source"], x["target"]) for x in opts["default_changed"]])
        show("macros only in source", macros["only_in_source"])
        show("macros only in target", macros["only_in_target"])
        show("headers added", headers["added"])
        show("headers removed", headers["removed"])
        show("headers content changed", headers["content_changed"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("report_json=%s" % args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())

