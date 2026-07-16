#!/usr/bin/env python3
"""Generate quick-reference Arm intrinsics manuals from JSON snapshots."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from arm_intrinsics_db_common import DEFAULT_DB_DIR, DEFAULT_MANUAL_DIR, load_index, load_records


PAGE_TITLES = {
    "neon": "ARM NEON Quick Reference",
    "sve": "ARM SVE / SVE2 Quick Reference",
    "sme": "ARM SME / SME2 Quick Reference",
}

PAGE_INTROS = {
    "neon": "面向固定 128-bit 向量化循环的 NEON quick reference，聚焦主向量循环、广播、加法、FMA、sum/dot reduction 和显式尾处理。",
    "sve": "面向长度无关循环的 SVE / SVE2 quick reference，聚焦 svcnt*、谓词、predicated load/store、广播、加法、FMA、horizontal reduction 和 dot-product。",
    "sme": "面向 streaming / ZA / slice 场景的 SME / SME2 quick reference，聚焦函数属性、ZA ownership、ZA 初始化和常用矩阵累加入口。",
}

PAGE_DESCRIPTIONS = {
    "neon": "主向量循环 + 标量尾处理",
    "sve": "长度无关循环 + 谓词化访存",
    "sme": "streaming / ZA ownership / tile-slice",
}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Generate quick-reference Arm intrinsics manuals from JSON snapshots."
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=DEFAULT_DB_DIR,
        help="Directory containing index.json and the snapshot files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_MANUAL_DIR,
        help="Destination directory for the generated Markdown manual.",
    )
    return parser.parse_args()


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with a trailing newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render a Markdown table."""

    if not rows:
        return []
    normalized_rows = [[cell.replace("\n", "<br>") for cell in row] for row in rows]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in normalized_rows:
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def first_or_empty(values: Iterable[str]) -> str:
    """Return the first non-empty string from an iterable."""

    for value in values:
        stripped = value.strip()
        if stripped:
            return stripped
    return ""


def instruction_label(record: dict[str, object]) -> str:
    """Return the display label used in quick reference tables."""

    return str(record["display_name"])


def instruction_family(record: dict[str, object]) -> str:
    """Return the instruction family summary for one record."""

    instruction_names = list(record["instruction_names"])
    if instruction_names:
        return ", ".join(f"`{name}`" for name in instruction_names)
    if record["kind"] == "attribute":
        return "`attribute`"
    return "`rule`"


def concise_use_case(record: dict[str, object]) -> str:
    """Return a short 'use when' summary."""

    if record["vectorization_role"] == "load":
        return "连续读取向量主体"
    if record["vectorization_role"] == "store":
        return "写回向量主体"
    if record["vectorization_role"] == "broadcast":
        return "广播标量到向量"
    if record["vectorization_role"] == "add":
        return "逐元素加法"
    if record["vectorization_role"] == "fma":
        return "乘加或累加内核"
    if record["vectorization_role"] == "horizontal-reduction":
        return "sum/dot 最终水平归约"
    if record["vectorization_role"] == "predicate":
        return "生成当前步谓词"
    if record["vectorization_role"] == "vector-length":
        return "获取当前 VL"
    if record["vectorization_role"] == "dot-product":
        return "SVE2 点积"
    if record["vectorization_role"] == "streaming-attribute":
        return "声明 streaming 上下文"
    if record["vectorization_role"] == "za-ownership":
        return "声明 ZA ownership"
    if record["vectorization_role"] == "za-init":
        return "初始化 ZA"
    if record["vectorization_role"] == "matrix-outer-product":
        return "outer-product 累加到 ZA"
    if record["vectorization_role"] == "za-accumulate":
        return "对 ZA slice 做多向量写入/累加"
    if record["vectorization_role"] == "sme-inline-state":
        return "显式管理 ZA 可访问范围"
    if record["vectorization_role"] == "sme-inline-za-init":
        return "inline asm fresh-ZA 清零"
    if record["vectorization_role"] == "sme-inline-outer-product":
        return "inline asm 外积累加到 ZA"
    if record["vectorization_role"] == "sme-inline-za-store":
        return "inline asm 写回 ZA slice"
    if record["vectorization_role"] == "sme-inline-za-read":
        return "inline asm 读出 ZA slice"
    if record["vectorization_role"] == "sme-inline-predicate":
        return "inline asm 生成 tile 谓词"
    if record["vectorization_role"] == "sme-inline-load":
        return "inline asm 谓词化载入"
    if record["vectorization_role"] == "sme-inline-beta":
        return "inline asm beta 路径合并"
    if record["vectorization_role"] == "rule":
        return "静态检查规则"
    return str(record["vectorization_role"])


