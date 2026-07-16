#!/usr/bin/env python3
"""Estimate register budgets for compute-intensive ARM micro-kernels."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from register_allocation_model import (  # noqa: E402
    DEFAULT_VECTOR_BITS,
    DTYPE_BITS,
    KernelDescriptor,
    effective_max_spill_risk,
    estimate_candidate,
    normalize_addressing_mode,
    normalize_codegen_style,
    normalize_dtype,
    normalize_kernel_kind,
    parse_shape,
)


@dataclass(frozen=True)
class RegisterBudget:
    """Structured register-budget summary for a proposed micro-kernel."""

    isa: str
    dtype: str
    shape: str
    m_tiles: int
    n_tiles: int
    vector_bits: int
    lanes_per_vector: int
    total_vector_registers: int
    reserved_vector_registers: int
    available_vector_registers: int
    accumulator_registers: int
    load_registers: int
    temporary_registers: int
    needed_vector_registers: int
    available_general_registers: int
    needed_general_registers: int
    pressure_ratio: float
    spill_risk: str
    recommendation: str
    register_class_budgets: dict[str, dict[str, Any]]
    eligible: bool
    rejection_reasons: list[str]
    verification_required: bool
    verification_actions: list[str]


def estimate_budget(
    *,
    isa: str,
    dtype: str,
    shape: str,
    vector_bits: int,
    reserve_vector_regs: int | None,
    reserve_general_regs: int,
    extra_temporaries: int,
    codegen_style: str = "intrinsics",
    kernel_kind: str = "microkernel",
    accumulator_dtype: str | None = None,
    k_unroll: int = 1,
    has_tail: bool = False,
    has_beta: bool = False,
    has_bias: bool = False,
    addressing_mode: str = "contiguous",
    uses_gather_scatter: bool = False,
    uses_za_tile: bool = False,
    max_spill_risk: str | None = None,
    sme_abi_verified: bool = False,
) -> RegisterBudget:
    """Estimate register pressure for a single tile shape."""

    codegen_style = normalize_codegen_style(codegen_style)
    dtype = normalize_dtype(dtype)
    accumulator_dtype = normalize_dtype(accumulator_dtype or dtype)
    kernel_kind = normalize_kernel_kind(kernel_kind)
    addressing_mode = normalize_addressing_mode(addressing_mode)
    effective_risk, risk_explicit = effective_max_spill_risk(
        codegen_style=codegen_style,
        requested_max_spill_risk=max_spill_risk,
    )
    descriptor = KernelDescriptor(
        isa=isa,
        codegen_style=codegen_style,
        kernel_kind=kernel_kind,
        dtype=dtype,
        accumulator_dtype=accumulator_dtype,
        vector_bits=vector_bits,
        shape_candidates=(shape,),
        k_unroll=k_unroll,
        has_tail=has_tail,
        has_beta=has_beta,
        has_bias=has_bias,
        addressing_mode=addressing_mode,
        uses_gather_scatter=uses_gather_scatter,
        uses_za_tile=uses_za_tile,
        reserve_vector_regs=reserve_vector_regs,
        reserve_general_regs=reserve_general_regs,
        extra_temporaries=extra_temporaries,
        max_spill_risk=effective_risk,
        max_spill_risk_explicit=risk_explicit,
        sme_abi_verified=sme_abi_verified,
    )
    candidate = estimate_candidate(descriptor, shape)
    return RegisterBudget(
        isa=candidate.isa,
        dtype=candidate.dtype,
        shape=candidate.shape,
        m_tiles=candidate.m_tiles,
        n_tiles=candidate.n_tiles,
        vector_bits=candidate.vector_bits,
        lanes_per_vector=candidate.lanes_per_vector,
        total_vector_registers=candidate.total_vector_registers,
        reserved_vector_registers=candidate.reserved_vector_registers,
        available_vector_registers=candidate.available_vector_registers,
        accumulator_registers=candidate.accumulator_registers,
        load_registers=candidate.load_registers,
        temporary_registers=candidate.temporary_registers,
        needed_vector_registers=candidate.needed_vector_registers,
        available_general_registers=candidate.available_general_registers,
        needed_general_registers=candidate.needed_general_registers,
        pressure_ratio=candidate.pressure_ratio,
        spill_risk=candidate.spill_risk,
        recommendation=candidate.recommendation,
        register_class_budgets={
            name: asdict(budget)
            for name, budget in candidate.register_class_budgets.items()
        },
        eligible=candidate.eligible,
        rejection_reasons=list(candidate.rejection_reasons),
        verification_required=candidate.verification_required,
        verification_actions=list(candidate.verification_actions),
    )


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Estimate register budget and spill risk for an ARM micro-kernel tile."
    )
    parser.add_argument("--isa", choices=("neon", "sve", "sme"), required=True)
    parser.add_argument("--dtype", type=normalize_dtype, required=True)
    parser.add_argument("--shape", required=True, help="Tile shape in MxN form, for example 8x4.")
    parser.add_argument(
        "--accumulator-dtype",
        type=normalize_dtype,
        help="Accumulator dtype. Defaults to --dtype.",
    )
    parser.add_argument(
        "--codegen-style",
        type=normalize_codegen_style,
        default="intrinsics",
        choices=("intrinsics", "inline_asm", "assembly"),
        help="Codegen path used by the candidate. Default: intrinsics.",
    )
    parser.add_argument(
        "--kernel-kind",
        type=normalize_kernel_kind,
        default="microkernel",
        help="Kernel family used for GPR and temporary estimates.",
    )
    parser.add_argument(
        "--vector-bits",
        type=int,
        help="Vector width in bits. Defaults to 128 for NEON and 256 for SVE/SME estimates.",
    )
    parser.add_argument(
        "--reserve-vector-regs",
        type=int,
        help="Override vector registers reserved for ABI, loop control, predicates, and constants.",
    )
    parser.add_argument(
        "--reserve-general-regs",
        type=int,
        default=13,
        help="General registers reserved for arguments, bases, loop counters, and ABI.",
    )
    parser.add_argument(
        "--extra-temporaries",
        type=int,
        default=0,
        help="Additional vector temporaries required by address transforms or epilogue code.",
    )
    parser.add_argument("--k-unroll", type=int, default=1, help="Inner K/tap unroll factor.")
    parser.add_argument("--has-tail", action="store_true", help="Candidate includes tail masking.")
    parser.add_argument("--has-beta", action="store_true", help="Candidate fuses beta*C.")
    parser.add_argument("--has-bias", action="store_true", help="Candidate fuses bias loads.")
    parser.add_argument(
        "--addressing-mode",
        type=normalize_addressing_mode,
        default="contiguous",
        help="Addressing mode: contiguous, strided, indirect, or gather_scatter.",
    )
    parser.add_argument(
        "--uses-gather-scatter",
        action="store_true",
        help="Candidate uses gather/scatter style memory operations.",
    )
    parser.add_argument("--uses-za-tile", action="store_true", help="Candidate uses SME ZA tiles.")
    parser.add_argument(
        "--sme-abi-verified",
        action="store_true",
        help="Target link environment has verified SME ABI support routines.",
    )
    parser.add_argument(
        "--max-spill-risk",
        choices=("low", "medium", "high"),
        help="Highest selectable spill risk. Defaults depend on --codegen-style.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    return parser.parse_args()


def format_text(budget: RegisterBudget) -> str:
    """Format a human-readable report."""

    rows = [
        ("isa", budget.isa),
        ("dtype", budget.dtype),
        ("microkernel_shape", budget.shape),
        ("vector_bits", budget.vector_bits),
        ("lanes_per_vector", budget.lanes_per_vector),
        ("available_vector_registers", budget.available_vector_registers),
        ("accumulator_registers", budget.accumulator_registers),
        ("load_registers", budget.load_registers),
        ("temporary_registers", budget.temporary_registers),
        ("needed_vector_registers", budget.needed_vector_registers),
        ("available_general_registers", budget.available_general_registers),
        ("needed_general_registers", budget.needed_general_registers),
        ("pressure_ratio", budget.pressure_ratio),
        ("spill_risk", budget.spill_risk),
        ("eligible", budget.eligible),
        ("verification_required", budget.verification_required),
        ("recommendation", budget.recommendation),
    ]
    return "\n".join(f"{key}: {value}" for key, value in rows)


def main() -> int:
    """Run the CLI."""

    args = parse_args()
    vector_bits = args.vector_bits or DEFAULT_VECTOR_BITS[args.isa]
    if vector_bits <= 0 or vector_bits % 64 != 0:
        raise SystemExit("--vector-bits must be a positive multiple of 64")
    if args.reserve_vector_regs is not None and not 0 <= args.reserve_vector_regs < 32:
        raise SystemExit("--reserve-vector-regs must be in [0, 31]")
    if not 0 <= args.reserve_general_regs < 31:
        raise SystemExit("--reserve-general-regs must be in [0, 30]")
    if args.extra_temporaries < 0:
        raise SystemExit("--extra-temporaries must be non-negative")
    if args.k_unroll <= 0:
        raise SystemExit("--k-unroll must be positive")

    parse_shape(args.shape)
    budget = estimate_budget(
        isa=args.isa,
        dtype=args.dtype,
        shape=args.shape,
        vector_bits=vector_bits,
        reserve_vector_regs=args.reserve_vector_regs,
        reserve_general_regs=args.reserve_general_regs,
        extra_temporaries=args.extra_temporaries,
        codegen_style=args.codegen_style,
        kernel_kind=args.kernel_kind,
        accumulator_dtype=args.accumulator_dtype,
        k_unroll=args.k_unroll,
        has_tail=args.has_tail,
        has_beta=args.has_beta,
        has_bias=args.has_bias,
        addressing_mode=args.addressing_mode,
        uses_gather_scatter=args.uses_gather_scatter,
        uses_za_tile=args.uses_za_tile,
        max_spill_risk=args.max_spill_risk,
        sme_abi_verified=args.sme_abi_verified,
    )
    payload = {"register_budget": asdict(budget)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_text(budget))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
