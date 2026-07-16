#!/usr/bin/env python3
"""Refresh the curated Arm intrinsics knowledge base from official sources."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from arm_intrinsics_db_common import DEFAULT_DB_DIR, SNAPSHOT_FILES, record_sort_key, save_json

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - exercised by runtime environment
    print(f"[依赖错误] refresh_arm_intrinsics_db.py 需要 requests 和 beautifulsoup4: {exc}", file=sys.stderr)
    sys.exit(2)


ACLE_URL = "https://arm-software.github.io/acle/main/acle.html"
NEON_URL = "https://arm-software.github.io/acle/neon_intrinsics/advsimd.html"
ARM_SIMD_URL = "https://developer.arm.com/servers-and-cloud-computing/arm-simd"
SME_SEARCH_ANNOUNCEMENT_URL = (
    "https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/"
    "scalable-matrix-extension-expanding-the-arm-intrinsics-search-engine"
)
SME_INTRO_BLOG_URL = (
    "https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/"
    "arm-scalable-matrix-extension-introduction"
)
SME_INSTRUCTIONS_BLOG_URL = (
    "https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/"
    "arm-scalable-matrix-extension-introduction-p2"
)
DDI0602_SME_INSTRUCTIONS_URL = "https://developer.arm.com/documentation/ddi0602/latest/SME-Instructions"
DDI0602_SVE_INSTRUCTIONS_URL = "https://developer.arm.com/documentation/ddi0602/latest/SVE-Instructions"
FETCH_TIMEOUT_SECONDS = 30
USER_AGENT = "apply-vectorization/arm-intrinsics-db-refresh"


class RefreshError(RuntimeError):
    """Raised when the refresh process cannot complete safely."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Refresh the curated Arm intrinsics knowledge base from official sources."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_DB_DIR,
        help="Destination directory for schema.json, index.json and snapshot files.",
    )
    parser.add_argument(
        "--retrieved-at",
        default=dt.date.today().isoformat(),
        help="Date string written into source.retrieved_at.",
    )
    return parser.parse_args()


def normalize_whitespace(text: str) -> str:
    """Collapse repeated whitespace for reliable text matching."""

    return " ".join(text.split())