def concise_constraint(record: dict[str, object]) -> str:
    """Return one short key constraint string."""

    return first_or_empty(
        [
            *record["correctness_rules"],
            *record["operand_constraints"],
            *record["anti_patterns"],
            str(record["tail_policy"]),
        ]
    )


def render_code_block(language: str, code: str) -> list[str]:
    """Render a fenced code block."""

    return [f"```{language}", code.rstrip(), "```", ""]


def render_compact_entry(record: dict[str, object]) -> list[str]:
    """Render a compact quick-lookup entry."""

    source = record["source"]
    lines = [f"### `{instruction_label(record)}`", ""]
    lines.append(f"- `Maps To`: {instruction_family(record)}")
    if record["prototype"]:
        lines.append(f"- `Prototype`: `{record['prototype']}`")
    if record["header"]:
        lines.append(f"- `Header`: `{record['header']}`")
    if record["feature_macros"]:
        lines.append(
            "- `Feature Gate`: "
            + ", ".join(f"`{value}`" for value in record["feature_macros"])
        )
    if record["required_function_attributes"]:
        lines.append(
            "- `Required Attributes`: "
            + ", ".join(f"`{value}`" for value in record["required_function_attributes"])
        )
    lines.append(f"- `Use When`: {concise_use_case(record)}")
    key_rule = first_or_empty(record["correctness_rules"])
    if key_rule:
        lines.append(f"- `Key Rule`: {key_rule}")
    key_mistake = first_or_empty(record["anti_patterns"])
    if key_mistake:
        lines.append(f"- `Common Mistake`: {key_mistake}")
    if record["usage_template"]:
        lines.append("- `Example`:")
        lines.extend(render_code_block("c", str(record["usage_template"])))
    lines.append(
        "- `Source`: "
        f"[{source['title']}]({source['url']}) / {source['section']} / {source['retrieved_at']}"
    )
    lines.append("")
    return lines


def page_records(records: list[dict[str, object]], page_name: str) -> list[dict[str, object]]:
    """Select records for one generated page."""

    if page_name == "neon":
        return [record for record in records if record["isa"] == "neon" and record["kind"] != "rule"]
    if page_name == "sve":
        return [record for record in records if record["isa"] in {"sve", "sve2"} and record["kind"] != "rule"]
    return [record for record in records if record["isa"] in {"sme", "sme2"} and record["kind"] != "rule"]


def page_rules(records: list[dict[str, object]], page_name: str) -> list[dict[str, object]]:
    """Select rule records relevant to one page."""

    if page_name == "neon":
        allowed = {"rule.neon.explicit-epilogue", "rule.cross-isa.no-mixed-templates"}
    elif page_name == "sve":
        allowed = {
            "rule.sve.length-agnostic",
            "rule.sve.predicated-load-store",
            "rule.sve.header-required",
            "rule.cross-isa.no-mixed-templates",
        }
    else:
        allowed = {
            "rule.sme.streaming-required",
            "rule.sme.za-ownership",
            "rule.sme.zero-za-fresh-za",
            "rule.sme.inline-asm-standalone-za",
            "rule.sme.no-abi-runtime-stubs",
            "rule.sve.header-required",
            "rule.cross-isa.no-mixed-templates",
        }
    return [record for record in records if record["id"] in allowed]


