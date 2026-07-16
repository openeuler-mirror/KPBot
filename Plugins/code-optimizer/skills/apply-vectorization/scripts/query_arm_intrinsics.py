#!/usr/bin/env python3
"""Query and validate the structured Arm intrinsics knowledge base."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from arm_intrinsics_db_common import (
    DEFAULT_DB_DIR,
    DEFAULT_INSTRUCTION_ASSET_DIR,
    DatabaseError,
    load_json,
    load_records,
)


PRIMARY_QUERY_FIELDS = ("name", "instruction", "attribute")
CODEGEN_STYLES = ("auto", "intrinsics", "inline_asm", "assembly")
INSTRUCTION_ASSET_FILES = {
    "neon": ("simd_instructions.json",),
    "sve": ("sve_instructions.json",),
    "sve2": ("sve_instructions.json",),
    "sme": ("sme_instructions.json",),
    "sme2": ("sme_instructions.json",),
    None: ("simd_instructions.json", "sve_instructions.json", "sme_instructions.json"),
}
ASSET_FILE_ISA = {
    "simd_instructions.json": "neon",
    "sve_instructions.json": "sve",
    "sme_instructions.json": "sme",
}
STREAMING_MARKERS = ("__arm_streaming", "__arm_streaming_compatible", "__arm_locally_streaming")
ZA_OWNERSHIP_MARKERS = ('__arm_inout("za")', '__arm_new("za")', '__arm_out("za")')
NEON_TOKEN_RE = re.compile(r"\b(v(?:ld1q|st1q|dupq_n|addq|fmaq?)_[a-z0-9_]+)\b")
SVE_TOKEN_RE = re.compile(r"\b(sv[a-z0-9_]+)\b")
SME_TOKEN_RE = re.compile(r"\b(?:sv(?:mopa|zero_za|add_write_za[a-z0-9_]*|[a-z0-9_]*_za[a-z0-9_]*)|__arm_[a-z0-9_]+)\b")
SME_INLINE_ASM_RE = re.compile(
    r"\b(?:smstart|smstop|zero|fmopa|st1w|mova|whilelo|ptrue|ld1w|fadd|fmla)\b",
    re.IGNORECASE,
)
SME_ABI_RUNTIME_RE = re.compile(r"\b__(?:arm_tpidr2_(?:save|restore)|arm_za_disable)\b")
INLINE_ASM_RE = re.compile(r"\b(?:__asm__|asm)\b")
AARCH64_ASM_RE = re.compile(
    r"\b(?:ld1|st1|fadd|fmla|fmul|add|subs|b\.(?:lt|le|gt|ge|ne|eq)|ret|smstart|smstop|fmopa|st1w)\b",
    re.IGNORECASE,
)
AARCH64_PLATFORM_RESERVED_RE = re.compile(r"\b[wx]18\b", re.IGNORECASE)
AARCH64_BAD_LABEL_RE = re.compile(r"(?m)^\s*\"?\s*(\d+[A-Za-z_.$][\w.$]*):")
NEON_LD1_ST1_POST_RE = re.compile(
    r"\b(?P<op>ld1|st1)\s+\{(?P<regs>[^{}]+)\}(?P<lane>\s*\[[^\]]+\])?\s*,\s*(?P<addr>\[.*\])\s*,\s*(?P<post>[^\"\\\n;/]+)",
    re.IGNORECASE,
)
NEON_REGISTER_LIST_ITEM_RE = re.compile(
    r"^v(?P<start>\d+)\.(?P<arr>16b|8b|8h|4h|4s|2s|2d|1d)(?:\s*-\s*v(?P<end>\d+)\.(?P=arr))?$",
    re.IGNORECASE,
)
NEON_ARRANGEMENT_BYTES = {
    "16b": 16,
    "8b": 8,
    "8h": 16,
    "4h": 8,
    "4s": 16,
    "2s": 8,
    "2d": 16,
    "1d": 8,
}
NEON_FMLA_SCALAR_ELEMENT_RE = re.compile(
    r"\bfmla\s+v\d+\.(?P<dst_arr>4s|2s|2d)\s*,\s*v\d+\.(?P<src_arr>4s|2s|2d)\s*,\s*v\d+\.(?P<scalar_arr>4s|2s|2d|s|d)\[(?P<index>\d+)\]",
    re.IGNORECASE,
)
ISA_TOKEN_FAMILIES = {
    "neon": {"neon"},
    "sve": {"sve", "sve2"},
    "sme": {"sme", "sme2"},
}
EMPTY_TOKEN_RE = re.compile(r"a^")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Lookup and validate the apply-vectorization Arm intrinsics knowledge base."
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=DEFAULT_DB_DIR,
        help="Directory containing the structured intrinsics JSON snapshots.",
    )
    parser.add_argument(
        "--asset-dir",
        type=Path,
        default=DEFAULT_INSTRUCTION_ASSET_DIR,
        help="Directory containing broad ARM instruction asset JSON files used as query fallback.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command_name in ("lookup", "search"):
        command_parser = subparsers.add_parser(command_name)
        command_parser.add_argument("--name")
        command_parser.add_argument("--instruction")
        command_parser.add_argument("--attribute")
        command_parser.add_argument("--isa", choices=("neon", "sve", "sve2", "sme", "sme2"))
        command_parser.add_argument("--group")
        command_parser.add_argument("--json", action="store_true")

    validate_parser = subparsers.add_parser("validate-snippet")
    validate_parser.add_argument("--file", type=Path)
    validate_parser.add_argument("--code")
    validate_parser.add_argument("--isa", choices=("neon", "sve", "sve2", "sme", "sme2"), required=True)
    validate_parser.add_argument("--style", choices=CODEGEN_STYLES, default="auto")
    validate_parser.add_argument(
        "--semantic-contract",
        help="Optional JSON object proving aliasing, index, and bit-exactness constraints for complex SVE paths.",
    )
    validate_parser.add_argument("--semantic-contract-file", type=Path)
    validate_parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def normalize_token(value: str) -> str:
    """Normalize a query token for comparison."""

    return re.sub(r"\s+", " ", value.strip().lower())


def extract_c_identifier_tokens(value: str) -> set[str]:
    """Extract C identifier-like intrinsic or attribute tokens from a database name."""

    tokens: set[str] = set()
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", value):
        if token.startswith(("v", "sv", "__arm_")):
            tokens.add(token)
    return tokens


def build_token_re(tokens: set[str]) -> re.Pattern[str]:
    """Build a regex that matches any known database token exactly."""

    if not tokens:
        return EMPTY_TOKEN_RE
    alternatives = "|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})\b")


def build_isa_token_patterns(records: list[dict[str, Any]]) -> dict[str, re.Pattern[str]]:
    """Build per-ISA token matchers from the structured intrinsics database."""

    tokens_by_family: dict[str, set[str]] = {family: set() for family in ISA_TOKEN_FAMILIES}
    for record in records:
        for family, isas in ISA_TOKEN_FAMILIES.items():
            if record["isa"] not in isas:
                continue
            for intrinsic_name in record["intrinsic_names"]:
                tokens_by_family[family].update(extract_c_identifier_tokens(intrinsic_name))
    return {family: build_token_re(tokens) for family, tokens in tokens_by_family.items()}


def matches_group(record: dict[str, Any], group_filter: str | None) -> bool:
    """Check whether a record matches the optional group filter."""

    if not group_filter:
        return True
    return normalize_token(group_filter) in normalize_token("/".join(record["group_path"]))


def matches_isa(record: dict[str, Any], isa: str | None) -> bool:
    """Check whether a record matches the optional ISA filter."""

    return isa is None or record["isa"] == isa


def lookup_matches(record: dict[str, Any], args: argparse.Namespace) -> bool:
    """Exact-match lookup semantics."""

    if args.name:
        token = normalize_token(args.name)
        if token == normalize_token(record["display_name"]):
            return True
        return any(token == normalize_token(name) for name in record["intrinsic_names"])
    if args.instruction:
        token = normalize_token(args.instruction)
        return any(token == normalize_token(name) for name in record["instruction_names"])
    if args.attribute:
        token = normalize_token(args.attribute)
        if record["kind"] != "attribute":
            return False
        if token == normalize_token(record["display_name"]):
            return True
        return any(token == normalize_token(name) for name in record["intrinsic_names"])
    return False


def search_matches(record: dict[str, Any], args: argparse.Namespace) -> bool:
    """Substring search semantics."""

    haystacks = [
        record["display_name"],
        *record["intrinsic_names"],
        *record["instruction_names"],
        *record["group_path"],
        record["vectorization_role"],
    ]
    if args.name:
        token = normalize_token(args.name)
        if any(token in normalize_token(value) for value in haystacks):
            return True
    if args.instruction:
        token = normalize_token(args.instruction)
        if any(token in normalize_token(value) for value in record["instruction_names"]):
            return True
    if args.attribute:
        token = normalize_token(args.attribute)
        if any(token in normalize_token(value) for value in (*record["intrinsic_names"], record["display_name"])):
            return True
    if args.group:
        return matches_group(record, args.group)
    return False


def slugify(value: str) -> str:
    """Convert an instruction title to a stable record id fragment."""

    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "instruction"


def classify_instruction_groups(title: str, description: str) -> list[str]:
    """Classify broad instruction assets for general-compute searches."""

    haystack = f"{title} {description}".upper()
    groups: list[str] = []
    if any(token in haystack for token in ("AES", "SHA", "SM4", "PMULL", "RAX1", "XAR")):
        groups.append("crypto")
    if any(token in haystack for token in ("HISTCNT", "MATCH", "TBL", "TBX", "EOR", "BSL", "BDEP", "BEXT")):
        groups.append("compression")
    if any(token in haystack for token in ("EOR", "AND", "ORR", "BSL", "BIC", "XAR", "BDEP", "BEXT")):
        groups.append("bitwise")
    if any(token in haystack for token in ("LD1", "ST1", "GATHER", "SCATTER")):
        groups.append("load-store")
    return groups


def normalize_asset_payload(file_name: str, payload: Any) -> list[dict[str, Any]]:
    """Normalize one broad ARM instruction asset into a list of instruction dictionaries."""

    if file_name == "sme_instructions.json":
        if not isinstance(payload, dict) or not isinstance(payload.get("instructions"), list):
            raise DatabaseError(f"instruction asset {file_name} must contain an instructions list")
        return list(payload["instructions"])
    if not isinstance(payload, list):
        raise DatabaseError(f"instruction asset {file_name} must contain a list")
    return list(payload)


def asset_features(file_name: str, item: dict[str, Any]) -> list[str]:
    """Extract feature names from one instruction asset item."""

    if file_name == "sme_instructions.json":
        features = item.get("feats", [])
    else:
        features = item.get("features", [])
    return [str(feature) for feature in features if str(feature)]


def asset_url(file_name: str, item: dict[str, Any]) -> str:
    """Extract a source URL from one instruction asset item."""

    if file_name == "sme_instructions.json":
        return str(item.get("source_url", ""))
    return str(item.get("url", ""))


def asset_syntax(file_name: str, item: dict[str, Any]) -> list[str]:
    """Extract assembly syntax strings from one instruction asset item."""

    if file_name == "sme_instructions.json":
        encodings = item.get("encodings", [])
        if not isinstance(encodings, list):
            return []
        values: list[str] = []
        for encoding in encodings:
            if not isinstance(encoding, dict):
                continue
            syntax = encoding.get("syntax")
            if syntax:
                values.append(str(syntax))
        return values
    syntax = item.get("syntax", [])
    if isinstance(syntax, list):
        return [str(value) for value in syntax if str(value)]
    return [str(syntax)] if syntax else []


def asset_record_from_item(file_name: str, item: dict[str, Any], requested_isa: str | None) -> dict[str, Any]:
    """Convert one broad instruction asset item to the query record shape."""

    family_isa = ASSET_FILE_ISA[file_name]
    record_isa = requested_isa if requested_isa in {"sve", "sve2", "sme", "sme2"} else family_isa
    title = str(item.get("title", "")).strip()
    description = str(item.get("description", "")).strip()
    features = asset_features(file_name, item)
    syntax = asset_syntax(file_name, item)
    group_path = ["instruction-assets", family_isa, *classify_instruction_groups(title, description)]
    if file_name == "sme_instructions.json" and item.get("category"):
        group_path.append(str(item["category"]).lower())
    group_path.extend(feature.lower().replace("feat_", "") for feature in features[:3])
    if len(group_path) == 2:
        group_path.append("general")

    return {
        "id": f"asset.{family_isa}.{slugify(title)}",
        "kind": "instruction",
        "isa": record_isa,
        "group_path": group_path,
        "display_name": title,
        "intrinsic_names": [],
        "instruction_names": [title.split(" ", 1)[0], title],
        "prototype": "; ".join(syntax),
        "header": "<arm_sve.h>" if family_isa == "sve" else "",
        "feature_macros": features,
        "required_function_attributes": [],
        "operand_constraints": [],
        "immediate_constraints": [],
        "vectorization_role": "broad-instruction-reference",
        "tail_policy": "Instruction asset only; generated SVE code must still pass apply-vectorization predicate and bounds rules.",
        "usage_template": "\n".join(syntax),
        "correctness_rules": [
            "This record comes from broad ARM instruction assets and is reference material, not a direct intrinsic recipe.",
            "Check ACLE intrinsic spelling, feature gates, predicates, and compile-only validation before emitting code.",
        ],
        "anti_patterns": [
            "Do not infer memory safety, bit-exactness, or profitability from instruction existence alone.",
        ],
        "related_items": [],
        "source": {
            "title": "ARM instruction asset",
            "url": asset_url(file_name, item),
            "section": title,
            "retrieved_at": "asset-snapshot",
        },
        "asset_source": file_name,
        "asset_description": description,
        "asset_pseudocode": str(item.get("pseudocode", "")),
        "asset_operational_info": str(item.get("operational_info", item.get("decode", ""))),
    }


def load_instruction_asset_records(asset_dir: Path, isa: str | None) -> list[dict[str, Any]]:
    """Load broad instruction asset records for query fallback."""

    records: list[dict[str, Any]] = []
    for file_name in INSTRUCTION_ASSET_FILES.get(isa, ()):
        path = asset_dir / file_name
        if not path.exists():
            continue
        for item in normalize_asset_payload(file_name, load_json(path)):
            if not isinstance(item, dict):
                continue
            if not str(item.get("title", "")).strip():
                continue
            records.append(asset_record_from_item(file_name, item, isa))
    return records


def query_asset_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Query broad instruction assets after the curated DB misses."""

    records = load_instruction_asset_records(args.asset_dir, args.isa)
    predicate = lookup_matches if args.command == "lookup" else search_matches
    return [record for record in records if matches_group(record, args.group) and predicate(record, args)]


