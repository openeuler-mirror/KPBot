#!/usr/bin/env python3
"""Select a verified register allocation for ARM compute micro-kernels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from register_allocation_model import (  # noqa: E402
    DEFAULT_VECTOR_BITS,
    KernelDescriptor,
    default_n_dimension,
    effective_max_spill_risk,
    normalize_addressing_mode,
    normalize_codegen_style,
    normalize_dtype,
    normalize_kernel_kind,
    parse_int_candidates,
    parse_positive_int,
    parse_shape,
    parse_shape_candidates,
    select_allocation,
)


DEFAULT_M_CANDIDATES: Final[tuple[int, ...]] = (4, 8, 12, 16, 20, 24)


def parse_m_candidates(raw_value: str) -> tuple[int, ...]:
    """Parse comma-separated positive M tile candidates."""

    return parse_int_candidates(raw_value, argument_name="m-candidates")


def parse_n_candidates(raw_value: str) -> tuple[int, ...]:
    """Parse comma-separated positive N tile candidates."""

    return parse_int_candidates(raw_value, argument_name="n-candidates")


def build_shape_candidates(args: argparse.Namespace, *, vector_bits: int, dtype: str) -> tuple[str, ...]:
    """Build the shape search space while preserving the legacy MxN path."""

    if args.shape_candidates:
        return args.shape_candidates

    if args.n_candidates:
        n_candidates = args.n_candidates
    else:
        n_value = (
            args.n
            if args.n is not None
            else default_n_dimension(dtype=dtype, vector_bits=vector_bits)
        )
        n_candidates = (n_value,)

    shapes = tuple(
        f"{m_candidate}x{n_candidate}"
        for m_candidate in args.m_candidates
        for n_candidate in n_candidates
    )
    for shape in shapes:
        parse_shape(shape)
    return shapes


def build_descriptor(args: argparse.Namespace) -> KernelDescriptor:
    """Build a register-allocation descriptor from CLI arguments."""

    codegen_style = normalize_codegen_style(args.codegen_style)
    dtype = normalize_dtype(args.dtype)
    accumulator_dtype = normalize_dtype(args.accumulator_dtype or dtype)
    kernel_kind = normalize_kernel_kind(args.kernel_kind)
    addressing_mode = normalize_addressing_mode(args.addressing_mode)
    vector_bits = args.vector_bits or DEFAULT_VECTOR_BITS[args.isa]
    if vector_bits <= 0 or vector_bits % 64 != 0:
        raise ValueError("--vector-bits must be a positive multiple of 64")
    if args.reserve_vector_regs is not None and not 0 <= args.reserve_vector_regs < 32:
        raise ValueError("--reserve-vector-regs must be in [0, 31]")
    if not 0 <= args.reserve_general_regs < 31:
        raise ValueError("--reserve-general-regs must be in [0, 30]")
    if args.extra_temporaries < 0:
        raise ValueError("--extra-temporaries must be non-negative")
    if args.k_unroll <= 0:
        raise ValueError("--k-unroll must be positive")

    effective_risk, risk_explicit = effective_max_spill_risk(
        codegen_style=codegen_style,
        requested_max_spill_risk=args.max_spill_risk,
    )
    shape_candidates = build_shape_candidates(args, vector_bits=vector_bits, dtype=dtype)
    return KernelDescriptor(
        isa=args.isa,
        codegen_style=codegen_style,
        kernel_kind=kernel_kind,
        dtype=dtype,
        accumulator_dtype=accumulator_dtype,
        vector_bits=vector_bits,
        shape_candidates=shape_candidates,
        k_unroll=args.k_unroll,
        has_tail=args.has_tail,
        has_beta=args.has_beta,
        has_bias=args.has_bias,
        addressing_mode=addressing_mode,
        uses_gather_scatter=args.uses_gather_scatter,
        uses_za_tile=args.uses_za_tile,
        reserve_vector_regs=args.reserve_vector_regs,
        reserve_general_regs=args.reserve_general_regs,
        extra_temporaries=args.extra_temporaries,
        max_spill_risk=effective_risk,
        max_spill_risk_explicit=risk_explicit,
        sme_abi_verified=args.sme_abi_verified,
    )


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Select an ARM micro-kernel tile with register-class budgets."
    )
    parser.add_argument("--isa", choices=("neon", "sve", "sme"), required=True)
    parser.add_argument("--dtype", type=normalize_dtype, required=True)
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
        "--n",
        type=parse_positive_int,
        help="Output columns for the tile. Defaults to one vector for the dtype.",
    )
    parser.add_argument(
        "--m-candidates",
        type=parse_m_candidates,
        default=DEFAULT_M_CANDIDATES,
        help="Comma-separated M tile candidates. Default: 4,8,12,16,20,24.",
    )
    parser.add_argument(
        "--n-candidates",
        type=parse_n_candidates,
        help="Comma-separated N tile candidates. Overrides --n when set.",
    )
    parser.add_argument(
        "--shape-candidates",
        type=parse_shape_candidates,
        help="Comma-separated MxN candidates. Overrides --m-candidates/--n-candidates.",
    )
    parser.add_argument(
        "--vector-bits",
        type=parse_positive_int,
        help="Vector width in bits. Defaults to 128 for NEON and 256 for SVE/SME.",
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
        help="Highest selectable spill risk. Defaults to medium for intrinsics and high otherwise.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    return parser.parse_args()


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    """Build the register allocation plan payload."""

    descriptor = build_descriptor(args)
    decision = select_allocation(descriptor)
    return decision.to_payload()


def format_text(payload: dict[str, object]) -> str:
    """Format a human-readable allocation summary."""

    plan = payload["register_allocation_plan"]
    selected = payload["selected_register_allocation"]
    if not isinstance(plan, dict):
        raise TypeError("register_allocation_plan must be a dict")
    if selected is None:
        rows = [
            ("strategy", plan["strategy"]),
            ("isa", plan["isa"]),
            ("codegen_style", plan["codegen_style"]),
            ("success", plan["success"]),
            ("error_message", plan["error_message"]),
        ]
        return "\n".join(f"{key}: {value}" for key, value in rows)
    if not isinstance(selected, dict):
        raise TypeError("selected_register_allocation must be a dict")
    rows = [
        ("strategy", plan["strategy"]),
        ("isa", plan["isa"]),
        ("codegen_style", plan["codegen_style"]),
        ("dtype", plan["dtype"]),
        ("selected_shape", selected["shape"]),
        ("accumulator_registers", selected["accumulator_registers"]),
        ("needed_vector_registers", selected["needed_vector_registers"]),
        ("pressure_ratio", selected["pressure_ratio"]),
        ("spill_risk", selected["spill_risk"]),
        ("score", selected["score"]),
        ("fallback_count", len(payload["fallback_register_allocations"])),
        ("verification_required", payload["verification_required"]),
    ]
    return "\n".join(f"{key}: {value}" for key, value in rows)


def main() -> int:
    """Run the CLI."""

    args = parse_args()
    try:
        payload = build_plan(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