def record_by_id(records: list[dict[str, object]], record_id: str) -> dict[str, object] | None:
    """Return one record by id."""

    for record in records:
        if record["id"] == record_id:
            return record
    return None


def headers_feature_rows(records: list[dict[str, object]]) -> list[list[str]]:
    """Build header and feature rows."""

    rows: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        header = str(record["header"]).strip() or "`n/a`"
        features = ", ".join(f"`{value}`" for value in record["feature_macros"]) or "`none`"
        key = (header, features)
        if key in seen:
            continue
        seen.add(key)
        rows.append([header, features, concise_use_case(record)])
    return rows


def role_rows(records: list[dict[str, object]], role_order: list[str]) -> list[list[str]]:
    """Build quick-reference rows ordered by vectorization role."""

    rows: list[list[str]] = []
    for role_name in role_order:
        for record in records:
            if record["vectorization_role"] != role_name:
                continue
            rows.append(
                [
                    f"`{instruction_label(record)}`",
                    instruction_family(record),
                    concise_use_case(record),
                    concise_constraint(record),
                ]
            )
    return rows


def pitfall_lines(rule_records: list[dict[str, object]]) -> list[str]:
    """Render common pitfalls from rule records."""

    lines: list[str] = []
    for record in rule_records:
        mistake = first_or_empty(record["anti_patterns"])
        if mistake:
            lines.append(f"- `{record['display_name']}`: {mistake}")
    return lines


def render_neon_page(records: list[dict[str, object]], rule_records: list[dict[str, object]]) -> str:
    """Render the NEON quick reference page."""

    lines = [
        f"# {PAGE_TITLES['neon']}",
        "",
        "<!-- Generated by scripts/generate_arm_intrinsics_manual.py. Do not edit manually. -->",
        "",
        PAGE_INTROS["neon"],
        "",
        "## Headers and Feature Gates",
        "",
    ]
    lines.extend(render_table(["Header", "Feature Gate", "Typical Use"], headers_feature_rows(records)))
    lines.extend(
        [
            "## Standard Vector Loop Pattern",
            "",
        ]
    )
    lines.extend(
        render_code_block(
            "c",
            """#include <arm_neon.h>

void add_arrays_f32(const float *a, const float *b, float *out, int n) {
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t va = vld1q_f32(a + i);
        float32x4_t vb = vld1q_f32(b + i);
        float32x4_t vc = vaddq_f32(va, vb);
        vst1q_f32(out + i, vc);
    }
    for (; i < n; ++i) {
        out[i] = a[i] + b[i];
    }
}""",
        )
    )
    lines.extend(["## Load / Store Patterns", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["load", "store"]),
        )
    )
    lines.extend(["## Arithmetic and Broadcast", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["broadcast", "add", "fma"]),
        )
    )
    lines.extend(["## Reduction Helpers", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["horizontal-reduction"]),
        )
    )
    lines.extend(["## Common Pitfalls", ""])
    lines.extend(pitfall_lines(rule_records))
    lines.append("")
    lines.extend(["## Quick Lookup", ""])
    for record_id in (
        "intrinsic.neon.vld1q_f32",
        "intrinsic.neon.vst1q_f32",
        "intrinsic.neon.vdupq_n_f32",
        "intrinsic.neon.vaddq_f32",
        "intrinsic.neon.vfmaq_f32",
        "intrinsic.neon.vaddvq_f32",
        "intrinsic.neon.vaddvq_s32",
    ):
        record = record_by_id(records, record_id)
        if record:
            lines.extend(render_compact_entry(record))
    return "\n".join(lines)