def require_query_term(args: argparse.Namespace) -> None:
    """Ensure the caller supplied at least one useful query term."""

    if any(getattr(args, field_name) for field_name in PRIMARY_QUERY_FIELDS):
        return
    if args.command == "search" and args.group:
        return
    raise DatabaseError(f"{args.command} requires at least one of --name, --instruction, --attribute or --group")


def render_match(record: dict[str, Any]) -> str:
    """Render one lookup/search match as text."""

    lines = [f"`{record['display_name']}` [{record['kind']}/{record['isa']}]"]
    if record["prototype"]:
        lines.append(f"  prototype: {record['prototype']}")
    if record["header"]:
        lines.append(f"  header: {record['header']}")
    if record["feature_macros"]:
        lines.append("  feature_macros: " + ", ".join(record["feature_macros"]))
    if record["required_function_attributes"]:
        lines.append(
            "  required_function_attributes: "
            + ", ".join(record["required_function_attributes"])
        )
    lines.append("  group_path: " + " / ".join(record["group_path"]))
    lines.append("  usage: " + record["usage_template"].replace("\n", " ").strip())
    if record["correctness_rules"]:
        lines.append("  correctness: " + " | ".join(record["correctness_rules"]))
    lines.append(
        "  source: "
        + f"{record['source']['title']} / {record['source']['section']} / {record['source']['url']}"
    )
    return "\n".join(lines)


