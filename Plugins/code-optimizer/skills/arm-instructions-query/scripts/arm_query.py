#!/usr/bin/env python3
"""Stable pipeline-level wrapper for NEON/SVE intrinsic and instruction facts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import acle_query  # noqa: E402
import query as isa_query  # noqa: E402


SUPPORTED_FAMILIES = ("neon", "sve", "sve2")
INSTRUCTION_ARCH_BY_FAMILY = {
    "neon": "simd",
    "sve": "sve",
    "sve2": "sve",
}
NAME_MATCH_RELEVANCE_WEIGHT = 100
EXPANDED_NAME_RELEVANCE_WEIGHT = 90
CATEGORY_RELEVANCE_WEIGHT = 50
FEATURE_MACRO_RELEVANCE_WEIGHT = 30
DESCRIPTION_RELEVANCE_WEIGHT = 10


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Pipeline-level ARM fact query wrapper. Defaults to NEON/SVE/SVE2 only; "
            "SME data remains available in lower-level tools but is intentionally not exposed here."
        )
    )
    parser.add_argument(
        "--data-dir",
        default=str(acle_query.DATA_DIR),
        help="Path to ACLE intrinsic data directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    intrinsic = subparsers.add_parser("intrinsic", help="Lookup one ACLE intrinsic by exact name.")
    intrinsic.add_argument("--name", required=True)
    intrinsic.add_argument("--family", choices=SUPPORTED_FAMILIES, required=True)
    intrinsic.add_argument("--json", action="store_true")

    intrinsic_search = subparsers.add_parser(
        "intrinsic-search", help="Search ACLE intrinsics by keyword."
    )
    intrinsic_search.add_argument("--keyword", required=True)
    intrinsic_search.add_argument("--family", choices=SUPPORTED_FAMILIES, required=True)
    intrinsic_search.add_argument("--limit", type=int, default=20)
    intrinsic_search.add_argument("--json", action="store_true")

    instruction = subparsers.add_parser("instruction", help="Lookup ARM instruction facts.")
    instruction.add_argument("--name", required=True)
    instruction.add_argument("--family", choices=SUPPORTED_FAMILIES, required=True)
    instruction.add_argument("--json", action="store_true")

    instruction_search = subparsers.add_parser(
        "instruction-search", help="Search ARM instructions by keyword."
    )
    instruction_search.add_argument("--keyword", required=True)
    instruction_search.add_argument("--family", choices=SUPPORTED_FAMILIES, required=True)
    instruction_search.add_argument("--limit", type=int, default=20)
    instruction_search.add_argument("--json", action="store_true")

    return parser.parse_args()


def is_sve2_instruction(item: dict[str, Any]) -> bool:
    """Return whether a normalized SVE instruction has an SVE2 feature tag."""

    return any(str(feature).upper().startswith("FEAT_SVE2") for feature in item.get("features", []))


def requires_sve2_instruction(item: dict[str, Any]) -> bool:
    """Return whether an SVE instruction is unavailable to plain SVE targets."""

    features = {str(feature).upper() for feature in item.get("features", [])}
    return is_sve2_instruction(item) and "FEAT_SVE" not in features


def instruction_records_for_family(family: str) -> list[dict[str, Any]]:
    """Load normalized instruction records for one exposed family."""

    data = isa_query.load_all()
    arch_key = INSTRUCTION_ARCH_BY_FAMILY[family]
    records = list(data[arch_key])
    if family == "sve":
        records = [record for record in records if not requires_sve2_instruction(record)]
    elif family == "sve2":
        records = [record for record in records if is_sve2_instruction(record)]
    return records


def instruction_base_name(title: str) -> str:
    """Return the mnemonic prefix from an instruction title."""

    return title.strip().split(" ", 1)[0].upper()


def entry_matches_intrinsic_name(entry: dict[str, Any], name: str) -> bool:
    """Return whether an ACLE entry exactly exposes the requested intrinsic spelling."""

    return acle_query.entry_matches_name(entry, name)


def normalized_keyword(keyword: str | None) -> str:
    """Return a normalized search keyword, or empty string for invalid input."""

    return str(keyword or "").strip().lower()


def intrinsic_matches_keyword(entry: dict[str, Any], keyword: str | None) -> bool:
    """Return whether an ACLE entry is relevant to a search keyword."""

    lowered = normalized_keyword(keyword)
    if not lowered:
        return False
    haystack = " ".join(
        [
            str(entry.get("name", "")),
            str(entry.get("base_name", "")),
            " ".join(entry.get("expanded_names", [])),
            str(entry.get("prototype", "")),
            str(entry.get("description", "")),
            str(entry.get("category", "")),
            " ".join(entry.get("mapped_instructions", [])),
            " ".join(entry.get("feature_macros", [])),
        ]
    ).lower()
    return lowered in haystack


def intrinsic_relevance(entry: dict[str, Any], keyword: str | None) -> int:
    """Score ACLE search relevance without depending on acle_query private helpers."""

    lowered = normalized_keyword(keyword)
    if not lowered:
        return 0
    score = 0
    name = str(entry.get("name", "")).lower()
    base_name = str(entry.get("base_name", "")).lower()
    expanded_names = " ".join(entry.get("expanded_names", [])).lower()
    category = str(entry.get("category", "")).lower()
    feature_macros = " ".join(entry.get("feature_macros", [])).lower()
    description = str(entry.get("description", "")).lower()
    if lowered in name or lowered in base_name:
        score += NAME_MATCH_RELEVANCE_WEIGHT
    if lowered in expanded_names:
        score += EXPANDED_NAME_RELEVANCE_WEIGHT
    if lowered in category:
        score += CATEGORY_RELEVANCE_WEIGHT
    if lowered in feature_macros:
        score += FEATURE_MACRO_RELEVANCE_WEIGHT
    if lowered in description:
        score += DESCRIPTION_RELEVANCE_WEIGHT
    return score


def relevant_feature_macros(macros: list[str], family: str) -> list[str]:
    """Filter mixed-family ACLE feature macros down to the requested NEON/SVE view."""

    if family == "neon":
        filtered = [macro for macro in macros if "NEON" in macro or "AdvSIMD" in macro]
    elif family == "sve2":
        filtered = [macro for macro in macros if "SVE" in macro and "SME" not in macro]
    else:
        filtered = [macro for macro in macros if "SVE" in macro and "SME" not in macro]
    return filtered or macros


def simplify_intrinsic_entry(entry: dict[str, Any], family: str) -> dict[str, Any]:
    """Keep the fields that downstream skills need for generation and evidence."""

    feature_macros = relevant_feature_macros(entry.get("feature_macros", []), family)
    return {
        "name": entry.get("name", ""),
        "base_name": entry.get("base_name", ""),
        "expanded_names": entry.get("expanded_names", []),
        "family": [family],
        "category": entry.get("category", ""),
        "prototype": entry.get("prototype", ""),
        "expanded_prototypes": entry.get("expanded_prototypes", []),
        "return_type": entry.get("return_type", ""),
        "arguments": entry.get("arguments", []),
        "header": entry.get("header", ""),
        "feature_macros": feature_macros,
        "mapped_instructions": entry.get("mapped_instructions", []),
        "instruction_details": entry.get("instruction_details", []),
        "argument_mappings": entry.get("argument_mappings", []),
        "result_mappings": entry.get("result_mappings", []),
        "architectures": entry.get("architectures", []),
    }


def relevant_instruction_features(features: list[str], family: str) -> list[str]:
    """Filter mixed SVE/SME instruction feature tags for this contract's family view."""

    if family in {"sve", "sve2"}:
        filtered = [feature for feature in features if "SME" not in str(feature).upper()]
        return filtered or features
    return features