def render_sve_page(records: list[dict[str, object]], rule_records: list[dict[str, object]]) -> str:
    """Render the SVE / SVE2 quick reference page."""

    lines = [
        f"# {PAGE_TITLES['sve']}",
        "",
        "<!-- Generated by scripts/generate_arm_intrinsics_manual.py. Do not edit manually. -->",
        "",
        PAGE_INTROS["sve"],
        "",
        "## Headers and Feature Gates",
        "",
    ]
    lines.extend(render_table(["Header", "Feature Gate", "Typical Use"], headers_feature_rows(records)))
    lines.extend(["## Predicate and Vector-Length Primitives", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["vector-length", "predicate"]),
        )
    )
    lines.extend(["## Load / Store Patterns", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["load", "store"]),
        )
    )
    lines.extend(["## Arithmetic and Broadcast", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["broadcast", "add", "fma"]),
        )
    )
    lines.extend(["## Reduction Helpers", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["horizontal-reduction"]),
        )
    )
    lines.extend(["## SVE2 Extensions", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["dot-product"]),
        )
    )
    lines.extend(["## Loop Control Pattern", ""])
    lines.extend(
        render_code_block(
            "c",
            """#include <arm_sve.h>
#include <stdint.h>

void add_arrays_f32_sve(const float *a, const float *b, float *out, int n) {
    const int vl = (int)svcntw();
    for (int i = 0; i < n; i += vl) {
        svbool_t pg = svwhilelt_b32((uint64_t)i, (uint64_t)n);
        svfloat32_t va = svld1_f32(pg, a + i);
        svfloat32_t vb = svld1_f32(pg, b + i);
        svfloat32_t vc = svadd_f32_x(pg, va, vb);
        svst1_f32(pg, out + i, vc);
    }
}""",
        )
    )
    lines.extend(["## Common Pitfalls", ""])
    lines.extend(pitfall_lines(rule_records))
    lines.append("")
    lines.extend(["## Quick Lookup", ""])
    for record_id in (
        "intrinsic.sve.svcntw",
        "intrinsic.sve.svwhilelt_b32",
        "intrinsic.sve.svld1_f32",
        "intrinsic.sve.svst1_f32",
        "intrinsic.sve.svdup_n_f32",
        "intrinsic.sve.svadd_f32_x",
        "intrinsic.sve.svmla_f32_x",
        "intrinsic.sve.svaddv_f32",
        "intrinsic.sve.svaddv_s32",
        "intrinsic.sve2.svdot_s32",
    ):
        record = record_by_id(records, record_id)
        if record:
            lines.extend(render_compact_entry(record))
    return "\n".join(lines)