def load_code(args: argparse.Namespace) -> str:
    """Load the code snippet to validate."""

    if bool(args.file) == bool(args.code):
        raise DatabaseError("validate-snippet requires exactly one of --file or --code")
    if args.file:
        return args.file.read_text(encoding="utf-8")
    return str(args.code)


def load_semantic_contract(args: argparse.Namespace) -> dict[str, Any]:
    """Load optional semantic contract proof metadata for snippet validation."""

    if args.semantic_contract and args.semantic_contract_file:
        raise DatabaseError("use only one of --semantic-contract or --semantic-contract-file")
    if args.semantic_contract_file:
        payload = load_json(args.semantic_contract_file)
    elif args.semantic_contract:
        try:
            payload = json.loads(args.semantic_contract)
        except json.JSONDecodeError as exc:
            raise DatabaseError(f"invalid --semantic-contract JSON: {exc}") from exc
    else:
        return {}
    if not isinstance(payload, dict):
        raise DatabaseError("semantic contract must be a JSON object")
    return payload


def collect_evidence(pattern: re.Pattern[str], code: str) -> list[str]:
    """Collect up to three regex matches for diagnostics."""

    matches = pattern.findall(code)
    if isinstance(matches, list) and matches and isinstance(matches[0], tuple):
        values = [item[0] for item in matches]
    else:
        values = list(matches)
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
        if len(seen) == 3:
            break
    return seen


def build_finding(
    *,
    rule_id: str,
    severity: str,
    message: str,
    matched_evidence: list[str],
    suggested_fix: str,
) -> dict[str, Any]:
    """Build one validation finding."""

    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "matched_evidence": matched_evidence,
        "suggested_fix": suggested_fix,
    }