def simplify_instruction_entry(entry: dict[str, Any], family: str) -> dict[str, Any]:
    """Keep instruction fields needed by generation, review, and repair skills."""

    features = relevant_instruction_features(entry.get("features", []), family)
    return {
        "title": entry.get("title", ""),
        "arch": entry.get("arch", ""),
        "description": entry.get("description", ""),
        "syntax": entry.get("syntax", []),
        "features": features,
        "operational_info": entry.get("operational_info", ""),
        "url": entry.get("url", ""),
        "pseudocode": entry.get("pseudocode", ""),
    }


def intrinsic_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """Build the common evidence payload for one intrinsic match."""

    return {
        "prototype": entry.get("prototype", ""),
        "header": entry.get("header", ""),
        "feature_macros": entry.get("feature_macros", []),
        "mapped_instructions": entry.get("mapped_instructions", []),
    }


def instruction_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """Build the common evidence payload for one instruction match."""

    return {
        "syntax_checked": bool(entry.get("syntax")),
        "features": entry.get("features", []),
        "pseudocode_checked": bool(entry.get("pseudocode")),
    }


def render_payload(payload: dict[str, Any], as_json: bool) -> None:
    """Print JSON or a compact human-readable summary."""

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"{payload['decision']}: {payload['query_type']} {payload['family']} {payload['query']}")
    for match in payload.get("matches", [])[:10]:
        label = match.get("name") or match.get("title")
        extra = match.get("prototype") or match.get("description", "")
        print(f"- {label}: {extra}")