def render_sme_page(records: list[dict[str, object]], rule_records: list[dict[str, object]]) -> str:
    """Render the SME / SME2 quick reference page."""

    lines = [
        f"# {PAGE_TITLES['sme']}",
        "",
        "<!-- Generated by scripts/generate_arm_intrinsics_manual.py. Do not edit manually. -->",
        "",
        PAGE_INTROS["sme"],
        "",
        "## Headers and Feature Gates",
        "",
    ]
    lines.extend(render_table(["Header", "Feature Gate", "Typical Use"], headers_feature_rows(records)))
    lines.extend(["## Streaming Mode and ZA Ownership", ""])
    lines.extend(
        render_table(
            ["Attribute", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["streaming-attribute", "za-ownership"]),
        )
    )
    lines.extend(["## Default Streaming-Compatible Pattern", ""])
    lines.extend(
        render_code_block(
            "c",
            """#include <arm_sme.h>
#include <stdint.h>

void add_arrays_f32_sme(const float *a, const float *b, float *out, int n) __arm_streaming;
void add_arrays_f32_sme(const float *a, const float *b, float *out, int n) {
    const int vl = (int)svcntw();
    for (int i = 0; i < n; i += vl) {
        svbool_t pg = svwhilelt_b32((uint64_t)i, (uint64_t)n);
        svfloat32_t va = svld1_f32(pg, a + i);
        svfloat32_t vb = svld1_f32(pg, b + i);
        svfloat32_t vc = svadd_f32_x(pg, va, vb);
        svst1_f32(pg, out + i, vc);
    }
}""",
        )
    )
    lines.extend(["## ZA Initialization and Accumulation", ""])
    lines.extend(
        render_table(
            ["Intrinsic", "Maps To", "Use For", "Key Constraint"],
            role_rows(records, ["za-init", "matrix-outer-product", "za-accumulate"]),
        )
    )
    lines.extend(["## SME ZA Inline ASM Instructions", ""])
    lines.extend(
        render_table(
            ["Instruction", "Maps To", "Use For", "Key Constraint"],
            role_rows(
                records,
                [
                    "sme-inline-state",
                    "sme-inline-za-init",
                    "sme-inline-predicate",
                    "sme-inline-load",
                    "sme-inline-outer-product",
                    "sme-inline-za-read",
                    "sme-inline-za-store",
                    "sme-inline-beta",
                ],
            ),
        )
    )
    lines.extend(["## Standalone ZA SGEMM Inline ASM Pattern", ""])
    lines.extend(
        render_code_block(
            "c",
            """#include <arm_sme.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

static void sgemm_tile_za_inline(
    int row_count, int col_count, int k,
    const float *a_work, const float *b_panel,
    float *c_tile, int ldc, float beta) __arm_streaming;
static void sgemm_tile_za_inline(
    int row_count, int col_count, int k,
    const float *a_work, const float *b_panel,
    float *c_tile, int ldc, float beta) __arm_streaming {
    __asm__ volatile(
        "smstart za\\n"
        "zero {za}\\n"
        :
        :
        : "memory", "za"
    );

    for (int kk = 0; kk < k; ++kk) {
        const float *a_vec = a_work + (size_t)kk * row_count;
        const float *b_vec = b_panel + (size_t)kk * col_count;
        __asm__ volatile(
            "whilelo p0.s, xzr, %x[row_count]\\n"
            "whilelo p1.s, xzr, %x[col_count]\\n"
            "ld1w { z0.s }, p0/z, [%[a_vec]]\\n"
            "ld1w { z1.s }, p1/z, [%[b_vec]]\\n"
            "fmopa za0.s, p0/m, p1/m, z0.s, z1.s\\n"
            :
            : [a_vec] "r"(a_vec),
              [b_vec] "r"(b_vec),
              [row_count] "r"((uint64_t)row_count),
              [col_count] "r"((uint64_t)col_count)
            : "memory", "p0", "p1", "z0", "z1", "za"
        );
    }

    uint32_t beta_bits;
    memcpy(&beta_bits, &beta, sizeof(beta_bits));
    for (int tile_row = 0; tile_row < row_count; ++tile_row) {
        float *c_row = c_tile + (size_t)tile_row * ldc;
        if (beta == 0.0f) {
            __asm__ volatile(
                "whilelo p1.s, xzr, %x[col_count]\\n"
                "mov w12, %w[tile_row]\\n"
                "st1w {za0h.s[w12, 0]}, p1, [%[c_row]]\\n"
                :
                : [col_count] "r"((uint64_t)col_count),
                  [tile_row] "r"(tile_row),
                  [c_row] "r"(c_row)
                : "memory", "p1", "w12", "za"
            );
        } else {
            __asm__ volatile(
                "whilelo p1.s, xzr, %x[col_count]\\n"
                "mov w12, %w[tile_row]\\n"
                "mova z2.s, p1/m, za0h.s[w12, 0]\\n"
                "ld1w { z3.s }, p1/z, [%[c_row]]\\n"
                "dup z4.s, %w[beta_bits]\\n"
                "fmla z2.s, p1/m, z3.s, z4.s\\n"
                "st1w { z2.s }, p1, [%[c_row]]\\n"
                :
                : [col_count] "r"((uint64_t)col_count),
                  [tile_row] "r"(tile_row),
                  [c_row] "r"(c_row),
                  [beta_bits] "r"(beta_bits)
                : "memory", "p1", "z2", "z3", "z4", "w12", "za"
            );
        }
    }

    __asm__ volatile(
        "smstop za\\n"
        :
        :
        : "memory", "za"
    );
}""",
        )
    )
    lines.extend(
        [
            "Canonical ZA slice store placeholder: `st1w {za0h.s[...]}`. Clang may print the MOVA alias as `mov z*.s, p*/m, za*h.s[...]` in emitted assembly.",
            "",
            "## Inline ASM Forbidden Items",
            "",
            "- Do not emit weak stubs for `__arm_tpidr2_save`, `__arm_tpidr2_restore` or `__arm_za_disable`.",
            "- Do not use `__arm_new(\"za\")` in standalone generated source unless the target link environment has already proved those support routines exist.",
            "- Do not return `success=true` without compile, link, assembly scan and unresolved-symbol scan results.",
            "",
            "## Inline ASM Validation Commands",
            "",
        ]
    )
    lines.extend(
        render_code_block(
            "bash",
            """clang -std=c11 -O3 -march=armv9.2-a+sme -msve-vector-bits=scalable -S candidate.c -o candidate.s
rg 'smstart[[:space:]]+za|zero[[:space:]]+\\{za\\}|fmopa|st1w|smstop[[:space:]]+za' candidate.s
rg '__arm_tpidr2|__arm_za_disable' candidate.s
clang -std=c11 -O3 -march=armv9.2-a+sme -msve-vector-bits=scalable candidate.c driver.c -o candidate.compare
nm -u candidate.compare | rg '__arm_tpidr2|__arm_za_disable'""",
        )
    )
    lines.extend(["## ZA Entry Checklist", ""])
    lines.extend(
        [
            "- Enter ZA/tile only for explicit GEMM, rank-k or outer-product style two-dimensional accumulation.",
            "- Keep elementwise, masked elementwise, sum/dot reduction and GEMV on NEON, SVE or SME streaming-compatible paths.",
            "- A ZA/tile response must state output tile mapping, row/column dimensions, K accumulation dimension, ZA ownership, boundary predication and write-back timing.",
            "- Inline asm ZA responses must state the `smstart za` / `smstop za` range, `zero {za}` timing, `fmopa` predicates, `st1w {za0h.s[...]}` write-back and clobbers.",
            "- If any tile mapping or ownership detail is unclear, reject the ZA path or fall back to streaming-compatible code.",
            "",
        ]
    )
    lines.extend(["## Common Pitfalls", ""])
    lines.extend(pitfall_lines(rule_records))
    lines.append("")
    lines.extend(["## Quick Lookup", ""])
    for record_id in (
        "attribute.sme.__arm_streaming",
        "attribute.sme.__arm_streaming_compatible",
        "attribute.sme.__arm_inout_za",
        "attribute.sme.__arm_new_za",
        "attribute.sme.__arm_out_za",
        "intrinsic.sme.svzero_za",
        "intrinsic.sme.svmopa_za32_s8_m",
        "intrinsic.sme2.svadd_write_za32_s32_vg1x2",
        "instruction.sme.inline_asm.smstart_za",
        "instruction.sme.inline_asm.zero_za",
        "instruction.sme.inline_asm.fmopa_f32",
        "instruction.sme.inline_asm.st1w_za0h_s32",
        "instruction.sme.inline_asm.mova_za0h_to_z",
        "instruction.sme.inline_asm.fmla_f32_predicated",
    ):
        record = record_by_id(records, record_id)
        if record:
            lines.extend(render_compact_entry(record))
    return "\n".join(lines)