def calculate_neon_register_list_bytes(register_list: str) -> int | None:
    """Return the transferred byte count for a NEON LD1/ST1 register list."""

    total_bytes = 0
    expected_arrangement: str | None = None
    for raw_item in register_list.split(","):
        item = raw_item.strip().lower()
        match = NEON_REGISTER_LIST_ITEM_RE.match(item)
        if not match:
            return None
        arrangement = match.group("arr").lower()
        if expected_arrangement is None:
            expected_arrangement = arrangement
        elif arrangement != expected_arrangement:
            return None
        start = int(match.group("start"))
        end_text = match.group("end")
        end = int(end_text) if end_text is not None else start
        if end < start:
            return None
        total_bytes += (end - start + 1) * NEON_ARRANGEMENT_BYTES[arrangement]
    return total_bytes if total_bytes > 0 else None


def collect_neon_ld1_st1_post_index_findings(code: str) -> list[dict[str, Any]]:
    """Find invalid NEON LD1/ST1 post-index forms in generated assembly."""

    findings: list[dict[str, Any]] = []
    for match in NEON_LD1_ST1_POST_RE.finditer(code):
        if match.group("lane"):
            continue
        post_index = match.group("post").strip()
        evidence = match.group(0).strip()
        if re.match(r"^\d", post_index):
            findings.append(
                build_finding(
                    rule_id="rule.aarch64.neon-ld1st1-post-index",
                    severity="error",
                    message="NEON LD1/ST1 post-index immediate must use #imm syntax.",
                    matched_evidence=[evidence],
                    suggested_fix="Use forms such as `ld1 {v0.4s, v1.4s}, [x0], #32`; use a register post-index operand only as `, xN`.",
                )
            )
            continue
        immediate_match = re.match(r"^#(?P<imm>\d+)\b", post_index)
        if not immediate_match:
            continue
        expected_bytes = calculate_neon_register_list_bytes(match.group("regs"))
        if expected_bytes is None:
            continue
        actual_bytes = int(immediate_match.group("imm"))
        if actual_bytes != expected_bytes:
            findings.append(
                build_finding(
                    rule_id="rule.aarch64.neon-ld1st1-post-index",
                    severity="error",
                    message="NEON LD1/ST1 post-index immediate must match the transferred register-list size.",
                    matched_evidence=[evidence],
                    suggested_fix=(
                        f"Use #{expected_bytes} for this register list, or split the load/store and pointer update "
                        "if a different address increment is required."
                    ),
                )
            )
    return findings


def infer_codegen_style(code: str) -> str:
    """Infer code generation style from snippet shape when --style auto is used."""

    if INLINE_ASM_RE.search(code):
        return "inline_asm"
    if re.search(r"(?m)^\s*\.(?:text|globl|global|type|p2align|arch)\b", code) or re.search(
        r"(?m)^\s*[A-Za-z_.$][\w.$]*:\s*$", code
    ):
        return "assembly"
    return "intrinsics"


def validate_rule_neon_explicit_epilogue(code: str, isa: str) -> dict[str, Any] | None:
    """Detect missing scalar epilogues in NEON loops."""

    if isa != "neon" and not NEON_TOKEN_RE.search(code):
        return None
    has_fixed_step = re.search(r"\+\=\s*(4|8|16)\b", code) is not None
    has_scalar_tail = re.search(r"for\s*\(\s*;\s*[A-Za-z_]\w*\s*<", code) is not None or re.search(
        r"while\s*\(\s*[A-Za-z_]\w*\s*<", code
    )
    if has_fixed_step and not has_scalar_tail:
        return build_finding(
            rule_id="rule.neon.explicit-epilogue",
            severity="error",
            message="NEON fixed-width loop is missing an explicit scalar epilogue.",
            matched_evidence=collect_evidence(NEON_TOKEN_RE, code) or ["fixed-step loop"],
            suggested_fix="Keep a scalar cleanup loop after the NEON main loop, or prove the trip count is a multiple of the NEON lane width.",
        )
    return None


def validate_rule_style_no_inline_asm_in_intrinsics(code: str, style: str) -> dict[str, Any] | None:
    """Detect inline assembly inside an intrinsics-only candidate."""

    if style != "intrinsics" or not INLINE_ASM_RE.search(code):
        return None
    return build_finding(
        rule_id="rule.codegen-style.intrinsics-no-inline-asm",
        severity="error",
        message="intrinsics style must not contain inline assembly.",
        matched_evidence=collect_evidence(INLINE_ASM_RE, code),
        suggested_fix="Use codegen_style=inline_asm for C/C++ code that intentionally contains asm/__asm__ blocks.",
    )


def validate_rule_style_inline_asm_present(code: str, style: str) -> dict[str, Any] | None:
    """Require visible inline assembly for inline_asm candidates."""

    if style != "inline_asm" or INLINE_ASM_RE.search(code):
        return None
    return build_finding(
        rule_id="rule.codegen-style.inline-asm-present",
        severity="error",
        message="inline_asm style must contain a visible asm/__asm__ block.",
        matched_evidence=[],
        suggested_fix="Generate a C/C++ translation unit with an asm volatile block, or use intrinsics style.",
    )


def validate_rule_style_inline_asm_clobber(code: str, style: str) -> dict[str, Any] | None:
    """Require a memory clobber for inline assembly that touches vector memory paths."""

    if style != "inline_asm" or not INLINE_ASM_RE.search(code):
        return None
    if '"memory"' in code or "'memory'" in code:
        return None
    return build_finding(
        rule_id="rule.codegen-style.inline-asm-clobber",
        severity="error",
        message="inline asm candidate is missing a memory clobber.",
        matched_evidence=collect_evidence(INLINE_ASM_RE, code),
        suggested_fix='Add a "memory" clobber and list every vector/predicate/general register modified by the asm block.',
    )


def validate_rule_style_assembly_shape(code: str, style: str) -> dict[str, Any] | None:
    """Require standalone assembly candidates to look like real AArch64 assembly."""

    if style != "assembly":
        return None
    missing: list[str] = []
    if not re.search(r"(?m)^\s*\.(?:text|globl|global)\b", code):
        missing.append(".text/.globl")
    if not re.search(r"(?m)^\s*[A-Za-z_.$][\w.$]*:\s*$", code):
        missing.append("function label")
    if not re.search(r"(?m)^\s*ret\b", code):
        missing.append("ret")
    if not AARCH64_ASM_RE.search(code):
        missing.append("AArch64 instruction body")
    if not missing:
        return None
    return build_finding(
        rule_id="rule.codegen-style.assembly-shape",
        severity="error",
        message="assembly style must provide a standalone AArch64 assembly function.",
        matched_evidence=collect_evidence(AARCH64_ASM_RE, code),
        suggested_fix="Emit a .S artifact with .text, .globl, a callable function label, AArch64 instructions and ret. Missing: "
        + ", ".join(missing),
    )