def command_intrinsic(args: argparse.Namespace) -> int:
    """Lookup one intrinsic."""

    data = acle_query.load_all_from(args.data_dir)
    matches = [
        simplify_intrinsic_entry(entry, args.family)
        for entry in data["intrinsics"]
        if acle_query.entry_matches_family(entry, args.family)
        and entry_matches_intrinsic_name(entry, args.name)
    ]
    payload = {
        "query_type": "acle_intrinsic",
        "family": args.family,
        "query": args.name,
        "tool": "arm_query.py",
        "decision": "found" if matches else "not_found",
        "evidence": intrinsic_evidence(matches[0]) if matches else {},
        "matches": matches,
    }
    render_payload(payload, args.json)
    return 0 if matches else 1


def command_intrinsic_search(args: argparse.Namespace) -> int:
    """Search intrinsics."""

    data = acle_query.load_all_from(args.data_dir)
    matches = [
        entry
        for entry in data["intrinsics"]
        if acle_query.entry_matches_family(entry, args.family)
        and intrinsic_matches_keyword(entry, args.keyword)
    ]
    matches.sort(key=lambda entry: (-intrinsic_relevance(entry, args.keyword), entry["name"]))
    compact_matches = [
        simplify_intrinsic_entry(entry, args.family) for entry in matches[: max(args.limit, 0)]
    ]
    payload = {
        "query_type": "acle_intrinsic_search",
        "family": args.family,
        "query": args.keyword,
        "tool": "arm_query.py",
        "decision": "found" if compact_matches else "not_found",
        "evidence": intrinsic_evidence(compact_matches[0]) if compact_matches else {},
        "matches": compact_matches,
        "total_matches": len(matches),
    }
    render_payload(payload, args.json)
    return 0 if compact_matches else 1


def command_instruction(args: argparse.Namespace) -> int:
    """Lookup instruction records by mnemonic or title."""

    name = args.name.upper()
    matches = [
        simplify_instruction_entry(entry, args.family)
        for entry in instruction_records_for_family(args.family)
        if entry.get("title", "").upper() == name or instruction_base_name(entry.get("title", "")) == name
    ]
    payload = {
        "query_type": "isa_instruction",
        "family": args.family,
        "query": args.name,
        "tool": "arm_query.py",
        "decision": "found" if matches else "not_found",
        "evidence": instruction_evidence(matches[0]) if matches else {},
        "matches": matches,
    }
    render_payload(payload, args.json)
    return 0 if matches else 1


def command_instruction_search(args: argparse.Namespace) -> int:
    """Search instruction records by keyword."""

    keyword = args.keyword.lower()
    matches: list[dict[str, Any]] = []
    for entry in instruction_records_for_family(args.family):
        haystack = " ".join(
            [
                str(entry.get("title", "")),
                str(entry.get("description", "")),
                " ".join(entry.get("syntax", [])),
                " ".join(entry.get("features", [])),
            ]
        ).lower()
        if keyword in haystack:
            matches.append(simplify_instruction_entry(entry, args.family))
    limited_matches = matches[: max(args.limit, 0)]
    payload = {
        "query_type": "isa_instruction_search",
        "family": args.family,
        "query": args.keyword,
        "tool": "arm_query.py",
        "decision": "found" if limited_matches else "not_found",
        "evidence": instruction_evidence(limited_matches[0]) if limited_matches else {},
        "matches": limited_matches,
        "total_matches": len(matches),
    }
    render_payload(payload, args.json)
    return 0 if limited_matches else 1


def main() -> int:
    """Dispatch CLI commands."""

    args = parse_args()
    if args.command == "intrinsic":
        return command_intrinsic(args)
    if args.command == "intrinsic-search":
        return command_intrinsic_search(args)
    if args.command == "instruction":
        return command_instruction(args)
    if args.command == "instruction-search":
        return command_instruction_search(args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
