#!/usr/bin/env python3
"""
ARM Instruction Set Query Tool.

Query NEON/SIMD, SVE, and SME instructions from local JSON data assets.
Usage: python3 query.py <command> [options]

Commands:
  search <name>        Fuzzy search instruction by name across all architectures
  info <name>          Get full details of a specific instruction (exact match)
  check <arch> <name>  Check if instruction exists in a specific architecture
  list <arch>          List all instructions in an architecture (simd/sve/sme)
  feature <feat>       List instructions requiring a specific FEAT_* feature
  grep <keyword>       Search description/syntax/encodings for a keyword
  stats                Print summary statistics
"""

import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
import re
from difflib import get_close_matches

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

SIMD_FILE = os.path.join(ASSETS_DIR, "simd_instructions.json")
SVE_FILE = os.path.join(ASSETS_DIR, "sve_instructions.json")
SME_FILE = os.path.join(ASSETS_DIR, "sme_instructions.json")

ARCH_ALIASES = {
    "simd": "simd", "neon": "simd", "advsimd": "simd", "asimd": "simd",
    "sve": "sve",
    "sme": "sme",
}

# ── load / normalize ──────────────────────────────────────────────

def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _simd_item(inst):
    """Normalize SIMD/SVE item to common schema."""
    return {
        "title": inst["title"],
        "arch": None,  # set by caller
        "description": inst.get("description", ""),
        "syntax": inst.get("syntax", []),
        "features": inst.get("features", []),
        "pseudocode": inst.get("pseudocode", ""),
        "operational_info": inst.get("operational_info", ""),
        "url": inst.get("url", ""),
        "encodings": [],
        "symbols": "",
        "decode": "",
        "category": "",
    }

def _sme_item(inst):
    """Normalize SME item to common schema."""
    return {
        "title": inst["title"],
        "arch": None,
        "description": inst.get("description", ""),
        "syntax": [],
        "features": inst.get("feats", []),
        "pseudocode": inst.get("pseudocode", ""),
        "operational_info": "",
        "url": inst.get("source_url", ""),
        "encodings": inst.get("encodings", []),
        "symbols": inst.get("symbols", ""),
        "decode": inst.get("decode", ""),
        "category": inst.get("category", ""),
    }

def load_all():
    """Return dict: {arch: [normalized_instructions]}."""
    simd_raw = _load_json(SIMD_FILE)
    sve_raw = _load_json(SVE_FILE)
    sme_raw = _load_json(SME_FILE)

    simd = [_simd_item(i) for i in simd_raw]
    for i in simd:
        i["arch"] = "SIMD"

    sve = [_simd_item(i) for i in sve_raw]
    for i in sve:
        i["arch"] = "SVE"

    sme = [_sme_item(i) for i in sme_raw["instructions"]]
    for i in sme:
        i["arch"] = "SME"

    return {"simd": simd, "sve": sve, "sme": sme}

def _build_index(data):
    """Build {title_lower: [matched_items]} across all archs."""
    idx = {}
    for arch in data:
        for inst in data[arch]:
            key = inst["title"].lower()
            idx.setdefault(key, []).append(inst)
    return idx

# ── display helpers ───────────────────────────────────────────────

def _bold(s):
    return f"\033[1m{s}\033[0m"

def _dim(s):
    return f"\033[2m{s}\033[0m"

def _print_instruction(inst, show_full=False):
    """Pretty-print a single normalized instruction."""
    arch_tag = inst['arch']
    print(f"\n{_bold('Instruction:')} {inst['title']}  {_dim('[' + arch_tag + ']')}")
    print(f"{_bold('Description:')} {inst['description']}")
    if inst.get("syntax"):
        print(f"{_bold('Syntax:')}")
        for s in inst["syntax"]:
            print(f"  {s}")
    if inst.get("encodings"):
        print(f"{_bold('Encodings:')}")
        for e in inst["encodings"]:
            print(f"  {e}")
    if inst.get("features"):
        print(f"{_bold('Features:')} {', '.join(inst['features'])}")
    if inst.get("category"):
        print(f"{_bold('Category:')} {inst['category']}")
    if inst.get("operational_info"):
        print(f"{_bold('Operational Info:')} {inst['operational_info']}")
    if inst.get("url"):
        print(f"{_bold('Reference:')} {inst['url']}")

    if show_full:
        if inst.get("pseudocode"):
            print(f"\n{_bold('Pseudocode:')}")
            print(inst["pseudocode"])
        if inst.get("symbols"):
            print(f"\n{_bold('Symbols:')}")
            print(inst["symbols"])
        if inst.get("decode"):
            print(f"\n{_bold('Decode:')}")
            print(inst["decode"])

# ── subcommands ───────────────────────────────────────────────────

