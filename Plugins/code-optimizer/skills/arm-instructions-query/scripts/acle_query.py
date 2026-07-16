#!/usr/bin/env python3
"""
ACLE Intrinsic Query Tool

Query Arm intrinsics from a single JSON database file.
Supports SVE, SVE2, SME, Neon, MVE/Helium, and ACLE scalar intrinsics.

Usage:
    python3 acle_query.py search <pattern> [--family=ISA] [--json]
    python3 acle_query.py info <name>      [--json]
    python3 acle_query.py list [--family=ISA] [--cat=CAT] [--json]
    python3 acle_query.py insn <name>      [--family=ISA] [--json]
    python3 acle_query.py types [--json]
    python3 acle_query.py macros [--json]
"""

import sys
sys.dont_write_bytecode = True

import argparse
import gzip
import json
import os
import time
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent.parent / "assets" / "acle_data"
DB_FILE = "arm_intrinsics_all.json"


def load_all_from(data_dir=None):
    """Load the single database file and build indexes on-the-fly."""
    if data_dir is None:
        data_dir = DATA_DIR
    data_dir = Path(data_dir)

    # Try compressed file first, then plain JSON
    db_path_gz = data_dir / (DB_FILE + ".gz")
    db_path = data_dir / DB_FILE

    if db_path_gz.exists():
        with gzip.open(db_path_gz, "rt", encoding="utf-8") as f:
            raw = json.load(f)
    elif db_path.exists():
        with open(db_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        print(f"Error: {db_path} not found.", file=sys.stderr)
        print(f"Run extract_arm_intrinsics.py first.", file=sys.stderr)
        sys.exit(1)

    # Support both old (list) and new (object) format
    if isinstance(raw, list):
        # Old flat list format
        intrinsics = raw
        macros = []
        types_data = []
    else:
        # New object format
        intrinsics = raw.get('intrinsics', [])
        macros = raw.get('macros', [])
        types_data = raw.get('types', [])

    # Build name index: expanded_name -> list of entry indices
    name_index = defaultdict(list)
    for i, e in enumerate(intrinsics):
        name_index[e['name']].append(i)
        if e.get('base_name'):
            name_index[e['base_name']].append(i)
        for n in e.get('expanded_names', []):
            name_index[n].append(i)

    # Build instruction index: INSN_UPPER -> list of entry indices
    insn_index = defaultdict(list)
    for i, e in enumerate(intrinsics):
        for insn in e.get('mapped_instructions', []):
            insn_index[insn.upper()].append(i)

    return {
        'intrinsics': intrinsics,
        'macros': macros,
        'types': types_data,
        'name_index': name_index,
        'insn_index': insn_index,
    }


# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def fmt_entry_brief(e):
    """One-line summary of an intrinsic entry."""
    name = e['name']
    fam = '/'.join(e.get('family', []))
    desc = e.get('description', '')[:50]
    return f"  {name:<30s} [{fam:<12s}] {desc}"


def fmt_entry_detail(e):
    """Full detail of an intrinsic entry."""
    lines = []
    lines.append(f"Name:        {e['name']}")
    lines.append(f"Family:      {', '.join(e.get('family', []))}")
    lines.append(f"Category:    {e.get('category', 'N/A')}")

    # Prototype
    lines.append(f"Prototype:   {e.get('prototype', '')}")

    # Expanded prototypes
    expanded = e.get('expanded_prototypes', [])
    if len(expanded) > 1:
        lines.append(f"Expanded:")
        for p in expanded[:15]:
            lines.append(f"  {p}")
        if len(expanded) > 15:
            lines.append(f"  ... (+{len(expanded)-15} more)")

    # Return type
    ret = e.get('return_type', '')
    bits = e.get('element_bits', '')
    if ret:
        bits_str = f" ({bits}-bit elements)" if bits else ""
        lines.append(f"Returns:     {ret}{bits_str}")

    # Arguments
    args = e.get('arguments', [])
    if args:
        lines.append(f"Arguments:   {', '.join(args)}")

    # Description
    if e.get('description'):
        lines.append(f"Description: {e['description'][:200]}")

    # Mapped instructions with details
    insn_details = e.get('instruction_details', [])
    mapped = e.get('mapped_instructions', [])
    if insn_details:
        lines.append(f"Instructions:")
        last_preamble = None
        for insn in insn_details[:8]:
            base = insn.get('base_instruction', '')
            operands = insn.get('operands', '')
            url = insn.get('url', '')
            preamble = insn.get('preamble', '')
            if preamble and preamble != last_preamble:
                if len(preamble) > 80:
                    preamble = preamble[:77] + '...'
                lines.append(f"  [{preamble}]")
                last_preamble = preamble
            if base:
                line = f"    {base} {operands}"
                if url:
                    line += f"  ({url})"
                lines.append(line)
    elif mapped:
        lines.append(f"Instructions: {', '.join(mapped)}")

    # Argument mappings
    arg_maps = e.get('argument_mappings', [])
    if arg_maps:
        lines.append(f"Arg mapping:")
        for m in arg_maps[:8]:
            lines.append(f"  {m['argument']} -> {m['register']}")

    # Result mappings
    res_maps = e.get('result_mappings', [])
    if res_maps:
        lines.append(f"Result mapping:")
        for m in res_maps[:4]:
            lines.append(f"  {m['register']} = {m['description']}")

    # Feature macros
    if e.get('feature_macros'):
        lines.append(f"Features:    {', '.join(e['feature_macros'])}")

    # Architecture
    if e.get('architectures'):
        lines.append(f"Arch:        {', '.join(e['architectures'])}")

    # Header
    if e.get('header'):
        lines.append(f"Header:      {e['header']}")

    # URL
    if e.get('url'):
        lines.append(f"URL:         {e['url']}")

    return '\n'.join(lines)


def fmt_type_brief(t):
    name = t['name']
    family = t.get('family', '')
    desc = t.get('description', '')[:50]
    return f"  {name:<25s} {family:<8s} {desc}"


def fmt_macro_brief(m):
    name = m['name']
    meaning = m.get('meaning', '')[:60]
    return f"  {name:<45s} {meaning}"


def entry_matches_family(entry, family=None):
    """Return whether an intrinsic entry belongs to the requested family."""
    return family is None or family in entry.get('family', [])


def entry_matches_name(entry, name):
    """Return whether a query name exactly matches any advertised intrinsic name."""
    return (
        entry.get('name') == name
        or entry.get('base_name') == name
        or name in entry.get('expanded_names', [])
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def search_score(e, pattern_lower):
    """Score an intrinsic entry by search relevance.

    Returns (score, entry) tuple. Higher score = more relevant.
    Score components (weighted):
      name/base_name match:    +100  (most important)
      expanded_names match:    +90
      category match:          +50
      feature_macros match:    +30
      description match:       +10   (least important, too broad)
    """
    score = 0
    name = e['name'].lower()
    base = e.get('base_name', '').lower()
    desc = e.get('description', '').lower()
    cat = e.get('category', '').lower()
    feats = ' '.join(e.get('feature_macros', [])).lower()
    expanded = ' '.join(e.get('expanded_names', [])).lower()

    if pattern_lower in name or pattern_lower in base:
        score += 100
    if pattern_lower in expanded:
        score += 90
    if pattern_lower in cat:
        score += 50
    if pattern_lower in feats:
        score += 30
    if pattern_lower in desc:
        score += 10

    return score


def cmd_search(data, pattern, family=None, as_json=False):
    """Search intrinsics by name or description pattern.

    Results are ranked by relevance:
      1. Name/base_name match (highest priority)
      2. Expanded names match
      3. Category match
      4. Feature macros match
      5. Description match (lowest priority)
    """
    pattern_lower = pattern.lower()
    scored_results = []
    seen = set()

    for e in data['intrinsics']:
        if family and family not in e.get('family', []):
            continue

        score = search_score(e, pattern_lower)
        if score > 0 and e['name'] not in seen:
            scored_results.append((score, e))
            seen.add(e['name'])

    # Sort by score (descending), then by name
    scored_results.sort(key=lambda x: (-x[0], x[1]['name']))
    results = [r[1] for r in scored_results]

    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    if not results:
        print(f"No intrinsics found matching '{pattern}'")
        return

    print(f"Found {len(results)} intrinsics matching '{pattern}':\n")
    for r in results[:50]:
        print(fmt_entry_brief(r))
    if len(results) > 50:
        print(f"\n  ... and {len(results) - 50} more. Use --json for full output.")


def cmd_info(data, name, family=None, as_json=False):
    """Get detailed info for a specific intrinsic."""
    intrinsics = data['intrinsics']
    name_index = data['name_index']

    # 1. Direct name index lookup (exact or expanded name)
    if name in name_index:
        candidates = [
            intrinsics[i]
            for i in name_index[name]
            if entry_matches_family(intrinsics[i], family)
        ]
        if candidates:
            e = candidates[0]
            if as_json:
                print(json.dumps(e, indent=2, ensure_ascii=False))
            else:
                if e['name'] != name:
                    print(f"'{name}' is an expanded variant of {e['name']}:\n")
                print(fmt_entry_detail(e))
            return

    # 2. Prefix match on name or base_name
    prefix_matches = []
    for e in intrinsics:
        if not entry_matches_family(e, family):
            continue
        if e['name'].startswith(name) or e.get('base_name', '').startswith(name):
            prefix_matches.append(e)

    if prefix_matches:
        if as_json:
            print(json.dumps(prefix_matches[:10], indent=2, ensure_ascii=False))
        else:
            print(f"No exact match for '{name}'. Did you mean:\n")
            for e in prefix_matches[:10]:
                print(fmt_entry_brief(e))
        return

    # 3. Check if name is a variant in expanded_names
    for e in intrinsics:
        if not entry_matches_family(e, family):
            continue
        if name in e.get('expanded_names', []):
            if as_json:
                print(json.dumps(e, indent=2, ensure_ascii=False))
            else:
                print(f"'{name}' is a variant of {e['name']}:\n")
                print(fmt_entry_detail(e))
            return

    family_hint = f" in family '{family}'" if family else ""
    print(f"No intrinsic found for '{name}'{family_hint}")
    print(f"\nTip: try 'search {name}' for broader matching.")


def cmd_list(data, family=None, category=None, as_json=False):
    """List intrinsics, optionally filtered by family and/or category."""
    results = []
    for e in data['intrinsics']:
        if family and family not in e.get('family', []):
            continue
        if category and category.lower() not in e.get('category', '').lower():
            continue
        results.append(e)

    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    if not results:
        filters = []
        if family:
            filters.append(f"family={family}")
        if category:
            filters.append(f"category={category}")
        print(f"No intrinsics found matching {', '.join(filters)}")
        return

    # Group by category
    cats = defaultdict(list)
    for e in results:
        cats[e.get('category', '(uncategorized)')].append(e)

    fam_str = f" [{family}]" if family else ""
    print(f"{'='*60}")
    print(f"Arm Intrinsics ({len(results)} total){fam_str}")
    if category:
        print(f"Filtered by: {category}")
    print(f"{'='*60}\n")

    for cat, items in sorted(cats.items()):
        print(f"## {cat} ({len(items)})")
        sorted_items = sorted(items, key=lambda x: x['name'])
        for item in sorted_items[:20]:
            fam = '/'.join(item.get('family', []))
            print(f"  {item['name']:<30s} [{fam:<12s}]")
        if len(items) > 20:
            print(f"  ... +{len(items) - 20} more")
        print()


def cmd_insn(data, name, family=None, as_json=False):
    """Query by instruction name: find which intrinsics map to it."""
    name_upper = name.upper()
    insn_index = data['insn_index']
    intrinsics = data['intrinsics']

    # 1. Exact or substring match in instruction index
    results = []
    seen = set()

    # Exact match
    if name_upper in insn_index:
        for idx in insn_index[name_upper]:
            e = intrinsics[idx]
            if family and family not in e.get('family', []):
                continue
            if e['name'] not in seen:
                results.append(e)
                seen.add(e['name'])

    # Substring match
    for insn_key, indices in insn_index.items():
        if name_upper in insn_key and insn_key != name_upper:
            for idx in indices:
                e = intrinsics[idx]
                if family and family not in e.get('family', []):
                    continue
                if e['name'] not in seen:
                    results.append(e)
                    seen.add(e['name'])

    if as_json:
        output = {
            'query': name,
            'matches': [{
                'name': e['name'],
                'family': e.get('family', []),
                'prototype': e.get('prototype', ''),
                'mapped_instructions': e.get('mapped_instructions', []),
            } for e in results],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return

    if not results:
        print(f"No instruction found matching '{name}'")
        print(f"\nTip: try 'search {name}' for broader matching.")
        return

    print(f"{'='*60}")
    print(f"Instruction Query: {name}")
    print(f"{'='*60}\n")
    print(f"Found {len(results)} intrinsics\n")

    # Group by family
    by_family = defaultdict(list)
    for e in results:
        fam = '/'.join(e.get('family', ['unknown']))
        by_family[fam].append(e)

    for fam, entries in sorted(by_family.items()):
        print(f"  [{fam}] ({len(entries)} entries)")
        for e in sorted(entries, key=lambda x: x['name'])[:20]:
            proto = e.get('prototype', '')[:60]
            print(f"    {e['name']:<30s} {proto}")
        if len(entries) > 20:
            print(f"    ... +{len(entries) - 20} more")
        print()


def cmd_types(data, as_json=False):
    """Show type system information."""
    types_data = data['types']

    if as_json:
        print(json.dumps(types_data, indent=2, ensure_ascii=False))
        return

    if not types_data:
        print("No type data available. Run extract_arm_intrinsics.py first.")
        return

    # Group by family
    families = defaultdict(list)
    for t in types_data:
        families[t.get('family', 'other')].append(t)

    print(f"{'='*60}")
    print(f"ACLE Type System ({len(types_data)} types)")
    print(f"{'='*60}\n")

    for fam, items in sorted(families.items()):
        print(f"## {fam.upper()} types ({len(items)})")
        for t in sorted(items, key=lambda x: x['name']):
            sizeless = " [sizeless]" if t.get('sizeless') else ""
            bits = ''
            if t.get('total_bits') or t.get('element_bits'):
                bits = f" ({t.get('total_bits', t.get('element_bits', '?'))}-bit)"
            print(f"  {t['name']:<25s}{bits}{sizeless}  {t.get('description', '')}")
        print()


def cmd_macros(data, as_json=False):
    """Show feature detection macros."""
    macros = data['macros']

    if as_json:
        print(json.dumps(macros, indent=2, ensure_ascii=False))
        return

    if not macros:
        print("No macro data available. Run extract_arm_intrinsics.py first.")
        return

    print(f"{'='*60}")
    print(f"ACLE Feature Detection Macros ({len(macros)} macros)")
    print(f"{'='*60}\n")

    for m in sorted(macros, key=lambda x: x['name']):
        print(fmt_macro_brief(m))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Arm Intrinsics Query Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s search svadd              Search for intrinsics matching 'svadd'
  %(prog)s search vadd --family=neon  Search NEON intrinsics for 'vadd'
  %(prog)s info svadd_s32_m           Get detailed info for a specific intrinsic
  %(prog)s info vaddq_s32             Get info for a NEON intrinsic
  %(prog)s info __dmb                 Get info for ACLE barrier intrinsic
  %(prog)s insn TBL                   Find intrinsics for TBL instruction
  %(prog)s insn ADD --family=sve      Find SVE intrinsics for ADD instruction
  %(prog)s list --family=sve          List all SVE intrinsics
  %(prog)s list --family=neon --cat=arith  List NEON arithmetic intrinsics
  %(prog)s types                      Show ACLE type system
  %(prog)s macros                     Show feature detection macros
  %(prog)s info svadd --json          JSON output for programmatic use
""")
    parser.add_argument("--data-dir", default=str(DATA_DIR),
                        help="Path to acle_data directory")

    subparsers = parser.add_subparsers(dest="command", help="Query command")

    # search
    p_search = subparsers.add_parser("search",
                                     help="Search intrinsics by name/description")
    p_search.add_argument("pattern", help="Search pattern (case-insensitive)")
    p_search.add_argument("--family", default=None,
                          choices=["sve", "sve2", "sme", "neon", "mve", "acle"],
                          help="Filter by ISA family")
    p_search.add_argument("--json", action="store_true", help="JSON output")

    # info
    p_info = subparsers.add_parser("info",
                                   help="Get detailed intrinsic info")
    p_info.add_argument("name", help="Intrinsic name (exact or prefix)")
    p_info.add_argument("--family", default=None,
                        choices=["sve", "sve2", "sme", "neon", "mve", "acle"],
                        help="Filter by ISA family")
    p_info.add_argument("--json", action="store_true", help="JSON output")

    # list
    p_list = subparsers.add_parser("list",
                                   help="List intrinsics by category")
    p_list.add_argument("--family", default=None,
                        choices=["sve", "sve2", "sme", "neon", "mve", "acle"],
                        help="Filter by ISA family")
    p_list.add_argument("--cat", default=None, help="Filter by category")
    p_list.add_argument("--json", action="store_true", help="JSON output")

    # insn
    p_insn = subparsers.add_parser("insn",
                                   help="Query by instruction name")
    p_insn.add_argument("name", help="Instruction name (e.g. TBL, ADD, UMINV)")
    p_insn.add_argument("--family", default=None,
                        choices=["sve", "sve2", "sme", "neon", "mve"],
                        help="Filter by ISA family")
    p_insn.add_argument("--json", action="store_true", help="JSON output")

    # types
    p_types = subparsers.add_parser("types", help="Show type system")
    p_types.add_argument("--json", action="store_true", help="JSON output")

    # macros
    p_macros = subparsers.add_parser("macros",
                                     help="Show feature detection macros")
    p_macros.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load data
    data_dir = Path(args.data_dir)
    data = load_all_from(data_dir)

    if args.command == "search":
        cmd_search(data, args.pattern, family=args.family, as_json=args.json)
    elif args.command == "info":
        cmd_info(data, args.name, family=args.family, as_json=args.json)
    elif args.command == "list":
        cmd_list(data, family=args.family, category=args.cat, as_json=args.json)
    elif args.command == "insn":
        cmd_insn(data, args.name, family=args.family, as_json=args.json)
    elif args.command == "types":
        cmd_types(data, as_json=args.json)
    elif args.command == "macros":
        cmd_macros(data, as_json=args.json)


if __name__ == "__main__":
    main()