def validate_rule_style_no_c_wrappers_in_assembly(code: str, style: str) -> dict[str, Any] | None:
    """Reject C-only constructs when the snippet itself is declared as assembly."""

    if style != "assembly":
        return None
    has_c_function = re.search(r"\b[A-Za-z_]\w*\s*\([^;{}]*\)\s*\{", code) is not None
    if "#include" not in code and not has_c_function:
        return None
    return build_finding(
        rule_id="rule.codegen-style.assembly-no-c-wrapper",
        severity="error",
        message="assembly style validation expects the .S artifact body, not the C wrapper.",
        matched_evidence=["#include" if "#include" in code else "C function body"],
        suggested_fix="Validate the .S artifact with --style assembly; validate any C wrapper separately as intrinsics or inline_asm.",
    )


def validate_rule_aarch64_platform_reserved_register(code: str, style: str) -> dict[str, Any] | None:
    """Reject platform-reserved AArch64 registers in generated asm paths."""

    if style not in {"inline_asm", "assembly"} or not AARCH64_PLATFORM_RESERVED_RE.search(code):
        return None
    return build_finding(
        rule_id="rule.aarch64.platform-reserved-register",
        severity="error",
        message="generated AArch64 assembly must not use x18/w18 as a temporary register.",
        matched_evidence=collect_evidence(AARCH64_PLATFORM_RESERVED_RE, code),
        suggested_fix=(
            "Use ordinary caller-saved temporaries such as x9-x17, or save/restore only registers that the "
            "target platform ABI permits. x18 is platform-reserved on Darwin/Apple arm64."
        ),
    )


def validate_rule_aarch64_label_identifier(code: str, style: str) -> dict[str, Any] | None:
    """Reject non-numeric labels that start with a digit."""

    if style not in {"inline_asm", "assembly"} or not AARCH64_BAD_LABEL_RE.search(code):
        return None
    return build_finding(
        rule_id="rule.aarch64.label-identifier",
        severity="error",
        message="AArch64 assembly labels must not start with a digit unless they are pure numeric local labels.",
        matched_evidence=collect_evidence(AARCH64_BAD_LABEL_RE, code),
        suggested_fix="Rename labels like `4x4_M_loop:` to `M4x4_loop:` or `.L4x4_M_loop:`; pure numeric locals such as `1:` with `1b/1f` references are still valid.",
    )


def validate_rule_aarch64_neon_ld1_st1_post_index(code: str, style: str) -> dict[str, Any] | None:
    """Reject common invalid NEON LD1/ST1 post-index forms."""

    if style not in {"inline_asm", "assembly"}:
        return None
    findings = collect_neon_ld1_st1_post_index_findings(code)
    return findings[0] if findings else None


def validate_rule_aarch64_fmla_scalar_element(code: str, style: str) -> dict[str, Any] | None:
    """Reject invalid NEON FMLA scalar-by-element operand forms."""

    if style not in {"inline_asm", "assembly"}:
        return None
    for match in NEON_FMLA_SCALAR_ELEMENT_RE.finditer(code):
        dst_arrangement = match.group("dst_arr").lower()
        src_arrangement = match.group("src_arr").lower()
        scalar_arrangement = match.group("scalar_arr").lower()
        index = int(match.group("index"))
        expected_scalar = "d" if dst_arrangement.endswith("d") else "s"
        max_index = 1 if dst_arrangement in {"2s", "2d"} else 3
        if (
            src_arrangement != dst_arrangement
            or scalar_arrangement != expected_scalar
            or index > max_index
        ):
            return build_finding(
                rule_id="rule.aarch64.fmla-scalar-element",
                severity="error",
                message="NEON FMLA scalar-by-element form must use a scalar element suffix and a valid lane index.",
                matched_evidence=[match.group(0)],
                suggested_fix=(
                    f"Use `fmla vD.{dst_arrangement}, vN.{dst_arrangement}, vM.{expected_scalar}[lane]` "
                    f"with lane in 0..{max_index}; do not write the scalar operand as `vM.{dst_arrangement}[lane]`."
                ),
            )
    return None


def validate_rule_sme_assembly_za_shape(code: str, isa: str, style: str) -> dict[str, Any] | None:
    """Detect incomplete standalone SME ZA assembly kernels."""

    if style != "assembly" or isa not in {"sme", "sme2"}:
        return None
    lower_code = code.lower()
    uses_za = any(token in lower_code for token in ("smstart za", "zero {za}", "fmopa", "st1w", "mova"))
    if not uses_za:
        return None
    missing = [required for required in ("smstart za", "zero {za}", "fmopa", "st1w", "smstop za") if required not in lower_code]
    if not missing:
        return None
    return build_finding(
        rule_id="rule.sme.assembly-standalone-za",
        severity="error",
        message="SME ZA standalone assembly is missing required ZA state-management instructions.",
        matched_evidence=collect_evidence(SME_INLINE_ASM_RE, code),
        suggested_fix="Keep smstart za, zero {za}, fmopa, predicated st1w and smstop za in the .S path. Missing: "
        + ", ".join(missing),
    )


def validate_rule_sve_length_agnostic(code: str, isa: str) -> dict[str, Any] | None:
    """Detect fixed-width loop steps in SVE code."""

    uses_sve = isa in {"sve", "sve2"} or bool(re.search(r"\bsv(?:cnt|whilelt|ld1|st1|dup|add|mla|dot)", code))
    if not uses_sve:
        return None
    has_fixed_step = re.search(r"\+\=\s*(2|4|8|16|32|64)\b", code) is not None
    uses_scalable_step = re.search(r"svcnt[a-z0-9_]*\s*\(", code) is not None or "rdsvl" in code
    if has_fixed_step and not uses_scalable_step:
        return build_finding(
            rule_id="rule.sve.length-agnostic",
            severity="error",
            message="SVE loop uses a hard-coded lane count instead of a scalable vector-length step.",
            matched_evidence=collect_evidence(SVE_TOKEN_RE, code) or ["fixed-step SVE loop"],
            suggested_fix="Advance the loop with svcnt*() and keep the body vector-length agnostic.",
        )
    return None