def render_rules_page(rule_records: list[dict[str, object]]) -> str:
    """Render the correctness checklist page."""

    sections = {
        "NEON": [
            record for record in rule_records if record["id"] in {"rule.neon.explicit-epilogue", "rule.cross-isa.no-mixed-templates"}
        ],
        "SVE": [
            record for record in rule_records
            if record["id"]
            in {
                "rule.sve.length-agnostic",
                "rule.sve.predicated-load-store",
                "rule.sve.header-required",
                "rule.cross-isa.no-mixed-templates",
            }
        ],
        "SME": [
            record for record in rule_records
            if record["id"]
            in {
                "rule.sme.streaming-required",
                "rule.sme.za-ownership",
                "rule.sme.zero-za-fresh-za",
                "rule.sme.inline-asm-standalone-za",
                "rule.sme.no-abi-runtime-stubs",
                "rule.sve.header-required",
                "rule.cross-isa.no-mixed-templates",
            }
        ],
    }

    lines = [
        "# ARM Vectorization Correctness Checklist",
        "",
        "<!-- Generated by scripts/generate_arm_intrinsics_manual.py. Do not edit manually. -->",
        "",
        "这是 `validate-snippet` 的 quick reference 版本，便于在生成代码前先做人工 checklist。",
        "",
        "## What This Checklist Covers",
        "",
        "- `NEON`: 固定宽度循环、显式尾处理、禁止跨 ISA 混用",
        "- `SVE`: 长度无关、谓词覆盖、头文件与 predicated load/store",
        "- `SME`: streaming、ZA ownership、`svzero_za()` 和 ZA/tile 入口约束",
        "",
    ]

    for section_title, records in sections.items():
        lines.append(f"## {section_title}")
        lines.append("")
        rows = [
            [
                f"`{record['display_name']}`",
                first_or_empty(record["correctness_rules"]),
                first_or_empty(record["anti_patterns"]),
            ]
            for record in records
        ]
        lines.extend(render_table(["Rule", "What It Checks", "Common Failure"], rows))
    lines.extend(["## Rule Reference", ""])
    for section_title, records in sections.items():
        lines.append(f"### {section_title}")
        lines.append("")
        for record in records:
            lines.extend(render_compact_entry(record))
    return "\n".join(lines)