def cmd_stats(data):
    """Print summary statistics."""
    simd_count = len(data["simd"])
    sve_count = len(data["sve"])
    sme_count = len(data["sme"])
    total = simd_count + sve_count + sme_count

    all_features = set()
    for arch in data:
        for inst in data[arch]:
            all_features.update(inst["features"])
    all_features = sorted(all_features)

    arch_features = {}
    for arch in data:
        feats = set()
        for inst in data[arch]:
            feats.update(inst["features"])
        arch_features[arch] = sorted(feats)

    print(f"{_bold('ARM Instruction Sets Summary')}")
    print(f"{'─' * 50}")
    print(f"  SIMD/NEON: {simd_count:>5} instructions")
    print(f"  SVE:       {sve_count:>5} instructions")
    print(f"  SME:       {sme_count:>5} instructions")
    print(f"  {'─' * 20}")
    print(f"  Total:     {total:>5} instructions")
    print(f"\n{_bold('Features per architecture:')}")
    for arch, feats in arch_features.items():
        print(f"  {arch}: {len(feats)} features")
    print(f"\n{_bold('All unique features:')} ({len(all_features)} total)")
    for f in all_features:
        count = sum(1 for arch in data for inst in data[arch] if f in inst["features"])
        print(f"  {f}: {count} instructions")


def cmd_list(data, arch):
    """List all instructions in an architecture."""
    arch_key = ARCH_ALIASES.get(arch.lower(), arch.lower())
    if arch_key not in data:
        print(f"Unknown architecture: {arch}. Valid: simd/neon, sve, sme", file=sys.stderr)
        sys.exit(1)

    instructions = data[arch_key]
    print(f"{_bold(f'{arch_key.upper()} Instructions')} ({len(instructions)} total)")
    print(f"{'─' * 60}")
    for i, inst in enumerate(instructions, 1):
        synopsis = inst["description"].split(".")[0] if inst["description"] else ""
        feats_str = f"  [{', '.join(inst['features'])}]" if inst['features'] else ""
        print(f"  {i:>4}. {_bold(inst['title']):<30s} {synopsis[:60]}{feats_str}")


def cmd_search(data, name, show_full=False):
    """Fuzzy search instruction by name."""
    idx = _build_index(data)

    # 1. exact (case-insensitive)
    key = name.lower()
    if key in idx:
        for inst in idx[key]:
            _print_instruction(inst, show_full=show_full)
        return

    # 2. starts-with
    starts = [(inst["title"].lower(), inst) for arch in data for inst in data[arch]
              if inst["title"].lower().startswith(key)]
    if starts:
        print(f"No exact match. Found {len(starts)} prefix matches:")
        for _, inst in starts:
            _print_instruction(inst, show_full=show_full)
        return

    # 3. substring
    subs = [(inst["title"].lower(), inst) for arch in data for inst in data[arch]
            if key in inst["title"].lower()]
    if subs:
        print(f"No exact or prefix match. Found {len(subs)} substring matches:")
        for _, inst in subs:
            _print_instruction(inst, show_full=show_full)
        return

    # 4. fuzzy (difflib)
    all_titles = [inst["title"] for arch in data for inst in data[arch]]
    matches = get_close_matches(name, all_titles, n=10, cutoff=0.4)
    if matches:
        print(f"No matches found for '{name}'.")
        print(f"Did you mean one of these?")
        for m in matches:
            for inst in idx.get(m.lower(), []):
                arch_tag = inst['arch']
                line = f"  {_bold(m)}  {_dim('[' + arch_tag + ']')}  {inst['description'][:80]}"
                print(line)
    else:
        print(f"No matches found for '{name}'.")


def cmd_info(data, name):
    """Get full details with exact match."""
    idx = _build_index(data)
    key = name.lower()
    if key not in idx:
        print(f"Instruction '{name}' not found. Trying fuzzy search...")
        cmd_search(data, name, show_full=True)
        return
    for inst in idx[key]:
        _print_instruction(inst, show_full=True)


def cmd_check(data, arch, name):
    """Check if instruction exists in a specific architecture."""
    arch_key = ARCH_ALIASES.get(arch.lower(), arch.lower())
    if arch_key not in data:
        print(f"Unknown architecture: {arch}. Valid: simd/neon, sve, sme", file=sys.stderr)
        sys.exit(1)

    key = name.lower()
    matches = [inst for inst in data[arch_key] if inst["title"].lower() == key]

    if matches:
        print(f"{_bold('YES')} — '{name}' exists in {arch_key.upper()}.")
        for inst in matches:
            print(f"  Description: {inst['description']}")
            if inst.get("syntax"):
                print(f"  Syntax: {' | '.join(inst['syntax'])}")
            if inst.get("encodings"):
                print(f"  Encodings: {' | '.join(inst['encodings'])}")
            if inst.get("features"):
                print(f"  Features: {', '.join(inst['features'])}")
    else:
        # fuzzy suggestions within this arch
        titles = [inst["title"] for inst in data[arch_key]]
        suggestions = get_close_matches(name, titles, n=5, cutoff=0.4)
        print(f"{_bold('NO')} — '{name}' not found in {arch_key.upper()}.")
        if suggestions:
            print(f"Similar instructions in {arch_key.upper()}: {', '.join(suggestions)}")

        # also check other archs
        idx = _build_index(data)
        if key in idx:
            other_archs = [inst["arch"] for inst in idx[key]]
            print(f"But '{name}' exists in: {', '.join(other_archs)}")