def fetch_html(url: str) -> str:
    """Fetch one official HTML document."""

    try:
        response = requests.get(
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:  # pragma: no cover - network dependent
        raise RefreshError("network_error", f"failed to fetch {url}: {exc}") from exc
    if response.status_code != 200:
        raise RefreshError(
            "network_error",
            f"failed to fetch {url}: unexpected HTTP status {response.status_code}",
        )
    return response.text


def fetch_optional_official_text(url: str) -> str:
    """Best-effort fetch for official pages that often block scripts."""

    try:
        html = fetch_html(url)
    except RefreshError:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return normalize_whitespace(soup.get_text("\n"))


def build_sve_mapping(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the SVE instruction-to-intrinsic family mapping table."""

    heading = None
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = normalize_whitespace(tag.get_text(" ", strip=True)).rstrip("^").strip()
        if text == "Mapping of SVE instructions to intrinsics":
            heading = tag
            break
    if heading is None:
        raise RefreshError(
            "structure_error",
            "ACLE page no longer exposes the 'Mapping of SVE instructions to intrinsics' heading",
        )

    table = heading.find_next("table")
    if table is None:
        raise RefreshError(
            "structure_error",
            "ACLE page no longer exposes the SVE instruction mapping table after its heading",
        )

    mapping: dict[str, str] = {}
    rows = table.find_all("tr")
    if len(rows) < 2:
        raise RefreshError("parse_error", "SVE instruction mapping table is unexpectedly empty")
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        instruction = normalize_whitespace(cells[0].get_text(" ", strip=True))
        intrinsic = normalize_whitespace(cells[1].get_text(" ", strip=True))
        if instruction:
            mapping[instruction] = intrinsic
    if not mapping:
        raise RefreshError("parse_error", "failed to parse any SVE instruction mapping rows")
    return mapping


def extract_headings(soup: BeautifulSoup) -> set[str]:
    """Collect normalized heading texts from an HTML page."""

    headings: set[str] = set()
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        headings.add(normalize_whitespace(tag.get_text(" ", strip=True)).rstrip("^").strip())
    return headings


def source(title: str, url: str, section: str, retrieved_at: str) -> dict[str, str]:
    """Build a source descriptor."""

    return {
        "title": title,
        "url": url,
        "section": section,
        "retrieved_at": retrieved_at,
    }


def make_record(
    *,
    record_id: str,
    kind: str,
    isa: str,
    group_path: list[str],
    display_name: str,
    intrinsic_names: list[str],
    instruction_names: list[str],
    prototype: str,
    header: str,
    feature_macros: list[str],
    required_function_attributes: list[str],
    operand_constraints: list[str],
    immediate_constraints: list[str],
    vectorization_role: str,
    tail_policy: str,
    usage_template: str,
    correctness_rules: list[str],
    anti_patterns: list[str],
    related_items: list[str],
    source_info: dict[str, str],
    validation: list[dict[str, str]],
) -> dict[str, Any]:
    """Build one output record plus its internal validation metadata."""

    return {
        "id": record_id,
        "kind": kind,
        "isa": isa,
        "group_path": group_path,
        "display_name": display_name,
        "intrinsic_names": intrinsic_names,
        "instruction_names": instruction_names,
        "prototype": prototype,
        "header": header,
        "feature_macros": feature_macros,
        "required_function_attributes": required_function_attributes,
        "operand_constraints": operand_constraints,
        "immediate_constraints": immediate_constraints,
        "vectorization_role": vectorization_role,
        "tail_policy": tail_policy,
        "usage_template": usage_template,
        "correctness_rules": correctness_rules,
        "anti_patterns": anti_patterns,
        "related_items": related_items,
        "source": source_info,
        "_validation": validation,
    }


def build_sme_inline_asm_instruction_records(retrieved_at: str) -> list[dict[str, Any]]:
    """Return curated SME ZA inline-asm instruction records."""

    common_rules = [
        "Use inside an SME streaming function; prefer __arm_streaming on both declaration and definition.",
        "Standalone generated code must not rely on __arm_new(\"za\") unless the target link environment has proven SME ABI support routines.",
        "Compile to assembly and scan for smstart za, zero {za}, fmopa, st1w and smstop za; scan linked output for unresolved __arm_tpidr2_* or __arm_za_disable symbols.",
    ]
    common_anti_patterns = [
        "Adding weak __arm_tpidr2_* or __arm_za_disable stubs to hide missing SME ABI runtime support.",
        "Mixing the inline-asm ZA ownership model with __arm_new(\"za\") in an unverified standalone benchmark.",
    ]

    def ddi_source(section: str, *, url: str = DDI0602_SME_INSTRUCTIONS_URL) -> dict[str, str]:
        return source(
            "Arm A-profile A64 Instruction Set Architecture / DDI0602",
            url,
            section,
            retrieved_at,
        )

    return [
        make_record(
            record_id="instruction.sme.inline_asm.smstart_za",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "state"],
            display_name="SMSTART ZA",
            intrinsic_names=[],
            instruction_names=["SMSTART"],
            prototype="smstart za",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Enables access to ZA storage for the current streaming kernel scope."],
            immediate_constraints=[],
            vectorization_role="sme-inline-state",
            tail_policy="Not applicable; pair the ZA enable range with a matching smstop za.",
            usage_template='''__asm__ volatile(
    "smstart za\\n"
    :
    :
    : "memory", "za"
);''',
            correctness_rules=[
                *common_rules,
                "Place smstart za before zero {za}, fmopa, mova or ZA tile store instructions.",
                "Keep smstart za and smstop za in the same generated kernel unless a verified ABI wrapper owns ZA outside it.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Starting ZA access and returning before smstop za on any generated control-flow path.",
            ],
            related_items=[
                "instruction.sme.inline_asm.smstop_za",
                "instruction.sme.inline_asm.zero_za",
                "attribute.sme.__arm_streaming",
            ],
            source_info=ddi_source(
                "SMSTART ZA; DDI0602 reference-only when developer.arm.com blocks scripted access; Arm SME introduction blog cross-checks SMSTART/SMSTOP semantics."
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "SMSTART"},
                {"type": "optional_text_contains", "source": "sme_intro_blog", "value": "SMSTART"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.smstop_za",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "state"],
            display_name="SMSTOP ZA",
            intrinsic_names=[],
            instruction_names=["SMSTOP"],
            prototype="smstop za",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Disables access to ZA storage after the inline-asm tile write-back is complete."],
            immediate_constraints=[],
            vectorization_role="sme-inline-state",
            tail_policy="Not applicable; execute after all ZA tile stores for the current kernel.",
            usage_template='''__asm__ volatile(
    "smstop za\\n"
    :
    :
    : "memory", "za"
);''',
            correctness_rules=[
                *common_rules,
                "Emit smstop za after the final ZA store and before returning from a standalone inline-asm ZA kernel.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Calling C helpers that may clobber streaming state between smstart za and smstop za.",
            ],
            related_items=[
                "instruction.sme.inline_asm.smstart_za",
                "instruction.sme.inline_asm.st1w_za0h_s32",
            ],
            source_info=ddi_source(
                "SMSTOP ZA; DDI0602 reference-only when developer.arm.com blocks scripted access; Arm SME introduction blog cross-checks SMSTART/SMSTOP semantics."
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "SMSTOP"},
                {"type": "optional_text_contains", "source": "sme_intro_blog", "value": "SMSTOP ZA"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.zero_za",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "za-init"],
            display_name="ZERO {ZA}",
            intrinsic_names=[],
            instruction_names=["ZERO"],
            prototype="zero {za}",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Zeros the whole ZA array before a fresh outer-product accumulation."],
            immediate_constraints=[],
            vectorization_role="sme-inline-za-init",
            tail_policy="Not applicable; execute once per fresh output tile before the K loop.",
            usage_template='''__asm__ volatile(
    "zero {za}\\n"
    :
    :
    : "memory", "za"
);''',
            correctness_rules=[
                *common_rules,
                "Run zero {za} after smstart za and before the first fmopa for each fresh output tile.",
                "Do not zero ZA when the kernel is meant to continue caller-owned accumulation.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Zeroing ZA inside the K loop and erasing previous outer-product accumulation.",
            ],
            related_items=[
                "instruction.sme.inline_asm.smstart_za",
                "instruction.sme.inline_asm.fmopa_f32",
                "intrinsic.sme.svzero_za",
            ],
            source_info=ddi_source(
                "ZERO {ZA}; DDI0602 reference-only when developer.arm.com blocks scripted access; Arm SME Instructions blog describes ZERO {ZA} as the whole-ZA alias."
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "ZERO"},
                {"type": "optional_text_contains", "source": "sme_instructions_blog", "value": "ZERO {ZA}"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.fmopa_f32",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "outer-product"],
            display_name="FMOPA ZA.S",
            intrinsic_names=[],
            instruction_names=["FMOPA"],
            prototype="fmopa <ZAda>.S, <Pn>/M, <Pm>/M, <Zn>.S, <Zm>.S",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=[
                "ZAda is the destination FP32 ZA tile accumulator.",
                "Pn and Pm independently predicate the row and column source vectors.",
                "Zn.S and Zm.S hold the FP32 source vectors for one K/outer-product step.",
            ],
            immediate_constraints=["The tile selector in za0.s / za1.s must be architectural and assembler-legal."],
            vectorization_role="sme-inline-outer-product",
            tail_policy="Express M/N tails through Pn/Pm predicates; express K tail by guarding the source loads before fmopa.",
            usage_template='''__asm__ volatile(
    "whilelo p0.s, xzr, %x[row_count]\\n"
    "whilelo p1.s, xzr, %x[col_count]\\n"
    "ld1w { z0.s }, p0/z, [%[a_work]]\\n"
    "ld1w { z1.s }, p1/z, [%[b_ptr]]\\n"
    "fmopa za0.s, p0/m, p1/m, z0.s, z1.s\\n"
    :
    : [a_work] "r"(a_work),
      [b_ptr] "r"(b_ptr),
      [row_count] "r"(row_count),
      [col_count] "r"(col_count)
    : "memory", "p0", "p1", "z0", "z1", "za"
);''',
            correctness_rules=[
                *common_rules,
                "Only use for GEMM, rank-k or explicit outer-product semantics with a documented ZA tile-to-C mapping.",
                "The row predicate must describe active C rows and the column predicate active C columns for the current tile.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Using fmopa for plain elementwise FMA or GEMV without a two-dimensional tile accumulation.",
            ],
            related_items=[
                "instruction.sme.inline_asm.ld1w_f32",
                "instruction.sme.inline_asm.whilelo_b32",
                "instruction.sme.inline_asm.st1w_za0h_s32",
                "intrinsic.sme.svmopa_za32_s8_m",
            ],
            source_info=ddi_source(
                "FMOPA (non-widening), FP32 form; DDI0602 reference-only when developer.arm.com blocks scripted access; Arm SME Instructions blog gives FMOPA <ZAda>.S, <Pn>/M, <Pm>/M, <Zn>.S, <Zm>.S."
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "FMOPA"},
                {
                    "type": "optional_text_contains",
                    "source": "sme_instructions_blog",
                    "value": "FMOPA <ZAda>.S, <Pn>/M, <Pm>/M, <Zn>.S, <Zm>.S",
                },
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.st1w_za0h_s32",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "za-store"],
            display_name="ST1W ZA0H.S slice",
            intrinsic_names=[],
            instruction_names=["ST1W"],
            prototype="st1w { za0h.s[<Wv>, #<imm>] }, <Pg>, [<Xn|SP>]",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=[
                "Pg predicates the active columns written from one ZA horizontal slice.",
                "Wv selects the runtime tile row slice; the immediate offset must be assembler-legal.",
                "Xn points at the destination C row or tile-slice base.",
            ],
            immediate_constraints=["Use #0 for the canonical template unless a verified multi-slice layout requires another immediate."],
            vectorization_role="sme-inline-za-store",
            tail_policy="Use Pg for column tails; loop over Wv-selected rows for row tails.",
            usage_template='''__asm__ volatile(
    "whilelo p1.s, xzr, %x[col_count]\\n"
    "mov w12, %w[tile_row]\\n"
    "st1w {za0h.s[w12, 0]}, p1, [%[c_row]]\\n"
    :
    : [col_count] "r"(col_count),
      [tile_row] "r"(tile_row),
      [c_row] "r"(c_row)
    : "memory", "p1", "w12", "za"
);''',
            correctness_rules=[
                *common_rules,
                "For beta == 0 paths, st1w may directly write the ZA slice to C after fmopa accumulation.",
                "Do not store inactive lanes; build Pg from the same active column count used by the tile.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Using an unpredicated C store for a partial N tile.",
            ],
            related_items=[
                "instruction.sme.inline_asm.mova_za0h_to_z",
                "instruction.sme.inline_asm.smstop_za",
            ],
            source_info=ddi_source(
                "ST1W (ZA tile slice store); DDI0602 reference-only when developer.arm.com blocks scripted access; Arm ACLE exposes SME ST1W instruction intrinsics."
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "ST1W"},
                {"type": "optional_text_contains", "source": "sme_instructions_blog", "value": "store"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.mova_za0h_to_z",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "za-read"],
            display_name="MOVA ZA0H.S slice to Z",
            intrinsic_names=[],
            instruction_names=["MOVA"],
            prototype="mova <Zd>.S, <Pg>/M, ZA0H.S[<Wv>, #<imm>]",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=[
                "Zd receives one predicated ZA horizontal slice.",
                "Pg must match the active columns in the destination C row.",
                "Wv and #imm select the same tile row slice used by the store-back path.",
            ],
            immediate_constraints=["Use #0 in the canonical single-slice template."],
            vectorization_role="sme-inline-za-read",
            tail_policy="Use the same Pg as the beta-path load and final vector store.",
            usage_template='''__asm__ volatile(
    "whilelo p1.s, xzr, %x[col_count]\\n"
    "mov w12, %w[tile_row]\\n"
    "mova z2.s, p1/m, za0h.s[w12, 0]\\n"
    :
    : [col_count] "r"(col_count),
      [tile_row] "r"(tile_row)
    : "memory", "p1", "z2", "w12", "za"
);''',
            correctness_rules=[
                *common_rules,
                "Use MOVA when beta != 0 requires combining the ZA result with the old C value in a vector register.",
                "The compiler may print the MOVA alias as mov in the emitted assembly; scan for a ZA slice operand as well as the mnemonic.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Reading a different ZA slice than the one corresponding to the C row being updated.",
            ],
            related_items=[
                "instruction.sme.inline_asm.fmla_f32_predicated",
                "instruction.sme.inline_asm.st1w_za0h_s32",
            ],
            source_info=ddi_source(
                "MOVA (ZA tile slice move); DDI0602 reference-only when developer.arm.com blocks scripted access; Arm SME Instructions blog describes vector-to/from-tile slice moves."
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "MOVA"},
                {"type": "optional_text_contains", "source": "sme_instructions_blog", "value": "MOVA"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.whilelo_b32",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "predicate"],
            display_name="WHILELO Pd.S",
            intrinsic_names=[],
            instruction_names=["WHILELO"],
            prototype="whilelo <Pd>.S, <Xn|SP>, <Xm>",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME", "__ARM_FEATURE_SVE"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Builds a predicate for active 32-bit elements from start < end."],
            immediate_constraints=[],
            vectorization_role="sme-inline-predicate",
            tail_policy="Use for M/N partial tile predicates and for vector-length agnostic tails.",
            usage_template='''__asm__ volatile(
    "whilelo p0.s, xzr, %x[row_count]\\n"
    "whilelo p1.s, xzr, %x[col_count]\\n"
    :
    : [row_count] "r"(row_count),
      [col_count] "r"(col_count)
    : "p0", "p1"
);''',
            correctness_rules=[
                *common_rules,
                "Use row and column predicates consistently across ld1w, fmopa, mova and st1w for a tile.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Using ptrue for a partial edge tile that requires masking.",
            ],
            related_items=[
                "instruction.sme.inline_asm.ptrue_b32",
                "instruction.sme.inline_asm.fmopa_f32",
            ],
            source_info=ddi_source(
                "WHILELO (SVE predicate generation usable in Streaming SVE mode); DDI0602 reference-only when developer.arm.com blocks scripted access.",
                url=DDI0602_SVE_INSTRUCTIONS_URL,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "WHILELO"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.ptrue_b32",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "predicate"],
            display_name="PTRUE Pd.S",
            intrinsic_names=[],
            instruction_names=["PTRUE"],
            prototype="ptrue <Pd>.S",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME", "__ARM_FEATURE_SVE"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Creates an all-active predicate for the current streaming vector length and element width."],
            immediate_constraints=[],
            vectorization_role="sme-inline-predicate",
            tail_policy="Use only for full tiles or known-full dimensions; use whilelo for edges.",
            usage_template='''__asm__ volatile(
    "ptrue p1.s\\n"
    :
    :
    : "p1"
);''',
            correctness_rules=[
                *common_rules,
                "Use ptrue only when the active dimension is exactly a full streaming vector, or when over-store/over-read cannot occur.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Using ptrue on the final partial C tile.",
            ],
            related_items=[
                "instruction.sme.inline_asm.whilelo_b32",
                "instruction.sme.inline_asm.ld1w_f32",
            ],
            source_info=ddi_source(
                "PTRUE (SVE predicate generation usable in Streaming SVE mode); DDI0602 reference-only when developer.arm.com blocks scripted access.",
                url=DDI0602_SVE_INSTRUCTIONS_URL,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "PTRUE"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.ld1w_f32",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "load"],
            display_name="LD1W Z.S predicated",
            intrinsic_names=[],
            instruction_names=["LD1W"],
            prototype="ld1w { <Zt>.S }, <Pg>/Z, [<Xn|SP>]",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME", "__ARM_FEATURE_SVE"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Pg/Z controls active 32-bit lanes and zeroes inactive lanes."],
            immediate_constraints=[],
            vectorization_role="sme-inline-load",
            tail_policy="Inactive lanes are zeroed; keep Pg aligned with the corresponding fmopa predicate.",
            usage_template='''__asm__ volatile(
    "ld1w { z0.s }, p0/z, [%[a_work]]\\n"
    "ld1w { z1.s }, p1/z, [%[b_ptr]]\\n"
    :
    : [a_work] "r"(a_work),
      [b_ptr] "r"(b_ptr)
    : "memory", "z0", "z1"
);''',
            correctness_rules=[
                *common_rules,
                "Load contiguous temporary A and B vectors; make non-unit-stride A columns contiguous before entering the inline asm K loop.",
                "Use the same p0/p1 predicates that drive fmopa row and column participation.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Using ld1w with a stride that does not match the packed or contiguous source vector.",
            ],
            related_items=[
                "instruction.sme.inline_asm.fmopa_f32",
                "instruction.sme.inline_asm.whilelo_b32",
            ],
            source_info=ddi_source(
                "LD1W (SVE predicated load usable in Streaming SVE mode); DDI0602 reference-only when developer.arm.com blocks scripted access.",
                url=DDI0602_SVE_INSTRUCTIONS_URL,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "LD1W"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.fadd_f32_predicated",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "beta-path"],
            display_name="FADD Z.S predicated",
            intrinsic_names=[],
            instruction_names=["FADD"],
            prototype="fadd <Zdn>.S, <Pg>/M, <Zdn>.S, <Zm>.S",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME", "__ARM_FEATURE_SVE"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Use Pg/M so inactive lanes preserve the destination vector."],
            immediate_constraints=[],
            vectorization_role="sme-inline-beta",
            tail_policy="Pg must match active columns in the C row being updated.",
            usage_template='''__asm__ volatile(
    "fadd z2.s, p1/m, z2.s, z3.s\\n"
    :
    :
    : "z2", "z3", "p1"
);''',
            correctness_rules=[
                *common_rules,
                "Use for beta == 1 or explicit C += ZA-row style paths after MOVA reads a ZA slice.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Using fadd when the BLAS beta coefficient is neither 0 nor 1.",
            ],
            related_items=[
                "instruction.sme.inline_asm.mova_za0h_to_z",
                "instruction.sme.inline_asm.fmla_f32_predicated",
            ],
            source_info=ddi_source(
                "FADD (SVE predicated vector form usable in Streaming SVE mode); DDI0602 reference-only when developer.arm.com blocks scripted access.",
                url=DDI0602_SVE_INSTRUCTIONS_URL,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "FADD"},
            ],
        ),
        make_record(
            record_id="instruction.sme.inline_asm.fmla_f32_predicated",
            kind="instruction",
            isa="sme",
            group_path=["sme", "inline-asm", "beta-path"],
            display_name="FMLA Z.S predicated",
            intrinsic_names=[],
            instruction_names=["FMLA"],
            prototype="fmla <Zda>.S, <Pg>/M, <Zn>.S, <Zm>.S",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME", "__ARM_FEATURE_SVE"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Use Pg/M to update only active C columns after loading old C and reading ZA."],
            immediate_constraints=[],
            vectorization_role="sme-inline-beta",
            tail_policy="Pg must match active columns in the C row being updated.",
            usage_template='''__asm__ volatile(
    "mova z2.s, p1/m, za0h.s[w12, 0]\\n"
    "ld1w { z3.s }, p1/z, [%[c_row]]\\n"
    "dup z4.s, %w[beta_bits]\\n"
    "fmla z2.s, p1/m, z3.s, z4.s\\n"
    "st1w { z2.s }, p1, [%[c_row]]\\n"
    :
    : [c_row] "r"(c_row),
      [beta_bits] "r"(beta_bits)
    : "memory", "z2", "z3", "z4", "p1", "za"
);''',
            correctness_rules=[
                *common_rules,
                "Use for beta != 0 paths as C = ZA + beta * C after MOVA reads the ZA slice.",
                "Preserve beta as an FP32 bit pattern when duplicating it through an integer inline-asm operand.",
            ],
            anti_patterns=[
                *common_anti_patterns,
                "Replacing the direct st1w beta == 0 path with an unnecessary old-C load.",
            ],
            related_items=[
                "instruction.sme.inline_asm.mova_za0h_to_z",
                "instruction.sme.inline_asm.st1w_za0h_s32",
            ],
            source_info=ddi_source(
                "FMLA (SVE predicated vector form usable in Streaming SVE mode); DDI0602 reference-only when developer.arm.com blocks scripted access.",
                url=DDI0602_SVE_INSTRUCTIONS_URL,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "FMLA"},
            ],
        ),
    ]


def build_curated_records(retrieved_at: str) -> list[dict[str, Any]]:
    """Return the curated, vectorization-focused record list."""

    records = [
        make_record(
            record_id="intrinsic.neon.vld1q_f32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "load-store", "contiguous-load", "ld1"],
            display_name="vld1q_f32",
            intrinsic_names=["vld1q_f32"],
            instruction_names=["LD1"],
            prototype="float32x4_t vld1q_f32(float32_t const *ptr)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["ptr must point to at least 4 contiguous float32 elements"],
            immediate_constraints=[],
            vectorization_role="load",
            tail_policy="Pair with an explicit scalar epilogue when the trip count is not a multiple of 4 lanes.",
            usage_template="float32x4_t va = vld1q_f32(a + i);",
            correctness_rules=[
                "Use only for fixed-width 128-bit NEON loops with unit-stride access.",
                "Advance the loop by 4 float32 lanes per iteration.",
            ],
            anti_patterns=[
                "Using vld1q_f32 inside a loop that still increments by 1 element.",
                "Reading from indirect or gather-style addresses.",
            ],
            related_items=[
                "intrinsic.neon.vst1q_f32",
                "intrinsic.neon.vaddq_f32",
                "rule.neon.explicit-epilogue",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Vector arithmetic / load-store intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vld1q_f32"}],
        ),
        make_record(
            record_id="intrinsic.neon.vst1q_f32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "load-store", "contiguous-store", "st1"],
            display_name="vst1q_f32",
            intrinsic_names=["vst1q_f32"],
            instruction_names=["ST1"],
            prototype="void vst1q_f32(float32_t *ptr, float32x4_t val)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["ptr must point to at least 4 writable float32 elements"],
            immediate_constraints=[],
            vectorization_role="store",
            tail_policy="Write back the vector body first, then finish the remainder with scalar stores.",
            usage_template="vst1q_f32(out + i, vc);",
            correctness_rules=[
                "Store the same lane width that the NEON main loop computes.",
                "Keep the store address unit-stride and alias-safe.",
            ],
            anti_patterns=[
                "Using vst1q_f32 when the destination overlaps with a later scalar tail in an unsafe way.",
                "Combining NEON stores with SVE or SME stores in the same kernel body.",
            ],
            related_items=[
                "intrinsic.neon.vld1q_f32",
                "intrinsic.neon.vaddq_f32",
                "rule.cross-isa.no-mixed-templates",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Vector arithmetic / load-store intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vst1q_f32"}],
        ),
        make_record(
            record_id="intrinsic.neon.vdupq_n_f32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "broadcast", "dup"],
            display_name="vdupq_n_f32",
            intrinsic_names=["vdupq_n_f32"],
            instruction_names=["DUP"],
            prototype="float32x4_t vdupq_n_f32(float32_t value)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["Broadcast one scalar float32 value into all 4 lanes"],
            immediate_constraints=[],
            vectorization_role="broadcast",
            tail_policy="No dedicated tail handling; use the same scalar value in both vector and scalar paths.",
            usage_template="float32x4_t alpha_vec = vdupq_n_f32(alpha);",
            correctness_rules=[
                "Use when one scalar is intentionally replicated across all NEON lanes.",
                "Keep the scalar source outside the main loop when possible.",
            ],
            anti_patterns=[
                "Rebuilding the same broadcast value on every iteration without need.",
                "Using NEON broadcast in an SVE length-agnostic loop.",
            ],
            related_items=[
                "intrinsic.neon.vfmaq_f32",
                "intrinsic.sve.svdup_n_f32",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Move / duplicate intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vdupq_n_f32"}],
        ),
        make_record(
            record_id="intrinsic.neon.vaddq_f32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "arithmetic", "add"],
            display_name="vaddq_f32",
            intrinsic_names=["vaddq_f32"],
            instruction_names=["FADD", "ADD"],
            prototype="float32x4_t vaddq_f32(float32x4_t a, float32x4_t b)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["a and b must both be float32x4_t vectors"],
            immediate_constraints=[],
            vectorization_role="add",
            tail_policy="Preserve scalar add semantics in the epilogue.",
            usage_template="float32x4_t vc = vaddq_f32(va, vb);",
            correctness_rules=[
                "Use the same element type for both operands and the destination vector.",
                "Keep the scalar fallback equivalent to the vector body.",
            ],
            anti_patterns=[
                "Mixing integer and floating-point NEON types in the same add without an explicit conversion.",
                "Using vaddq_f32 when the loop really needs a fused multiply-add pattern.",
            ],
            related_items=[
                "intrinsic.neon.vld1q_f32",
                "intrinsic.neon.vst1q_f32",
                "intrinsic.sve.svadd_f32_x",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Vector arithmetic / Addition",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vaddq_f32"}],
        ),
        make_record(
            record_id="intrinsic.neon.vfmaq_f32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "arithmetic", "fma"],
            display_name="vfmaq_f32",
            intrinsic_names=["vfmaq_f32"],
            instruction_names=["FMLA"],
            prototype="float32x4_t vfmaq_f32(float32x4_t acc, float32x4_t lhs, float32x4_t rhs)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["acc, lhs and rhs must all be float32x4_t vectors"],
            immediate_constraints=[],
            vectorization_role="fma",
            tail_policy="Use the same multiply-add formula in the scalar cleanup path.",
            usage_template="vacc = vfmaq_f32(vacc, vlhs, vrhs);",
            correctness_rules=[
                "Choose vfmaq_f32 only when the scalar algorithm already performs an accumulate += lhs * rhs.",
                "Keep the accumulator lifetime explicit; do not overwrite unrelated vectors.",
            ],
            anti_patterns=[
                "Replacing a plain add with vfmaq_f32 without a multiply operand.",
                "Using vfmaq_f32 in a loop with non-contiguous rhs loads.",
            ],
            related_items=[
                "intrinsic.neon.vdupq_n_f32",
                "intrinsic.sve.svmla_f32_x",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Vector arithmetic / Fused multiply-add",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vfmaq_f32"}],
        ),
        make_record(
            record_id="intrinsic.neon.vaddvq_f32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "reduction", "horizontal-add"],
            display_name="vaddvq_f32",
            intrinsic_names=["vaddvq_f32"],
            instruction_names=["FADDP"],
            prototype="float32_t vaddvq_f32(float32x4_t a)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["a must contain the accumulated float32 lanes to reduce"],
            immediate_constraints=[],
            vectorization_role="horizontal-reduction",
            tail_policy="Run scalar cleanup before or after the horizontal add, then combine with the reduced vector accumulator.",
            usage_template="float acc = vaddvq_f32(vacc);",
            correctness_rules=[
                "Use only for final horizontal reduction of a vector accumulator.",
                "Document that float32 reduction changes the scalar addition order and is not bit-exact.",
            ],
            anti_patterns=[
                "Using vaddvq_f32 for prefix-sum or per-element scan output.",
                "Hiding a strict floating-point ordering requirement behind a vector reduction.",
            ],
            related_items=[
                "intrinsic.neon.vaddq_f32",
                "intrinsic.neon.vfmaq_f32",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Vector arithmetic / Across vector arithmetic",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vaddvq_f32"}],
        ),
        make_record(
            record_id="intrinsic.neon.vaddvq_s32",
            kind="intrinsic",
            isa="neon",
            group_path=["neon", "reduction", "horizontal-add"],
            display_name="vaddvq_s32",
            intrinsic_names=["vaddvq_s32"],
            instruction_names=["ADDV"],
            prototype="int32_t vaddvq_s32(int32x4_t a)",
            header="<arm_neon.h>",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["a must contain the accumulated int32 lanes to reduce"],
            immediate_constraints=[],
            vectorization_role="horizontal-reduction",
            tail_policy="Run scalar cleanup for any remaining elements and add it to the reduced vector accumulator.",
            usage_template="int32_t acc = vaddvq_s32(vacc);",
            correctness_rules=[
                "Use only when int32 overflow behavior is either irrelevant or explicitly acceptable.",
                "Keep prefix-scan style outputs on the reject path.",
            ],
            anti_patterns=[
                "Using vaddvq_s32 when the original scalar contract depends on trapping or undefined overflow details.",
                "Reducing a vector accumulator while also writing per-iteration accumulator state.",
            ],
            related_items=[
                "intrinsic.neon.vaddq_f32",
                "rule.neon.explicit-epilogue",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Vector arithmetic / Across vector arithmetic",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "vaddvq_s32"}],
        ),
        make_record(
            record_id="intrinsic.sve.svcntw",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "predicate-and-vl", "vector-length"],
            display_name="svcntw",
            intrinsic_names=["svcntw"],
            instruction_names=["CNTW"],
            prototype="uint64_t svcntw(void)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["Returns the current vector length measured in 32-bit elements"],
            immediate_constraints=[],
            vectorization_role="vector-length",
            tail_policy="Drive the loop step with svcntw() instead of a hard-coded lane count.",
            usage_template="const int vl = (int)svcntw();",
            correctness_rules=[
                "Use svcntw() or the matching svcnt*() form to keep the loop vector-length agnostic.",
                "Do not assume the returned value is constant across all machines.",
            ],
            anti_patterns=[
                "Hard-coding i += 4 in an SVE loop.",
                "Casting away the scalable-width intent by treating svcntw() like a compile-time constant.",
            ],
            related_items=[
                "intrinsic.sve.svwhilelt_b32",
                "rule.sve.length-agnostic",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / List of SVE intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svcntw"},
                {"type": "mapping_equals", "instruction": "CNTW", "expected_intrinsic": "svcntw"},
            ],
        ),
        make_record(
            record_id="intrinsic.sve.svwhilelt_b32",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "predicate-and-vl", "whilelt"],
            display_name="svwhilelt_b32",
            intrinsic_names=["svwhilelt_b32"],
            instruction_names=["WHILELT"],
            prototype="svbool_t svwhilelt_b32(uint64_t op1, uint64_t op2)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["op1 is the current scalar index and op2 is the loop upper bound"],
            immediate_constraints=[],
            vectorization_role="predicate",
            tail_policy="The predicate masks both the full vectors and the final partial iteration.",
            usage_template="svbool_t pg = svwhilelt_b32((uint64_t)i, (uint64_t)n);",
            correctness_rules=[
                "Build a fresh predicate from the current scalar index and loop bound each iteration.",
                "Use the same predicate for both loads, arithmetic and stores that belong to that step.",
            ],
            anti_patterns=[
                "Generating pg once outside the loop and reusing it for all iterations.",
                "Pairing svwhilelt_b32 with a loop that still advances by a fixed NEON lane count.",
            ],
            related_items=[
                "intrinsic.sve.svcntw",
                "intrinsic.sve.svld1_f32",
                "intrinsic.sve.svst1_f32",
                "rule.sve.predicated-load-store",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svwhilelt"},
                {"type": "mapping_equals", "instruction": "WHILELT", "expected_intrinsic": "svwhilelt"},
            ],
        ),
        make_record(
            record_id="intrinsic.sve.svld1_f32",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "load-store", "predicated-load", "ld1"],
            display_name="svld1_f32",
            intrinsic_names=["svld1_f32"],
            instruction_names=["LD1", "LD1W"],
            prototype="svfloat32_t svld1_f32(svbool_t pg, float32_t const *base)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["pg must come from a matching predicate such as svwhilelt_b32"],
            immediate_constraints=[],
            vectorization_role="load",
            tail_policy="The predicate naturally suppresses inactive tail lanes; do not add a separate scalar tail for the same iteration.",
            usage_template="svfloat32_t va = svld1_f32(pg, a + i);",
            correctness_rules=[
                "Pass a valid svbool_t predicate as the first argument.",
                "Use unit-stride contiguous access unless the algorithm explicitly supports another addressing mode.",
            ],
            anti_patterns=[
                "Calling svld1_f32 without a predicate value.",
                "Using svld1_f32 together with a fixed i += 4 increment.",
            ],
            related_items=[
                "intrinsic.sve.svwhilelt_b32",
                "intrinsic.sve.svst1_f32",
                "rule.sve.predicated-load-store",
                "rule.sve.header-required",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svld1"},
                {
                    "type": "mapping_equals",
                    "instruction": "LD1W (scalar plus scalar)",
                    "expected_intrinsic": "svld1 , svld1uw",
                },
            ],
        ),
        make_record(
            record_id="intrinsic.sve.svst1_f32",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "load-store", "predicated-store", "st1"],
            display_name="svst1_f32",
            intrinsic_names=["svst1_f32"],
            instruction_names=["ST1", "ST1W"],
            prototype="void svst1_f32(svbool_t pg, float32_t *base, svfloat32_t data)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["Use the same predicate and vector shape as the corresponding SVE load and compute step"],
            immediate_constraints=[],
            vectorization_role="store",
            tail_policy="Predication handles the partial last step; keep the scalar fallback only for non-SVE paths.",
            usage_template="svst1_f32(pg, out + i, vc);",
            correctness_rules=[
                "Keep the store predicate aligned with the compute predicate.",
                "Write back only to contiguous, alias-safe output memory.",
            ],
            anti_patterns=[
                "Using an unpredicated store in a scalable-width loop.",
                "Mixing svst1_f32 with NEON stores in the same loop body.",
            ],
            related_items=[
                "intrinsic.sve.svld1_f32",
                "intrinsic.sve.svwhilelt_b32",
                "rule.cross-isa.no-mixed-templates",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svst1"},
                {
                    "type": "mapping_equals",
                    "instruction": "ST1W (scalar plus scalar)",
                    "expected_intrinsic": "svst1 , svst1w",
                },
            ],
        ),
        make_record(
            record_id="intrinsic.sve.svdup_n_f32",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "broadcast", "dup"],
            display_name="svdup_n_f32",
            intrinsic_names=["svdup_n_f32"],
            instruction_names=["DUP"],
            prototype="svfloat32_t svdup_n_f32(float32_t value)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["Broadcast one scalar float32 value across the active SVE vector lanes"],
            immediate_constraints=[],
            vectorization_role="broadcast",
            tail_policy="No direct tail logic; the surrounding predicate determines which lanes participate.",
            usage_template="svfloat32_t vlhs = svdup_n_f32(lhs[row * k + depth]);",
            correctness_rules=[
                "Use the matching scalar type for the target vector element width.",
                "Prefer one broadcast per scalar value rather than rebuilding identical vectors repeatedly.",
            ],
            anti_patterns=[
                "Treating svdup_n_f32 like a fixed 4-lane NEON duplicate.",
                "Using svdup_n_f32 while the loop step is still hard-coded.",
            ],
            related_items=[
                "intrinsic.neon.vdupq_n_f32",
                "intrinsic.sve.svmla_f32_x",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / List of SVE intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "svdup"}],
        ),
        make_record(
            record_id="intrinsic.sve.svadd_f32_x",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "arithmetic", "predicated-add"],
            display_name="svadd_f32_x",
            intrinsic_names=["svadd_f32_x"],
            instruction_names=["ADD", "ADD (vectors, predicated)"],
            prototype="svfloat32_t svadd_f32_x(svbool_t pg, svfloat32_t op1, svfloat32_t op2)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["Use a predicate generated for the same loop slice"],
            immediate_constraints=[],
            vectorization_role="add",
            tail_policy="Let the predicate mask the inactive tail lanes instead of branching to a second vector body.",
            usage_template="svfloat32_t vc = svadd_f32_x(pg, va, vb);",
            correctness_rules=[
                "Match the element type of both operands and the destination vector.",
                "Use the same predicate for all operations in a single SVE step.",
            ],
            anti_patterns=[
                "Dropping the predicate argument or reusing a stale predicate.",
                "Switching from svadd_f32_x back to NEON arithmetic mid-loop.",
            ],
            related_items=[
                "intrinsic.neon.vaddq_f32",
                "intrinsic.sve.svwhilelt_b32",
                "intrinsic.sve.svld1_f32",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svadd"},
                {
                    "type": "mapping_equals",
                    "instruction": "ADD (vectors, predicated)",
                    "expected_intrinsic": "svadd",
                },
            ],
        ),
        make_record(
            record_id="intrinsic.sve.svmla_f32_x",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "arithmetic", "predicated-fma"],
            display_name="svmla_f32_x",
            intrinsic_names=["svmla_f32_x"],
            instruction_names=["FMLA", "MLA"],
            prototype=(
                "svfloat32_t svmla_f32_x("
                "svbool_t pg, svfloat32_t acc, svfloat32_t lhs, svfloat32_t rhs)"
            ),
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["acc, lhs and rhs must all use the same scalable float32 element type"],
            immediate_constraints=[],
            vectorization_role="fma",
            tail_policy="Predication masks the inactive lanes; keep the scalar cleanup only for the non-SVE fallback path.",
            usage_template="vacc = svmla_f32_x(pg, vacc, vlhs, vrhs);",
            correctness_rules=[
                "Use svmla_f32_x when the scalar logic is an accumulate += lhs * rhs pattern.",
                "Keep the accumulator vector alive across the depth loop instead of recreating it per scalar op.",
            ],
            anti_patterns=[
                "Using svmla_f32_x for a plain add without multiplication.",
                "Combining svmla_f32_x with fixed-width loop stepping.",
            ],
            related_items=[
                "intrinsic.neon.vfmaq_f32",
                "intrinsic.sve.svdup_n_f32",
                "intrinsic.sve.svwhilelt_b32",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "svmla"}],
        ),
        make_record(
            record_id="intrinsic.sve.svaddv_f32",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "reduction", "horizontal-add"],
            display_name="svaddv_f32",
            intrinsic_names=["svaddv_f32"],
            instruction_names=["FADDV"],
            prototype="float32_t svaddv_f32(svbool_t pg, svfloat32_t op)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["pg selects active lanes in the accumulated vector"],
            immediate_constraints=[],
            vectorization_role="horizontal-reduction",
            tail_policy="Use the loop predicate to keep inactive tail lanes out of the final reduction.",
            usage_template="float acc = svaddv_f32(svptrue_b32(), vacc);",
            correctness_rules=[
                "Use for final reduction of an SVE vector accumulator, not for scan output.",
                "Document that float32 reduction changes the scalar addition order and may not be bit-exact.",
            ],
            anti_patterns=[
                "Calling svaddv_f32 while the scalar contract requires strict left-to-right floating-point order.",
                "Treating svaddv_f32 as a replacement for prefix-sum semantics.",
            ],
            related_items=[
                "intrinsic.sve.svadd_f32_x",
                "intrinsic.sve.svmla_f32_x",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svaddv"},
                {"type": "mapping_equals", "instruction": "FADDV", "expected_intrinsic": "svaddv"},
            ],
        ),
        make_record(
            record_id="intrinsic.sve.svaddv_s32",
            kind="intrinsic",
            isa="sve",
            group_path=["sve", "reduction", "horizontal-add"],
            display_name="svaddv_s32",
            intrinsic_names=["svaddv_s32"],
            instruction_names=["SADDV"],
            prototype="int32_t svaddv_s32(svbool_t pg, svint32_t op)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["pg selects active int32 lanes in the accumulated vector"],
            immediate_constraints=[],
            vectorization_role="horizontal-reduction",
            tail_policy="Predicate the vector loop and reduce only active lanes.",
            usage_template="int32_t acc = svaddv_s32(svptrue_b32(), vacc);",
            correctness_rules=[
                "Use only when int32 overflow behavior is either irrelevant or explicitly acceptable.",
                "Do not use for loops that expose the running accumulator on every iteration.",
            ],
            anti_patterns=[
                "Vectorizing an int32 sum when the original semantics require trapping or checked overflow.",
                "Using svaddv_s32 for histogram, scatter or prefix-scan patterns.",
            ],
            related_items=[
                "intrinsic.sve.svadd_f32_x",
                "intrinsic.sve.svwhilelt_b32",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svaddv"},
                {"type": "mapping_equals", "instruction": "SADDV", "expected_intrinsic": "svaddv"},
            ],
        ),
        make_record(
            record_id="intrinsic.sve2.svdot_s32",
            kind="intrinsic",
            isa="sve2",
            group_path=["sve", "sve2", "dot-product"],
            display_name="svdot_s32",
            intrinsic_names=["svdot_s32"],
            instruction_names=["SDOT", "SDOT (vectors)"],
            prototype="svint32_t svdot_s32(svint32_t acc, svint8_t lhs, svint8_t rhs)",
            header="<arm_sve.h>",
            feature_macros=["__ARM_FEATURE_SVE2"],
            required_function_attributes=[],
            operand_constraints=["acc is widened int32 accumulation, lhs/rhs are int8 vectors"],
            immediate_constraints=[],
            vectorization_role="dot-product",
            tail_policy="Use only in SVE2-aware kernels; partial lanes still rely on the surrounding predicate strategy.",
            usage_template="acc = svdot_s32(acc, lhs_i8, rhs_i8);",
            correctness_rules=[
                "Gate usage on __ARM_FEATURE_SVE2.",
                "Use a widened int32 accumulator that matches the dot-product semantics.",
            ],
            anti_patterns=[
                "Calling svdot_s32 in plain SVE-only code without an SVE2 feature check.",
                "Using float32 operands with svdot_s32.",
            ],
            related_items=[
                "intrinsic.sve.svmla_f32_x",
                "rule.sve.header-required",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svdot"},
                {"type": "mapping_equals", "instruction": "SDOT (vectors)", "expected_intrinsic": "svdot"},
            ],
        ),
        make_record(
            record_id="attribute.sme.__arm_streaming",
            kind="attribute",
            isa="sme",
            group_path=["sme", "attributes", "streaming-mode"],
            display_name='__arm_streaming',
            intrinsic_names=['__arm_streaming'],
            instruction_names=[],
            prototype="void kernel(...) __arm_streaming;",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=[],
            operand_constraints=["Annotates a function that must execute in SME streaming mode"],
            immediate_constraints=[],
            vectorization_role="streaming-attribute",
            tail_policy="Not applicable; this attribute constrains the function context, not the loop epilogue.",
            usage_template="void kernel(...) __arm_streaming;",
            correctness_rules=[
                "Use when the function body contains SME-only intrinsics that require streaming mode.",
                "Do not add __arm_streaming to code that only needs plain NEON or plain SVE semantics.",
            ],
            anti_patterns=[
                "Calling ZA or SME tile intrinsics without a streaming-capable function context.",
                "Treating __arm_streaming as a stylistic annotation instead of an ABI contract.",
            ],
            related_items=[
                "attribute.sme.__arm_streaming_compatible",
                "attribute.sme.__arm_inout_za",
                "rule.sme.streaming-required",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME keyword attributes related to streaming mode",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "__arm_streaming"}],
        ),
        make_record(
            record_id="attribute.sme.__arm_streaming_compatible",
            kind="attribute",
            isa="sme",
            group_path=["sme", "attributes", "streaming-mode"],
            display_name='__arm_streaming_compatible',
            intrinsic_names=['__arm_streaming_compatible'],
            instruction_names=[],
            prototype="void helper(...) __arm_streaming_compatible;",
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=[],
            operand_constraints=["Marks functions that are callable from streaming and non-streaming contexts"],
            immediate_constraints=[],
            vectorization_role="streaming-attribute",
            tail_policy="Not applicable; this attribute constrains call-compatibility, not loop shape.",
            usage_template="void zero_za_wrapper(...) __arm_streaming_compatible;",
            correctness_rules=[
                "Use when the function or intrinsic is specified as compatible with streaming and non-streaming callers.",
                "Prefer the stricter __arm_streaming attribute when the body requires streaming-only instructions.",
            ],
            anti_patterns=[
                "Using __arm_streaming_compatible to hide a missing ZA ownership contract.",
                "Assuming it automatically implies __arm_inout(\"za\") or __arm_out(\"za\").",
            ],
            related_items=[
                "attribute.sme.__arm_streaming",
                "intrinsic.sme.svzero_za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME keyword attributes related to streaming mode",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "__arm_streaming_compatible"}
            ],
        ),
        make_record(
            record_id="attribute.sme.__arm_inout_za",
            kind="attribute",
            isa="sme",
            group_path=["sme", "attributes", "za-ownership"],
            display_name='__arm_inout("za")',
            intrinsic_names=['__arm_inout("za")'],
            instruction_names=[],
            prototype='void kernel(...) __arm_inout("za");',
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Declares that the callee both consumes and preserves ZA state across the call"],
            immediate_constraints=[],
            vectorization_role="za-ownership",
            tail_policy="Not applicable; this is a ZA state contract.",
            usage_template='void kernel(...) __arm_streaming __arm_inout("za");',
            correctness_rules=[
                "Use when the function receives an existing ZA state and updates it in place.",
                "Keep the caller and callee signatures consistent with the same ZA ownership semantics.",
            ],
            anti_patterns=[
                "Adding __arm_inout(\"za\") without any ZA state flowing across the call boundary.",
                "Using ZA intrinsics while omitting all ZA ownership attributes.",
            ],
            related_items=[
                "attribute.sme.__arm_new_za",
                "attribute.sme.__arm_out_za",
                "rule.sme.za-ownership",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME ZA state assertions",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": '__arm_inout("za")'}],
        ),
        make_record(
            record_id="attribute.sme.__arm_new_za",
            kind="attribute",
            isa="sme",
            group_path=["sme", "attributes", "za-ownership"],
            display_name='__arm_new("za")',
            intrinsic_names=['__arm_new("za")'],
            instruction_names=[],
            prototype='void kernel(...) __arm_new("za");',
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Declares that the function creates a fresh ZA state"],
            immediate_constraints=[],
            vectorization_role="za-ownership",
            tail_policy="Not applicable; this is a ZA state lifecycle contract.",
            usage_template='void kernel(...) __arm_streaming __arm_new("za");',
            correctness_rules=[
                "Use when the current function is responsible for creating a new ZA state.",
                "Pair with explicit ZA initialization when the tile state must start from zero.",
            ],
            anti_patterns=[
                "Using __arm_new(\"za\") when the function actually consumes an incoming ZA tile state.",
                "Calling svzero_za() without documenting fresh-ZA semantics.",
            ],
            related_items=[
                "intrinsic.sme.svzero_za",
                "attribute.sme.__arm_out_za",
                "rule.sme.zero-za-fresh-za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME ZA state assertions",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": '__arm_new("za")'}],
        ),
        make_record(
            record_id="attribute.sme.__arm_out_za",
            kind="attribute",
            isa="sme",
            group_path=["sme", "attributes", "za-ownership"],
            display_name='__arm_out("za")',
            intrinsic_names=['__arm_out("za")'],
            instruction_names=[],
            prototype='void kernel(...) __arm_out("za");',
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Declares that the function produces a ZA state for later consumption"],
            immediate_constraints=[],
            vectorization_role="za-ownership",
            tail_policy="Not applicable; this is a call-boundary contract for ZA state.",
            usage_template='void kernel(...) __arm_streaming __arm_out("za");',
            correctness_rules=[
                "Use when the function writes a new ZA result that the caller continues to use.",
                "Document the write-back boundary clearly when ZA leaves the current function.",
            ],
            anti_patterns=[
                "Using __arm_out(\"za\") for a function that never exposes ZA state to its caller.",
                "Combining __arm_out(\"za\") with a scalar-only body.",
            ],
            related_items=[
                "attribute.sme.__arm_inout_za",
                "attribute.sme.__arm_new_za",
                "intrinsic.sme.svzero_za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME ZA state assertions",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": '__arm_out("za")'}],
        ),
        make_record(
            record_id="intrinsic.sme.svzero_za",
            kind="intrinsic",
            isa="sme",
            group_path=["sme", "za", "initialization"],
            display_name="svzero_za()",
            intrinsic_names=["svzero_za"],
            instruction_names=["ZERO"],
            prototype='void svzero_za(void) __arm_streaming_compatible __arm_out("za")',
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=['__arm_streaming_compatible', '__arm_out("za")'],
            operand_constraints=["Resets ZA state to zero before a fresh accumulation sequence"],
            immediate_constraints=[],
            vectorization_role="za-init",
            tail_policy="No direct tail behavior; use before a fresh ZA accumulation path only.",
            usage_template="svzero_za();",
            correctness_rules=[
                "Use only when the function owns a fresh ZA state or explicitly outputs a new ZA result.",
                "Do not call svzero_za() in a path that should preserve incoming ZA accumulation.",
            ],
            anti_patterns=[
                "Calling svzero_za() inside a streaming kernel that should reuse caller-owned ZA state.",
                "Using svzero_za() as a substitute for missing ZA ownership attributes.",
            ],
            related_items=[
                "attribute.sme.__arm_new_za",
                "attribute.sme.__arm_out_za",
                "rule.sme.zero-za-fresh-za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME instruction intrinsics / ZERO",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "svzero_za"}],
        ),
        make_record(
            record_id="intrinsic.sme.svmopa_za32_s8_m",
            kind="intrinsic",
            isa="sme",
            group_path=["sme", "za", "outer-product", "mopa"],
            display_name="svmopa_za32_s8_m",
            intrinsic_names=["svmopa_za32_s8_m"],
            instruction_names=["SMOPA", "FMOPA"],
            prototype=(
                'void svmopa_za32_s8_m(uint64_t tile, svbool_t pn, svbool_t pm, '
                'svint8_t zn, svint8_t zm) __arm_streaming __arm_inout("za")'
            ),
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=['__arm_streaming', '__arm_inout("za")'],
            operand_constraints=[
                "tile must identify a valid ZA tile",
                "pn and pm must describe the active rows and columns for the outer product",
            ],
            immediate_constraints=["tile must be an integer constant in the architectural tile range"],
            vectorization_role="matrix-outer-product",
            tail_policy="Tail handling must be expressed through tile-slice predication, not a scalar remainder loop.",
            usage_template="svmopa_za32_s8_m(tile, pn, pm, lhs_i8, rhs_i8);",
            correctness_rules=[
                "Only enter this path when tile layout, ZA ownership and write-back boundaries are explicit.",
                "Keep the kernel in streaming mode and mark ZA as an inout architectural state.",
            ],
            anti_patterns=[
                "Using svmopa_za32_s8_m in an elementwise add/mul kernel.",
                "Calling an SME ZA intrinsic without a tile ownership story.",
            ],
            related_items=[
                "attribute.sme.__arm_streaming",
                "attribute.sme.__arm_inout_za",
                "intrinsic.sme.svzero_za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME instruction intrinsics / BFMOPA, FMOPA (widening), SMOPA, UMOPA",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svmopa_za32"},
                {
                    "type": "heading_exists",
                    "heading": "BFMOPA, FMOPA (widening), SMOPA, UMOPA",
                },
            ],
        ),
        make_record(
            record_id="intrinsic.sme2.svadd_write_za32_s32_vg1x2",
            kind="intrinsic",
            isa="sme2",
            group_path=["sme", "sme2", "za-accumulate", "add-write"],
            display_name="svadd_write_za32_s32_vg1x2",
            intrinsic_names=["svadd_write_za32_s32_vg1x2"],
            instruction_names=["ADD"],
            prototype=(
                'void svadd_write_za32_s32_vg1x2(uint32_t slice, svint32x2_t zn, svint32x2_t zm) '
                '__arm_streaming __arm_inout("za")'
            ),
            header="<arm_sme.h>",
            feature_macros=["__ARM_FEATURE_SME2"],
            required_function_attributes=['__arm_streaming', '__arm_inout("za")'],
            operand_constraints=[
                "slice selects the ZA tile slice to update",
                "zn and zm are vg1x2 multi-vector operands with matching element types",
            ],
            immediate_constraints=["slice must be a valid ZA slice index for the selected tile shape"],
            vectorization_role="za-accumulate",
            tail_policy="Use slice-level predication and ZA slice boundaries instead of scalar cleanup code.",
            usage_template="svadd_write_za32_s32_vg1x2(slice, lhs_group, rhs_group);",
            correctness_rules=[
                "Gate the path on __ARM_FEATURE_SME2 and keep the function in streaming mode.",
                "Use only when the algorithm is already organized around ZA slices or tiles.",
            ],
            anti_patterns=[
                "Using SME2 ZA slice intrinsics in a plain SVE streaming loop with no ZA state.",
                "Treating slice-based ZA writes like a scalar store replacement.",
            ],
            related_items=[
                "attribute.sme.__arm_streaming",
                "attribute.sme.__arm_inout_za",
                "intrinsic.sme.svmopa_za32_s8_m",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME2 instruction intrinsics / ADD, SUB (store into ZA, multi)",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "svadd_write_za32"},
                {"type": "heading_exists", "heading": "ADD, SUB (store into ZA, multi)"},
            ],
        ),
        make_record(
            record_id="rule.neon.explicit-epilogue",
            kind="rule",
            isa="neon",
            group_path=["rules", "neon", "epilogue"],
            display_name="NEON loops must keep an explicit scalar epilogue",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_NEON"],
            required_function_attributes=[],
            operand_constraints=["Applies when a fixed-width NEON main loop advances by a constant lane count"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Require a scalar cleanup loop unless the trip count is proven to be an exact multiple of the NEON width.",
            usage_template="for (; i + 4 <= n; i += 4) { ... } for (; i < n; ++i) { ... }",
            correctness_rules=[
                "A fixed-width NEON loop must show how it handles the remainder elements.",
            ],
            anti_patterns=[
                "Ending the kernel after the NEON loop with no scalar cleanup.",
            ],
            related_items=[
                "intrinsic.neon.vld1q_f32",
                "intrinsic.neon.vst1q_f32",
            ],
            source_info=source(
                "Arm Neon Intrinsics Reference",
                NEON_URL,
                "Advanced SIMD (Neon) intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "neon", "value": "Arm Neon Intrinsics Reference"}],
        ),
        make_record(
            record_id="rule.sve.length-agnostic",
            kind="rule",
            isa="sve",
            group_path=["rules", "sve", "vector-length"],
            display_name="SVE loops must be vector-length agnostic",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["Applies when SVE intrinsics or SVE predicates are used"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Use svcnt*() and predicate-based masking instead of hard-coded lane counts.",
            usage_template="for (int i = 0; i < n; i += svcntw()) { ... }",
            correctness_rules=[
                "The loop step must come from svcnt*() or an equivalent scalable-width construct.",
            ],
            anti_patterns=[
                "Using i += 4 or another fixed lane count in an SVE loop.",
            ],
            related_items=[
                "intrinsic.sve.svcntw",
                "intrinsic.sve.svwhilelt_b32",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / SVE introduction",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "vector-length agnostic"}],
        ),
        make_record(
            record_id="rule.sve.predicated-load-store",
            kind="rule",
            isa="sve",
            group_path=["rules", "sve", "predication"],
            display_name="SVE loads and stores must show predicate coverage",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SVE"],
            required_function_attributes=[],
            operand_constraints=["Applies to predicated SVE memory access paths"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="The same predicate must guard the active lanes of the current step.",
            usage_template="svbool_t pg = svwhilelt_b32(...); svld1_f32(pg, ...); svst1_f32(pg, ...);",
            correctness_rules=[
                "Predicated loads and stores must use a live predicate argument, typically from svwhilelt_*.",
            ],
            anti_patterns=[
                "Using svld1_* or svst1_* with no visible predicate generation.",
            ],
            related_items=[
                "intrinsic.sve.svwhilelt_b32",
                "intrinsic.sve.svld1_f32",
                "intrinsic.sve.svst1_f32",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SVE language extensions and intrinsics / Mapping of SVE instructions to intrinsics",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "svwhilelt"}],
        ),
        make_record(
            record_id="rule.sve.header-required",
            kind="rule",
            isa="sve",
            group_path=["rules", "sve", "headers"],
            display_name="SVE and SME intrinsics must include the matching header",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SVE", "__ARM_FEATURE_SME"],
            required_function_attributes=[],
            operand_constraints=["Applies when code uses sv* or SME-specific __arm_* constructs"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Not applicable.",
            usage_template="#include <arm_sve.h>  // or <arm_sme.h> when SME/ZA intrinsics are used",
            correctness_rules=[
                "sv* intrinsics require <arm_sve.h>; SME/ZA intrinsics and attributes require <arm_sme.h>.",
            ],
            anti_patterns=[
                "Using svld1_f32 or svzero_za() with no Arm SVE or SME header include.",
            ],
            related_items=[
                "intrinsic.sve.svld1_f32",
                "intrinsic.sme.svzero_za",
                "attribute.sme.__arm_streaming",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "Header files / <arm_sve.h> / <arm_sme.h>",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "<arm_sve.h>"},
                {"type": "text_contains", "source": "acle", "value": "<arm_sme.h>"},
            ],
        ),
        make_record(
            record_id="rule.sme.streaming-required",
            kind="rule",
            isa="sme",
            group_path=["rules", "sme", "streaming-mode"],
            display_name="SME intrinsics must appear in a streaming-capable context",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Applies when ZA or SME instruction intrinsics are present"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Not applicable.",
            usage_template='void kernel(...) __arm_streaming __arm_inout("za");',
            correctness_rules=[
                "ZA and SME instruction intrinsics require a streaming-aware function context.",
            ],
            anti_patterns=[
                "Calling svmopa_* or svadd_write_za* from a plain C function with no streaming attribute.",
            ],
            related_items=[
                "attribute.sme.__arm_streaming",
                "intrinsic.sme.svmopa_za32_s8_m",
                "intrinsic.sme2.svadd_write_za32_s32_vg1x2",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "Header files / <arm_sme.h> / SME keyword attributes related to streaming mode",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "__arm_streaming"}],
        ),
        make_record(
            record_id="rule.sme.za-ownership",
            kind="rule",
            isa="sme",
            group_path=["rules", "sme", "za-ownership"],
            display_name="ZA intrinsics must declare ownership at the function boundary",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=[
                '__arm_inout("za")',
                '__arm_new("za")',
                '__arm_out("za")',
            ],
            operand_constraints=["Applies when code uses ZA state, tile or slice intrinsics"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Not applicable.",
            usage_template='void kernel(...) __arm_streaming __arm_inout("za");',
            correctness_rules=[
                "At least one ZA ownership attribute must be visible when a function uses ZA state.",
            ],
            anti_patterns=[
                "Calling ZA intrinsics while omitting all of __arm_inout(\"za\"), __arm_new(\"za\") and __arm_out(\"za\").",
            ],
            related_items=[
                "attribute.sme.__arm_inout_za",
                "attribute.sme.__arm_new_za",
                "attribute.sme.__arm_out_za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME ZA state assertions",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": '__arm_inout("za")'}],
        ),
        make_record(
            record_id="rule.sme.zero-za-fresh-za",
            kind="rule",
            isa="sme",
            group_path=["rules", "sme", "za-initialization"],
            display_name="svzero_za() requires fresh-ZA intent or explicit ZA output",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=['__arm_new("za")', '__arm_out("za")'],
            operand_constraints=["Applies when svzero_za() is present"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Not applicable.",
            usage_template='void kernel(...) __arm_streaming __arm_new("za"); svzero_za();',
            correctness_rules=[
                "svzero_za() should only appear in a fresh-ZA path or a function that explicitly outputs ZA state.",
            ],
            anti_patterns=[
                "Zeroing ZA in a kernel that should preserve or continue caller-owned accumulation.",
            ],
            related_items=[
                "intrinsic.sme.svzero_za",
                "attribute.sme.__arm_new_za",
                "attribute.sme.__arm_out_za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / SME instruction intrinsics / ZERO",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "svzero_za"}],
        ),
        make_record(
            record_id="rule.sme.inline-asm-standalone-za",
            kind="rule",
            isa="sme",
            group_path=["rules", "sme", "inline-asm"],
            display_name="SME ZA inline asm must be standalone and scan-verifiable",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=["__arm_streaming"],
            operand_constraints=["Applies when generated code uses smstart/smstop, ZERO, FMOPA, MOVA or ZA ST1W inline asm."],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Use predicates for partial tile edges; do not hide tail handling behind unmasked stores.",
            usage_template="__arm_streaming + smstart za + zero {za} + fmopa + predicated st1w + smstop za",
            correctness_rules=[
                "Standalone SME ZA inline asm must include explicit ZA state start/stop, fresh zeroing, outer-product accumulation and predicated write-back.",
                "Compile to assembly and scan for required SME instructions plus absence of __arm_tpidr2_* calls.",
            ],
            anti_patterns=[
                "Returning success for inline asm that was not compiled, linked and scanned.",
                "Using __arm_new(\"za\") in the standalone inline-asm path before SME ABI runtime support is verified.",
            ],
            related_items=[
                "instruction.sme.inline_asm.smstart_za",
                "instruction.sme.inline_asm.fmopa_f32",
                "instruction.sme.inline_asm.st1w_za0h_s32",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME language extensions and intrinsics / Controlling the use of streaming mode / SME instruction intrinsics",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": "SMSTART"},
                {"type": "text_contains", "source": "acle", "value": "FMOPA"},
                {"type": "text_contains", "source": "acle", "value": "ST1W"},
            ],
        ),
        make_record(
            record_id="rule.sme.no-abi-runtime-stubs",
            kind="rule",
            isa="sme",
            group_path=["rules", "sme", "abi-runtime"],
            display_name="Do not fake SME ABI support routines with weak stubs",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_FEATURE_SME"],
            required_function_attributes=[],
            operand_constraints=["Applies when generated code or link output references SME ABI support routines."],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Not applicable.",
            usage_template="nm -u candidate.compare | rg '__arm_tpidr2|__arm_za_disable'",
            correctness_rules=[
                "Generated SME source must not define weak __arm_tpidr2_save, __arm_tpidr2_restore or __arm_za_disable stubs.",
                "If these symbols remain unresolved after linking, return success=false unless the target runtime is known to provide them.",
            ],
            anti_patterns=[
                "Adding weak stubs to make an executable link while silently disabling the real SME ABI contract.",
            ],
            related_items=[
                "attribute.sme.__arm_new_za",
                "instruction.sme.inline_asm.smstart_za",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "SME ABI state and SME ZA state assertions",
                retrieved_at,
            ),
            validation=[
                {"type": "text_contains", "source": "acle", "value": '__arm_new("za")'},
            ],
        ),
        make_record(
            record_id="rule.cross-isa.no-mixed-templates",
            kind="rule",
            isa="sme",
            group_path=["rules", "cross-isa", "template-consistency"],
            display_name="Do not mix NEON, SVE and SME templates in one kernel body",
            intrinsic_names=[],
            instruction_names=[],
            prototype="",
            header="",
            feature_macros=["__ARM_NEON", "__ARM_FEATURE_SVE", "__ARM_FEATURE_SME"],
            required_function_attributes=[],
            operand_constraints=["Applies whenever more than one SIMD ISA family appears in the same snippet"],
            immediate_constraints=[],
            vectorization_role="rule",
            tail_policy="Use one ISA template per kernel body; move cross-ISA dispatch to a higher layer.",
            usage_template="Dispatch NEON, SVE and SME implementations in separate functions instead of mixing them inline.",
            correctness_rules=[
                "A generated kernel body should follow one SIMD model at a time: fixed-width NEON, scalable SVE or streaming/ZA SME.",
            ],
            anti_patterns=[
                "Calling vld1q_f32 and svld1_f32 in the same loop body.",
                "Mixing NEON arithmetic with SME ZA ownership attributes inside one generated kernel.",
            ],
            related_items=[
                "intrinsic.neon.vld1q_f32",
                "intrinsic.sve.svld1_f32",
                "attribute.sme.__arm_streaming",
            ],
            source_info=source(
                "Arm C Language Extensions",
                ACLE_URL,
                "Advanced SIMD intrinsics / SVE introduction / SME keyword attributes",
                retrieved_at,
            ),
            validation=[{"type": "text_contains", "source": "acle", "value": "Advanced SIMD (Neon) intrinsics"}],
        ),
    ]
    records.extend(build_sme_inline_asm_instruction_records(retrieved_at))
    return records


SCHEMA_PAYLOAD = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "apply-vectorization Arm intrinsics knowledge base record",
    "type": "object",
    "required": [
        "id",
        "kind",
        "isa",
        "group_path",
        "display_name",
        "intrinsic_names",
        "instruction_names",
        "prototype",
        "header",
        "feature_macros",
        "required_function_attributes",
        "operand_constraints",
        "immediate_constraints",
        "vectorization_role",
        "tail_policy",
        "usage_template",
        "correctness_rules",
        "anti_patterns",
        "related_items",
        "source",
    ],
    "properties": {
        "id": {"type": "string"},
        "kind": {"enum": ["intrinsic", "instruction", "attribute", "rule"]},
        "isa": {"enum": ["neon", "sve", "sve2", "sme", "sme2"]},
        "group_path": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "display_name": {"type": "string"},
        "intrinsic_names": {"type": "array", "items": {"type": "string"}},
        "instruction_names": {"type": "array", "items": {"type": "string"}},
        "prototype": {"type": "string"},
        "header": {"type": "string"},
        "feature_macros": {"type": "array", "items": {"type": "string"}},
        "required_function_attributes": {"type": "array", "items": {"type": "string"}},
        "operand_constraints": {"type": "array", "items": {"type": "string"}},
        "immediate_constraints": {"type": "array", "items": {"type": "string"}},
        "vectorization_role": {"type": "string"},
        "tail_policy": {"type": "string"},
        "usage_template": {"type": "string"},
        "correctness_rules": {"type": "array", "items": {"type": "string"}},
        "anti_patterns": {"type": "array", "items": {"type": "string"}},
        "related_items": {"type": "array", "items": {"type": "string"}},
        "source": {
            "type": "object",
            "required": ["title", "url", "section", "retrieved_at"],
            "properties": {
                "title": {"type": "string"},
                "url": {"type": "string"},
                "section": {"type": "string"},
                "retrieved_at": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


def validate_official_presence(
    records: list[dict[str, Any]],
    *,
    neon_text: str,
    acle_text: str,
    optional_texts: dict[str, str],
    headings: set[str],
    sve_mapping: dict[str, str],
) -> list[dict[str, Any]]:
    """Validate all curated records against the fetched official sources."""

    validated_records: list[dict[str, Any]] = []
    for record in records:
        for rule in record["_validation"]:
            rule_type = rule["type"]
            if rule_type == "text_contains":
                text_source = neon_text if rule["source"] == "neon" else acle_text
                if rule["value"] not in text_source:
                    raise RefreshError(
                        "whitelist_error",
                        f"curated record {record['id']} could not be verified from official text: {rule['value']}",
                    )
            elif rule_type == "optional_text_contains":
                text_source = optional_texts.get(rule["source"], "")
                if text_source and rule["value"] not in text_source:
                    raise RefreshError(
                        "whitelist_error",
                        "curated record "
                        f"{record['id']} could not be verified from optional official text "
                        f"{rule['source']}: {rule['value']}",
                    )
            elif rule_type == "mapping_equals":
                actual = sve_mapping.get(rule["instruction"])
                if actual != rule["expected_intrinsic"]:
                    raise RefreshError(
                        "whitelist_error",
                        "curated record "
                        f"{record['id']} expected SVE mapping {rule['instruction']} -> "
                        f"{rule['expected_intrinsic']}, got {actual!r}",
                    )
            elif rule_type == "heading_exists":
                if rule["heading"] not in headings:
                    raise RefreshError(
                        "structure_error",
                        f"ACLE page no longer exposes expected heading: {rule['heading']}",
                    )
            else:
                raise RefreshError(
                    "parse_error",
                    f"curated record {record['id']} uses unsupported validation type: {rule_type}",
                )
        cleaned = deepcopy(record)
        cleaned.pop("_validation", None)
        validated_records.append(cleaned)
    return sorted(validated_records, key=record_sort_key)


def split_snapshots(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split the validated records into the repository snapshot files."""

    grouped = {name: [] for name in SNAPSHOT_FILES}
    for record in records:
        if record["kind"] == "attribute" or record["kind"] == "rule":
            grouped["attributes.json"].append(record)
        elif record["isa"] == "neon":
            grouped["neon.json"].append(record)
        elif record["isa"] in {"sve", "sve2"}:
            grouped["sve.json"].append(record)
        else:
            grouped["sme.json"].append(record)
    return grouped


def build_index(
    records: list[dict[str, Any]],
    snapshots: dict[str, list[dict[str, Any]]],
    retrieved_at: str,
) -> dict[str, Any]:
    """Build the index.json metadata payload."""

    counts_by_isa: dict[str, int] = {}
    counts_by_kind: dict[str, int] = {}
    for record in records:
        counts_by_isa[record["isa"]] = counts_by_isa.get(record["isa"], 0) + 1
        counts_by_kind[record["kind"]] = counts_by_kind.get(record["kind"], 0) + 1

    return {
        "schema_version": "1.0.0",
        "retrieved_at": retrieved_at,
        "scope": "Curated vectorization-focused Arm intrinsics and attributes for apply-vectorization.",
        "automation_policy": {
            "fetched_sources": [ACLE_URL, NEON_URL],
            "optional_official_sources": [SME_INTRO_BLOG_URL, SME_INSTRUCTIONS_BLOG_URL],
            "reference_only_sources": [
                ARM_SIMD_URL,
                SME_SEARCH_ANNOUNCEMENT_URL,
                DDI0602_SME_INSTRUCTIONS_URL,
                DDI0602_SVE_INSTRUCTIONS_URL,
            ],
        },
        "snapshots": [
            {"file": file_name, "count": len(snapshots[file_name])}
            for file_name in SNAPSHOT_FILES
        ],
        "counts_by_isa": counts_by_isa,
        "counts_by_kind": counts_by_kind,
        "sources": [
            {
                "title": "Arm C Language Extensions",
                "url": ACLE_URL,
                "fetch_mode": "html-scraped",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Arm Neon Intrinsics Reference",
                "url": NEON_URL,
                "fetch_mode": "html-scraped",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Arm SIMD: Optimize, Migrate, and Accelerate C/C++ for Peak Performance",
                "url": ARM_SIMD_URL,
                "fetch_mode": "reference-only",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Scalable Matrix Extension: Expanding the Arm Intrinsics Search Engine",
                "url": SME_SEARCH_ANNOUNCEMENT_URL,
                "fetch_mode": "reference-only",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Part 1: Arm Scalable Matrix Extension (SME) Introduction",
                "url": SME_INTRO_BLOG_URL,
                "fetch_mode": "optional-html; reference-only when developer.arm.com returns 403",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Part 2: Arm Scalable Matrix Extension (SME) Instructions",
                "url": SME_INSTRUCTIONS_BLOG_URL,
                "fetch_mode": "optional-html; reference-only when developer.arm.com returns 403",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Arm A-profile A64 Instruction Set Architecture / SME Instructions",
                "url": DDI0602_SME_INSTRUCTIONS_URL,
                "fetch_mode": "reference-only; direct scripted access can return 403",
                "retrieved_at": retrieved_at,
            },
            {
                "title": "Arm A-profile A64 Instruction Set Architecture / SVE Instructions",
                "url": DDI0602_SVE_INSTRUCTIONS_URL,
                "fetch_mode": "reference-only; direct scripted access can return 403",
                "retrieved_at": retrieved_at,
            },
        ],
    }


def main() -> int:
    """Refresh the database snapshots."""

    args = parse_args()
    acle_html = fetch_html(ACLE_URL)
    neon_html = fetch_html(NEON_URL)

    acle_soup = BeautifulSoup(acle_html, "html.parser")
    neon_soup = BeautifulSoup(neon_html, "html.parser")
    acle_text = normalize_whitespace(acle_soup.get_text("\n"))
    neon_text = normalize_whitespace(neon_soup.get_text("\n"))
    optional_texts = {
        "sme_intro_blog": fetch_optional_official_text(SME_INTRO_BLOG_URL),
        "sme_instructions_blog": fetch_optional_official_text(SME_INSTRUCTIONS_BLOG_URL),
        "ddi0602_sme": fetch_optional_official_text(DDI0602_SME_INSTRUCTIONS_URL),
        "ddi0602_sve": fetch_optional_official_text(DDI0602_SVE_INSTRUCTIONS_URL),
    }
    headings = extract_headings(acle_soup)
    sve_mapping = build_sve_mapping(acle_soup)

    curated_records = build_curated_records(args.retrieved_at)
    validated_records = validate_official_presence(
        curated_records,
        neon_text=neon_text,
        acle_text=acle_text,
        optional_texts=optional_texts,
        headings=headings,
        sve_mapping=sve_mapping,
    )
    snapshots = split_snapshots(validated_records)
    index_payload = build_index(validated_records, snapshots, args.retrieved_at)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_json(args.output_dir / "schema.json", SCHEMA_PAYLOAD)
    save_json(args.output_dir / "index.json", index_payload)
    for file_name, payload in snapshots.items():
        save_json(args.output_dir / file_name, payload)

    print(
        "[完成] 已刷新 Arm intrinsics 知识库: "
        f"{sum(len(payload) for payload in snapshots.values())} 条记录 -> {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RefreshError as exc:
        print(f"[{exc.category}] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