def validate_rule_sve_predicated_load_store(code: str, isa: str) -> dict[str, Any] | None:
    """Detect missing predicate coverage around SVE loads and stores."""

    if isa not in {"sve", "sve2"} and not re.search(r"\bsv(?:ld1|st1)_[a-z0-9_]+\b", code):
        return None
    if not re.search(r"\bsv(?:ld1|st1)_[a-z0-9_]+\b", code):
        return None
    if re.search(r"\bsv(?:ld1|st1)_[a-z0-9_]+\s*\(\s*svptrue_[a-z0-9_]*\s*\(", code):
        return build_finding(
            rule_id="rule.sve.tail-predicate",
            severity="error",
            message="SVE load/store uses a bare svptrue predicate and can overrun the tail.",
            matched_evidence=collect_evidence(re.compile(r"\bsvptrue_[a-z0-9_]+\b"), code),
            suggested_fix="Build the load/store predicate from the current loop boundary with svwhilelt_*.",
        )
    has_predicate = re.search(r"\bsvwhilelt_[a-z0-9_]+\b", code)
    if not has_predicate:
        return build_finding(
            rule_id="rule.sve.predicated-load-store",
            severity="error",
            message="SVE load/store sequence is missing visible loop-boundary predicate generation.",
            matched_evidence=collect_evidence(re.compile(r"\bsv(?:ld1|st1)_[a-z0-9_]+\b"), code),
            suggested_fix="Create an svbool_t predicate with svwhilelt_* for the active loop range and pass it consistently to SVE load/store intrinsics.",
        )
    return None


def sve_element_width_from_tokens(code: str) -> set[str]:
    """Infer SVE element widths used by typed intrinsics in a snippet."""

    widths: set[str] = set()
    suffix_widths = {
        "8": ("_s8", "_u8"),
        "16": ("_s16", "_u16", "_f16", "_bf16"),
        "32": ("_s32", "_u32", "_f32"),
        "64": ("_s64", "_u64", "_f64"),
    }
    for width, suffixes in suffix_widths.items():
        if any(suffix in code for suffix in suffixes):
            widths.add(width)
    return widths


def validate_rule_sve_vl_matches_element_width(code: str, isa: str) -> dict[str, Any] | None:
    """Detect svcnt* loop steps that do not match the SVE element width."""

    if isa not in {"sve", "sve2"} and "svcnt" not in code:
        return None
    cnt_widths = set()
    for suffix, width in (("b", "8"), ("h", "16"), ("w", "32"), ("d", "64")):
        if re.search(rf"\bsvcnt{suffix}\s*\(", code):
            cnt_widths.add(width)
    if not cnt_widths:
        return None
    element_widths = sve_element_width_from_tokens(code)
    if not element_widths or cnt_widths & element_widths:
        return None
    return build_finding(
        rule_id="rule.sve.vl-element-width",
        severity="error",
        message="SVE loop step uses svcnt* for a different element width than the typed operations.",
        matched_evidence=collect_evidence(re.compile(r"\bsvcnt[bhwd]\b|\bsv[a-z0-9_]+_(?:s|u|f|bf)(?:8|16|32|64)\b"), code),
        suggested_fix="Use svcntb/h/w/d that matches the element width loaded, stored, and advanced by the pointer.",
    )