def cmd_feature(data, feat):
    """List instructions requiring a specific FEAT_* feature."""
    # add FEAT_ prefix if missing
    query = feat.upper()
    if not query.startswith("FEAT_"):
        query = "FEAT_" + query

    results = []
    for arch in data:
        for inst in data[arch]:
            if query in inst["features"]:
                results.append(inst)

    if not results:
        all_features = set()
        for arch in data:
            for inst in data[arch]:
                all_features.update(inst["features"])
        all_features = sorted(all_features)
        print(f"No instructions found for feature '{query}'.")
        suggestions = get_close_matches(query, all_features, n=5, cutoff=0.4)
        if suggestions:
            print(f"Similar features: {', '.join(suggestions)}")
        return

    print(f"{_bold(f'Instructions requiring {query}')} ({len(results)} total)")
    print(f"{'─' * 60}")
    for inst in results:
        arch_tag = inst['arch']
        line = f"  {_bold(inst['title']):<35s} {_dim('[' + arch_tag + ']')}  {inst['description'][:80]}"
        print(line)


def cmd_grep(data, keyword):
    """Search description, syntax, encodings, and title for a keyword."""
    kw = keyword.lower()
    results = []
    for arch in data:
        for inst in data[arch]:
            desc_lower = inst["description"].lower()
            title_lower = inst["title"].lower()
            syntax_text = " ".join(inst.get("syntax", [])).lower()
            enc_text = " ".join(inst.get("encodings", [])).lower()

            if kw in desc_lower or kw in syntax_text or kw in enc_text or kw in title_lower:
                results.append((inst, _highlight_context(inst, kw)))

    if not results:
        print(f"No instructions found matching '{keyword}'.")
        return

    header = 'Search results for "' + keyword + '"'
    print(f"{_bold(header)} ({len(results)} total)")
    print(f"{'─' * 60}")
    for inst, context in results:
        arch_tag = inst['arch']
        line = f"  {_bold(inst['title']):<35s} {_dim('[' + arch_tag + ']')}"
        print(line)
        if context:
            print(f"    {context}")


def _highlight_context(inst, keyword):
    """Extract and highlight the matching context snippet."""
    desc_lower = inst["description"].lower()
    if keyword in desc_lower:
        idx = desc_lower.index(keyword)
        start = max(0, idx - 40)
        end = min(len(inst["description"]), idx + len(keyword) + 60)
        snippet = inst["description"][start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(inst["description"]):
            snippet = snippet + "…"
        return snippet
    syntax_text = " ".join(inst.get("syntax", []) + inst.get("encodings", []))
    if keyword in syntax_text.lower():
        return f"Syntax: {syntax_text}"
    return ""


# ── main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ARM Instruction Set Query Tool (NEON/SIMD, SVE, SME)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 query.py stats
  python3 query.py search ABS
  python3 query.py search "ADD (vector)"
  python3 query.py info ABS
  python3 query.py check sve ABS
  python3 query.py check neon MLA
  python3 query.py list simd
  python3 query.py feature SVE2
  python3 query.py grep "absolute value"
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # stats
    subparsers.add_parser("stats", help="Print summary statistics")

    # search
    p_search = subparsers.add_parser("search", help="Fuzzy search instruction by name")
    p_search.add_argument("name", help="Instruction name or partial name")
    p_search.add_argument("-f", "--full", action="store_true", help="Show pseudocode and other full details")

    # info
    p_info = subparsers.add_parser("info", help="Get full details (exact match, falls back to fuzzy)")
    p_info.add_argument("name", help="Exact instruction name")

    # check
    p_check = subparsers.add_parser("check", help="Check if instruction exists in an architecture")
    p_check.add_argument("arch", help="Architecture: simd/neon, sve, sme")
    p_check.add_argument("name", help="Instruction name")

    # list
    p_list = subparsers.add_parser("list", help="List all instructions in an architecture")
    p_list.add_argument("arch", help="Architecture: simd/neon, sve, sme")

    # feature
    p_feat = subparsers.add_parser("feature", help="List instructions requiring a feature")
    p_feat.add_argument("feat", help="Feature name (e.g., SVE2, AdvSIMD, with or without FEAT_ prefix)")

    # grep
    p_grep = subparsers.add_parser("grep", help="Search descriptions/syntax for a keyword")
    p_grep.add_argument("keyword", help="Keyword to search for")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # load data
    data = load_all()

    if args.command == "stats":
        cmd_stats(data)
    elif args.command == "search":
        cmd_search(data, args.name, show_full=args.full)
    elif args.command == "info":
        cmd_info(data, args.name)
    elif args.command == "check":
        cmd_check(data, args.arch, args.name)
    elif args.command == "list":
        cmd_list(data, args.arch)
    elif args.command == "feature":
        cmd_feature(data, args.feat)
    elif args.command == "grep":
        cmd_grep(data, args.keyword)


if __name__ == "__main__":
    main()