def render_readme(
    index_payload: dict[str, object],
    page_to_records: dict[str, list[dict[str, object]]],
    rule_records: list[dict[str, object]],
) -> str:
    """Render the manual root README in quick-reference style."""

    total_count = sum(len(records) for records in page_to_records.values()) + len(rule_records)
    lines = [
        "# ARM Vectorization Intrinsics Manual",
        "",
        "<!-- Generated by scripts/generate_arm_intrinsics_manual.py. Do not edit manually. -->",
        "",
        "一个面向 `apply-vectorization` 的 ARM 向量化 quick-reference 手册，覆盖 `NEON`、`SVE`、`SME`，以及当前向量化相关的 `SVE2`、`SME2` 常用条目。",
        "",
        "## What This Manual Does",
        "",
        "1. **Quick Reference by ISA**: 直接给出各 ISA 的常用向量化条目和代码模式",
        "2. **Correctness Checklist**: 汇总 `validate-snippet` 的静态规则",
        "3. **Official Source Mapping**: 保留官方来源和 feature gate 信息",
        "4. **Code Generation Support**: 为 `apply-vectorization` 提供可直接查阅的 load/store/arithmetic/ZA inline asm 模式",
        "",
        "## Key Features",
        "",
        f"- **{total_count} 条 curated 记录**：仅保留与向量化直接相关的常用条目",
        f"- **{len(page_to_records['neon'])} 条 NEON 记录**：固定宽度主体循环、广播、加法、FMA",
        f"- **{len(page_to_records['sve'])} 条 SVE / SVE2 记录**：长度无关、谓词、predicated load/store、dot-product",
        f"- **{len(page_to_records['sme'])} 条 SME / SME2 记录**：streaming、ZA ownership、ZA 初始化、inline asm 外积与写回",
        f"- **{len(rule_records)} 条规则**：供 `validate-snippet` 和人工检查共同复用",
        "",
        "## Directory Structure",
        "",
        "```text",
        "arm-intrinsics-manual/",
        "├── README.md",
        "├── neon.md",
        "├── sve.md",
        "├── sme.md",
        "└── correctness-rules.md",
        "```",
        "",
        "## Usage Examples",
        "",
        "### Query an Intrinsic",
        "",
    ]
    lines.extend(
        render_code_block(
            "bash",
            "python3 ../scripts/query_arm_intrinsics.py lookup --name vld1q_f32",
        )
    )
    lines.extend(
        [
            "### Query an Instruction Family",
            "",
        ]
    )
    lines.extend(
        render_code_block(
            "bash",
            "python3 ../scripts/query_arm_intrinsics.py lookup --instruction FMOPA --isa sme",
        )
    )
    lines.extend(
        [
            "### Validate a Candidate Snippet",
            "",
        ]
    )
    lines.extend(
        render_code_block(
            "bash",
            "python3 ../scripts/query_arm_intrinsics.py validate-snippet --file ./candidate.c --isa sve --json",
        )
    )
    lines.extend(
        [
            "## Page Overview",
            "",
        ]
    )
    lines.extend(
        render_table(
            ["Page", "Focus", "Coverage"],
            [
                ["[NEON](./neon.md)", PAGE_DESCRIPTIONS["neon"], f"{len(page_to_records['neon'])} 条记录"],
                ["[SVE / SVE2](./sve.md)", PAGE_DESCRIPTIONS["sve"], f"{len(page_to_records['sve'])} 条记录"],
                ["[SME / SME2](./sme.md)", PAGE_DESCRIPTIONS["sme"], f"{len(page_to_records['sme'])} 条记录"],
                ["[Correctness Rules](./correctness-rules.md)", "静态规则与失败模式", f"{len(rule_records)} 条规则"],
            ],
        )
    )
    lines.extend(
        [
            "## How It Works",
            "",
            "1. `refresh_arm_intrinsics_db.py` 抓取官方 `ACLE` 和 `Neon Intrinsics Reference`，并对 `developer.arm.com` SME 指令页做可选抓取 / reference-only 记录",
            "2. 结构化快照写入 `references/arm_intrinsics_db/`",
            "3. `generate_arm_intrinsics_manual.py` 将快照生成为 quick-reference 手册",
            "4. `query_arm_intrinsics.py` 与 `validate-snippet` 直接消费同一份快照",
            "",
            "## Data Source",
            "",
            f"- `retrieved_at`: `{index_payload['retrieved_at']}`",
            f"- `schema_version`: `{index_payload['schema_version']}`",
            "",
        ]
    )
    for source in index_payload["sources"]:
        lines.append(
            f"- [{source['title']}]({source['url']}): {source['fetch_mode']} / {source['retrieved_at']}"
        )
    lines.extend(
        [
            "",
            "## Requirements",
            "",
            "- Python 3 用于查询与生成脚本",
            "- `refresh_arm_intrinsics_db.py` 需要 `requests` 和 `beautifulsoup4`",
            "- `query_arm_intrinsics.py` 和生成后的手册在运行时不依赖网络",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    """Generate the quick-reference manual pages."""

    args = parse_args()
    index_payload = load_index(args.db_dir)
    records = load_records(args.db_dir)
    rules = [record for record in records if record["kind"] == "rule"]
    page_to_records = {
        "neon": page_records(records, "neon"),
        "sve": page_records(records, "sve"),
        "sme": page_records(records, "sme"),
    }
    page_to_rules = {
        "neon": page_rules(rules, "neon"),
        "sve": page_rules(rules, "sve"),
        "sme": page_rules(rules, "sme"),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_text(args.output_dir / "README.md", render_readme(index_payload, page_to_records, rules))
    write_text(args.output_dir / "neon.md", render_neon_page(page_to_records["neon"], page_to_rules["neon"]))
    write_text(args.output_dir / "sve.md", render_sve_page(page_to_records["sve"], page_to_rules["sve"]))
    write_text(args.output_dir / "sme.md", render_sme_page(page_to_records["sme"], page_to_rules["sme"]))
    write_text(args.output_dir / "correctness-rules.md", render_rules_page(rules))
    print(f"[完成] 已生成 Arm intrinsics 手册 -> {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