def validate_rule_sve_gather_scatter_contract(
    code: str,
    isa: str,
    semantic_contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Require explicit proof metadata for SVE gather/scatter paths."""

    if isa not in {"sve", "sve2"}:
        return None
    uses_gather = bool(re.search(r"\bsvld1_gather_[a-z0-9_]+\b", code))
    uses_scatter = bool(re.search(r"\bsvst1_scatter_[a-z0-9_]+\b", code))
    if not uses_gather and not uses_scatter:
        return None

    index_properties = set(semantic_contract.get("index_properties", []))
    aliasing = semantic_contract.get("aliasing")
    missing: list[str] = []
    for required in ("readonly", "in_bounds"):
        if required not in index_properties:
            missing.append(required)
    if uses_scatter and "unique" not in index_properties:
        missing.append("unique")
    if aliasing not in {"no_overlap", "restrict", "no_alias"}:
        missing.append("no_overlap aliasing")
    if not missing:
        return None

    return build_finding(
        rule_id="rule.sve.gather-scatter-contract",
        severity="error",
        message="SVE gather/scatter requires explicit alias, bounds, and uniqueness proof metadata.",
        matched_evidence=collect_evidence(re.compile(r"\bsv(?:ld1_gather|st1_scatter)_[a-z0-9_]+\b"), code),
        suggested_fix="Provide semantic_contract with aliasing=no_overlap, index_properties including readonly and in_bounds, and unique for scatter or read-modify-write scatter. Missing: "
        + ", ".join(missing),
    )


def validate_rule_sve_general_compute_risk(code: str, isa: str) -> dict[str, Any] | None:
    """Reject high-risk SVE compression/crypto shapes that need algorithm-specific proof."""

    if isa not in {"sve", "sve2"} or not SVE_TOKEN_RE.search(code):
        return None
    lower_code = code.lower()
    risk_patterns = [
        (
            "variable-length parser",
            r"\b(?:token|literal_len|match_len|var_len)\b.*(?:\+=\s*(?:token|literal_len|match_len|len)|while\s*\()",
            "Keep variable-length token parsing scalar unless the request proves fixed independent blocks.",
        ),
        (
            "overlapping match copy",
            r"\b(?:offset|match)\b.*\b(?:dst|out)\s*\+\s*i\s*-",
            "Reject LZ-style overlapping match copy or use a memmove-equivalent scalar path.",
        ),
        (
            "variable shift",
            r"(?:<<|>>)\s*(?:shift|bits|len|n)\b",
            "Mask or range-prove variable shifts before generating SVE bit-manipulation code.",
        ),
        (
            "signed overflow",
            r"\bsv(?:add|sub|mul)_s(?:8|16|32|64)\b",
            "Use unsigned arithmetic or prove that signed overflow cannot occur in bit-exact byte/crypto code.",
        ),
    ]
    for label, pattern, fix in risk_patterns:
        if re.search(pattern, lower_code, re.DOTALL):
            return build_finding(
                rule_id="rule.sve.general-compute-risk",
                severity="error",
                message=f"SVE general-compute candidate contains a high-risk {label} pattern.",
                matched_evidence=[label],
                suggested_fix=fix,
            )
    return None


def validate_rule_sve_header_required(code: str, isa: str) -> dict[str, Any] | None:
    """Detect missing Arm SIMD headers."""

    uses_sve = bool(re.search(r"\bsv[a-z0-9_]+\b", code))
    uses_sme = bool(re.search(r"\b(?:sv(?:mopa|zero_za|add_write_za)|__arm_)", code))
    has_sve_header = "#include <arm_sve.h>" in code or "#include <arm_sme.h>" in code
    has_sme_header = "#include <arm_sme.h>" in code
    if uses_sme and not has_sme_header:
        return build_finding(
            rule_id="rule.sve.header-required",
            severity="error",
            message="SME or ZA-specific code is missing <arm_sme.h>.",
            matched_evidence=collect_evidence(SME_TOKEN_RE, code),
            suggested_fix="Include <arm_sme.h> for SME attributes and ZA intrinsics.",
        )
    if uses_sve and not has_sve_header:
        return build_finding(
            rule_id="rule.sve.header-required",
            severity="error",
            message="SVE code is missing <arm_sve.h> or <arm_sme.h>.",
            matched_evidence=collect_evidence(SVE_TOKEN_RE, code),
            suggested_fix="Include <arm_sve.h> for SVE code, or <arm_sme.h> when the code is an SME path.",
        )
    return None


def validate_rule_sme_streaming_required(code: str, isa: str) -> dict[str, Any] | None:
    """Detect SME intrinsics without a streaming-capable context."""

    uses_sme = isa in {"sme", "sme2"} or bool(re.search(r"\b(?:sv(?:mopa|zero_za|add_write_za)|__arm_(?:inout|new|out))", code))
    if not uses_sme:
        return None
    if not any(marker in code for marker in STREAMING_MARKERS):
        return build_finding(
            rule_id="rule.sme.streaming-required",
            severity="error",
            message="SME or ZA intrinsics appear without a streaming-capable function context.",
            matched_evidence=collect_evidence(SME_TOKEN_RE, code),
            suggested_fix="Add __arm_streaming or __arm_streaming_compatible as appropriate for the SME path.",
        )
    return None


def validate_rule_sme_za_ownership(code: str, isa: str) -> dict[str, Any] | None:
    """Detect ZA state usage without an ownership attribute."""

    uses_za = bool(re.search(r"\b(?:svzero_za|sv[a-z0-9_]*_za[a-z0-9_]*|svmopa_[a-z0-9_]+)\b", code))
    if not uses_za:
        return None
    if not any(marker in code for marker in ZA_OWNERSHIP_MARKERS):
        return build_finding(
            rule_id="rule.sme.za-ownership",
            severity="error",
            message="ZA state is used without an explicit ownership attribute.",
            matched_evidence=collect_evidence(re.compile(r"\b(?:svzero_za|sv[a-z0-9_]*_za[a-z0-9_]*|svmopa_[a-z0-9_]+)\b"), code),
            suggested_fix='Add one of __arm_inout("za"), __arm_new("za") or __arm_out("za") to the function declaration.',
        )
    return None


def validate_rule_sme_zero_za(code: str, isa: str) -> dict[str, Any] | None:
    """Detect svzero_za() without fresh-ZA intent."""

    if "svzero_za" not in code:
        return None
    if '__arm_new("za")' not in code and '__arm_out("za")' not in code:
        return build_finding(
            rule_id="rule.sme.zero-za-fresh-za",
            severity="error",
            message="svzero_za() is present without fresh-ZA or explicit ZA output semantics.",
            matched_evidence=["svzero_za()"],
            suggested_fix='Reserve svzero_za() for a fresh-ZA path such as __arm_new("za") or an explicit __arm_out("za") flow.',
        )
    return None


def validate_rule_sme_inline_asm_shape(code: str, isa: str) -> dict[str, Any] | None:
    """Detect incomplete standalone SME ZA inline-asm kernels."""

    lower_code = code.lower()
    uses_za_inline_asm = isa in {"sme", "sme2"} and bool(
        re.search(r"\b(?:fmopa|st1w|mova)\b", lower_code)
        or "zero {za}" in lower_code
        or "smstart za" in lower_code
    )
    if not uses_za_inline_asm:
        return None
    missing: list[str] = []
    for required in ("smstart za", "zero {za}", "fmopa", "st1w", "smstop za"):
        if required not in lower_code:
            missing.append(required)
    if '"za"' not in code and "'za'" not in code:
        missing.append("za clobber")
    if not any(marker in code for marker in ("__arm_streaming", "__arm_locally_streaming")):
        missing.append("__arm_streaming or __arm_locally_streaming")
    if '__arm_new("za")' in code:
        missing.append('remove unverified __arm_new("za") from inline-asm standalone path')
    if missing:
        return build_finding(
            rule_id="rule.sme.inline-asm-standalone-za",
            severity="error",
            message="SME ZA inline asm is missing the standalone state-management and verification shape.",
            matched_evidence=collect_evidence(SME_INLINE_ASM_RE, code) or ["SME inline asm"],
            suggested_fix=(
                "Use __arm_streaming plus inline asm containing smstart za, zero {za}, fmopa, "
                "predicated st1w and smstop za; clobber za and memory; keep __arm_new(\"za\") "
                "only for a separately verified SME ABI runtime path. Missing: " + ", ".join(missing)
            ),
        )
    return None


def validate_rule_sme_no_abi_runtime_stubs(code: str, isa: str) -> dict[str, Any] | None:
    """Detect generated weak stubs or direct references for SME ABI support routines."""

    if isa not in {"sme", "sme2"} and "__arm_tpidr2" not in code and "__arm_za_disable" not in code:
        return None
    if "__arm_tpidr2" not in code and "__arm_za_disable" not in code:
        return None
    weak_stub = "__attribute__((weak" in code or "__attribute__((__weak__" in code
    return build_finding(
        rule_id="rule.sme.no-abi-runtime-stubs",
        severity="error",
        message="Generated SME source must not define or depend on fake SME ABI support routine stubs.",
        matched_evidence=collect_evidence(SME_ABI_RUNTIME_RE, code) or ["__arm_tpidr2/__arm_za_disable"],
        suggested_fix=(
            "Remove weak stubs and either compile a no-runtime inline-asm ZA path or verify that the real "
            "target link environment provides the SME ABI support routines."
            + (" The snippet appears to contain weak stub syntax." if weak_stub else "")
        ),
    )


def validate_rule_cross_isa(
    code: str,
    isa: str,
    token_patterns: dict[str, re.Pattern[str]],
) -> dict[str, Any] | None:
    """Detect mixed ISA templates in one kernel body."""

    has_neon = bool(token_patterns["neon"].search(code))
    has_sve = bool(token_patterns["sve"].search(code))
    has_sme = bool(token_patterns["sme"].search(code))
    has_plain_sve = has_sve and not has_sme
    families = sum(int(value) for value in (has_neon, has_plain_sve, has_sme))
    if families >= 2:
        evidence: list[str] = []
        if has_neon:
            evidence.extend(collect_evidence(token_patterns["neon"], code))
        if has_plain_sve:
            evidence.extend(collect_evidence(token_patterns["sve"], code))
        if has_sme:
            evidence.extend(collect_evidence(token_patterns["sme"], code))
        return build_finding(
            rule_id="rule.cross-isa.no-mixed-templates",
            severity="error",
            message="The snippet mixes multiple SIMD ISA templates in one kernel body.",
            matched_evidence=evidence[:3],
            suggested_fix="Keep one ISA template per generated kernel and move cross-ISA dispatch to a higher-level wrapper.",
        )
    return None


VALIDATORS: tuple[Callable[[str, str], dict[str, Any] | None], ...] = (
    validate_rule_neon_explicit_epilogue,
    validate_rule_sve_length_agnostic,
    validate_rule_sve_vl_matches_element_width,
    validate_rule_sve_predicated_load_store,
    validate_rule_sve_general_compute_risk,
    validate_rule_sve_header_required,
    validate_rule_sme_streaming_required,
    validate_rule_sme_za_ownership,
    validate_rule_sme_zero_za,
    validate_rule_sme_inline_asm_shape,
    validate_rule_sme_no_abi_runtime_stubs,
)

STYLE_VALIDATORS: tuple[Callable[[str, str], dict[str, Any] | None], ...] = (
    validate_rule_style_no_inline_asm_in_intrinsics,
    validate_rule_style_inline_asm_present,
    validate_rule_style_inline_asm_clobber,
    validate_rule_style_assembly_shape,
    validate_rule_style_no_c_wrappers_in_assembly,
    validate_rule_aarch64_platform_reserved_register,
    validate_rule_aarch64_label_identifier,
    validate_rule_aarch64_neon_ld1_st1_post_index,
    validate_rule_aarch64_fmla_scalar_element,
)


def run_query(args: argparse.Namespace) -> int:
    """Execute lookup/search commands."""

    require_query_term(args)
    records = load_records(args.db_dir)
    predicate = lookup_matches if args.command == "lookup" else search_matches
    matches = [
        record
        for record in records
        if matches_isa(record, args.isa) and matches_group(record, args.group) and predicate(record, args)
    ]
    if not matches:
        matches = query_asset_records(args)

    if args.json:
        print(json.dumps({"matches": matches}, ensure_ascii=False, indent=2))
    elif matches:
        for record in matches:
            print(render_match(record))
            print()
    else:
        print("[未命中] 没有找到匹配的记录", file=sys.stderr)
    return 0 if matches else 1


def run_validation(args: argparse.Namespace) -> int:
    """Execute validate-snippet."""

    records = load_records(args.db_dir)
    token_patterns = build_isa_token_patterns(records)
    code = load_code(args)
    semantic_contract = load_semantic_contract(args)
    style = infer_codegen_style(code) if args.style == "auto" else args.style
    findings: list[dict[str, Any]] = []
    if style != "assembly":
        for validator in VALIDATORS:
            finding = validator(code, args.isa)
            if finding:
                findings.append(finding)
        gather_scatter_finding = validate_rule_sve_gather_scatter_contract(
            code,
            args.isa,
            semantic_contract,
        )
        if gather_scatter_finding:
            findings.append(gather_scatter_finding)
        cross_isa_finding = validate_rule_cross_isa(code, args.isa, token_patterns)
        if cross_isa_finding:
            findings.append(cross_isa_finding)
    for validator in STYLE_VALIDATORS:
        finding = validator(code, style)
        if finding:
            findings.append(finding)
    sme_assembly_finding = validate_rule_sme_assembly_za_shape(code, args.isa, style)
    if sme_assembly_finding:
        findings.append(sme_assembly_finding)

    findings.sort(key=lambda item: (item["rule_id"], item["severity"], item["message"]))
    payload = {"isa": args.isa, "codegen_style": style, "valid": not findings, "findings": findings}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif findings:
        for finding in findings:
            print(f"[{finding['severity']}] {finding['rule_id']}: {finding['message']}")
            print("  evidence: " + ", ".join(finding["matched_evidence"]))
            print("  fix: " + finding["suggested_fix"])
    else:
        print("[通过] 未发现静态 ISA 形态问题")
    return 0 if not findings else 1


def main() -> int:
    """CLI entry point."""

    args = parse_args()
    try:
        if args.command in {"lookup", "search"}:
            return run_query(args)
        return run_validation(args)
    except FileNotFoundError as exc:
        print(f"[文件错误] {exc}", file=sys.stderr)
        return 2
    except DatabaseError as exc:
        print(f"[数据库错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
