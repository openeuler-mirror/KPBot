#!/usr/bin/env python3
"""Shared register-allocation model for ARM vector micro-kernels."""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Final


DTYPE_BITS: Final[dict[str, int]] = {
    "float64": 64,
    "double": 64,
    "float32": 32,
    "fp32": 32,
    "float16": 16,
    "fp16": 16,
    "bfloat16": 16,
    "bf16": 16,
    "int64": 64,
    "uint64": 64,
    "int32": 32,
    "uint32": 32,
    "int16": 16,
    "uint16": 16,
    "int8": 8,
    "uint8": 8,
}

DEFAULT_VECTOR_BITS: Final[dict[str, int]] = {
    "neon": 128,
    "sve": 256,
    "sme": 256,
}

ISA_RESERVED_VECTOR_REGS: Final[dict[str, int]] = {
    "neon": 4,
    "sve": 6,
    "sme": 8,
}

RISK_ORDER: Final[dict[str, int]] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "spill-likely": 3,
}

CODEGEN_STYLES: Final[tuple[str, ...]] = ("auto", "intrinsics", "inline_asm", "assembly")
KERNEL_KINDS: Final[tuple[str, ...]] = (
    "microkernel",
    "gemm",
    "convolution",
    "filter",
    "stencil",
    "reduction",
    "dot",
)
ADDRESSING_MODES: Final[tuple[str, ...]] = (
    "contiguous",
    "strided",
    "indirect",
    "gather_scatter",
)


@dataclass(frozen=True)
class RegisterClassBudget:
    """Budget for one architectural register class."""

    name: str
    total: int
    reserved: int
    available: int
    needed: int
    pressure_ratio: float
    spill_risk: str
    recommendation: str

    @property
    def exceeded(self) -> bool:
        """Return whether this class needs more registers than are available."""

        return self.needed > self.available


@dataclass(frozen=True)
class KernelDescriptor:
    """Inputs that affect register pressure before code generation."""

    isa: str
    codegen_style: str
    kernel_kind: str
    dtype: str
    accumulator_dtype: str
    vector_bits: int
    shape_candidates: tuple[str, ...]
    k_unroll: int = 1
    has_tail: bool = False
    has_beta: bool = False
    has_bias: bool = False
    addressing_mode: str = "contiguous"
    uses_gather_scatter: bool = False
    uses_za_tile: bool = False
    reserve_vector_regs: int | None = None
    reserve_general_regs: int = 13
    extra_temporaries: int = 0
    max_spill_risk: str = "medium"
    max_spill_risk_explicit: bool = False
    sme_abi_verified: bool = False


@dataclass(frozen=True)
class AllocationCandidate:
    """A scored register allocation candidate."""

    isa: str
    codegen_style: str
    kernel_kind: str
    dtype: str
    accumulator_dtype: str
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
    register_class_budgets: dict[str, RegisterClassBudget]
    score: float
    throughput_score: float
    pressure_headroom: float
    codegen_complexity: float
    eligible: bool
    rejection_reasons: tuple[str, ...]
    verification_required: bool
    verification_actions: tuple[str, ...]

    def to_dict(self, *, selected_accumulators: int | None = None) -> dict[str, Any]:
        """Return a JSON-ready representation while keeping legacy fields."""

        payload = asdict(self)
        payload["register_class_budgets"] = {
            name: asdict(budget) for name, budget in self.register_class_budgets.items()
        }
        payload["underutilization_risk"] = (
            self.eligible
            and selected_accumulators is not None
            and self.accumulator_registers < selected_accumulators
        )
        if not self.eligible:
            payload["selection_note"] = "rejected: " + "; ".join(self.rejection_reasons)
        elif payload["underutilization_risk"]:
            payload["selection_note"] = "not selected: lower throughput score than selected shape"
        elif selected_accumulators == self.accumulator_registers:
            payload["selection_note"] = "selected"
        else:
            payload["selection_note"] = "eligible"
        return payload


@dataclass(frozen=True)
class AllocationDecision:
    """Final decision payload for pre-codegen register allocation."""

    success: bool
    descriptor: KernelDescriptor
    candidates: tuple[AllocationCandidate, ...]
    selected: AllocationCandidate | None
    fallback: tuple[AllocationCandidate, ...]
    error_message: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical selection payload consumed by apply-vectorization."""

        selected_accumulators = (
            self.selected.accumulator_registers if self.selected is not None else None
        )
        candidate_entries = [
            candidate.to_dict(selected_accumulators=selected_accumulators)
            for candidate in self.candidates
        ]
        fallback_entries = [
            candidate.to_dict(selected_accumulators=selected_accumulators)
            for candidate in self.fallback
        ]
        if self.selected is None:
            return {
                "register_allocation_plan": {
                    "success": False,
                    "strategy": "maximize_verified_throughput_score",
                    "isa": self.descriptor.isa,
                    "codegen_style": self.descriptor.codegen_style,
                    "dtype": self.descriptor.dtype,
                    "vector_bits": self.descriptor.vector_bits,
                    "max_spill_risk": self.descriptor.max_spill_risk,
                    "selected_shape": None,
                    "selected_reason": "",
                    "verification_required": False,
                    "verification_actions": [],
                    "error_message": self.error_message,
                },
                "candidate_register_allocations": candidate_entries,
                "selected_register_allocation": None,
                "fallback_register_allocations": [],
                "underutilization_risk": False,
                "verification_required": False,
                "verification_actions": [],
            }

        selected_entry = self.selected.to_dict(selected_accumulators=selected_accumulators)
        return {
            "register_allocation_plan": {
                "success": True,
                "strategy": "maximize_verified_throughput_score",
                "isa": self.descriptor.isa,
                "codegen_style": self.descriptor.codegen_style,
                "kernel_kind": self.descriptor.kernel_kind,
                "dtype": self.descriptor.dtype,
                "accumulator_dtype": self.descriptor.accumulator_dtype,
                "vector_bits": self.descriptor.vector_bits,
                "max_spill_risk": self.descriptor.max_spill_risk,
                "selected_shape": self.selected.shape,
                "selected_reason": (
                    "highest eligible throughput score under register-class budgets"
                ),
                "verification_required": self.selected.verification_required,
                "verification_actions": list(self.selected.verification_actions),
            },
            "candidate_register_allocations": candidate_entries,
            "selected_register_allocation": selected_entry,
            "fallback_register_allocations": fallback_entries,
            "register_budget": selected_register_budget(self.selected),
            "spill_risk": self.selected.spill_risk,
            "underutilization_risk": False,
            "verification_required": self.selected.verification_required,
            "verification_actions": list(self.selected.verification_actions),
        }


def normalize_dtype(raw_dtype: str) -> str:
    """Return the canonical dtype key used by the estimator."""

    dtype = raw_dtype.lower()
    if dtype not in DTYPE_BITS:
        supported = ", ".join(sorted(DTYPE_BITS))
        raise argparse.ArgumentTypeError(f"unsupported dtype {raw_dtype!r}; supported: {supported}")
    if dtype == "fp32":
        return "float32"
    if dtype == "fp16":
        return "float16"
    if dtype == "bf16":
        return "bfloat16"
    if dtype == "double":
        return "float64"
    return dtype


def normalize_codegen_style(raw_style: str) -> str:
    """Normalize legacy codegen style names."""

    style = raw_style.lower()
    if style == "asm":
        return "inline_asm"
    if style == "auto":
        return "intrinsics"
    if style not in CODEGEN_STYLES:
        supported = ", ".join(CODEGEN_STYLES)
        raise argparse.ArgumentTypeError(f"unsupported codegen style {raw_style!r}: {supported}")
    return style


def normalize_kernel_kind(raw_kind: str) -> str:
    """Normalize a kernel-kind CLI value."""

    kind = raw_kind.lower()
    if kind not in KERNEL_KINDS:
        supported = ", ".join(KERNEL_KINDS)
        raise argparse.ArgumentTypeError(f"unsupported kernel kind {raw_kind!r}: {supported}")
    return kind


def normalize_addressing_mode(raw_mode: str) -> str:
    """Normalize an addressing mode CLI value."""

    mode = raw_mode.lower().replace("-", "_")
    if mode not in ADDRESSING_MODES:
        supported = ", ".join(ADDRESSING_MODES)
        raise argparse.ArgumentTypeError(f"unsupported addressing mode {raw_mode!r}: {supported}")
    return mode


def parse_shape(raw_shape: str) -> tuple[int, int]:
    """Parse a shape in MxN form."""

    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", raw_shape)
    if match is None:
        raise argparse.ArgumentTypeError("shape must use MxN form, for example 8x4")
    m_tiles = int(match.group(1))
    n_tiles = int(match.group(2))
    if m_tiles <= 0 or n_tiles <= 0:
        raise argparse.ArgumentTypeError("shape dimensions must be positive")
    return m_tiles, n_tiles


def parse_positive_int(raw_value: str) -> int:
    """Parse a strictly positive integer CLI value."""

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got {raw_value!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def parse_int_candidates(raw_value: str, *, argument_name: str) -> tuple[int, ...]:
    """Parse comma-separated positive integer candidates."""

    values: list[int] = []
    for part in raw_value.split(","):
        item = part.strip()
        if not item:
            raise argparse.ArgumentTypeError(f"{argument_name} must not contain empty entries")
        values.append(parse_positive_int(item))
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError(f"{argument_name} must not contain duplicates")
    return tuple(values)


def parse_shape_candidates(raw_value: str) -> tuple[str, ...]:
    """Parse comma-separated MxN shape candidates."""

    shapes: list[str] = []
    for part in raw_value.split(","):
        item = part.strip()
        if not item:
            raise argparse.ArgumentTypeError("shape-candidates must not contain empty entries")
        m_tiles, n_tiles = parse_shape(item)
        shapes.append(f"{m_tiles}x{n_tiles}")
    if len(set(shapes)) != len(shapes):
        raise argparse.ArgumentTypeError("shape-candidates must not contain duplicates")
    return tuple(shapes)


def default_n_dimension(*, dtype: str, vector_bits: int) -> int:
    """Return one vector-length worth of columns for the dtype."""

    dtype_bits = DTYPE_BITS[dtype]
    return max(1, vector_bits // dtype_bits)


def effective_max_spill_risk(
    *, codegen_style: str, requested_max_spill_risk: str | None
) -> tuple[str, bool]:
    """Return the effective risk threshold and whether the user set it explicitly."""

    if requested_max_spill_risk is not None:
        return requested_max_spill_risk, True
    if codegen_style == "intrinsics":
        return "medium", False
    return "high", False


def is_risk_allowed(spill_risk: str, max_spill_risk: str) -> bool:
    """Return whether a spill risk can be selected."""

    if spill_risk == "spill-likely":
        return False
    return RISK_ORDER[spill_risk] <= RISK_ORDER[max_spill_risk]


def classify_spill_risk(needed: int, available: int) -> tuple[str, str]:
    """Classify pressure from needed and available registers."""

    if needed == 0 and available == 0:
        return "low", "This register class is not used by the selected ISA path."
    if available <= 0:
        return "spill-likely", "No allocatable registers remain after reservations."
    ratio = needed / available
    if ratio <= 0.70:
        return "low", "Register budget has enough headroom for scheduling and epilogue code."
    if ratio <= 0.90:
        return "medium", "Budget is workable, but keep temporaries short-lived."
    if ratio <= 1.0:
        return "high", "Budget is tight; force assembly spill analysis before accepting."
    return "spill-likely", "Required registers exceed the budget."


def build_register_class_budget(
    *, name: str, total: int, reserved: int, needed: int
) -> RegisterClassBudget:
    """Build one register class budget."""

    available = max(0, total - reserved)
    if needed == 0 and available == 0:
        pressure_ratio = 0.0
    elif available > 0:
        pressure_ratio = round(needed / available, 3)
    else:
        pressure_ratio = 999.999
    spill_risk, recommendation = classify_spill_risk(needed, available)
    return RegisterClassBudget(
        name=name,
        total=total,
        reserved=reserved,
        available=available,
        needed=needed,
        pressure_ratio=pressure_ratio,
        spill_risk=spill_risk,
        recommendation=recommendation,
    )


def overall_risk(budgets: dict[str, RegisterClassBudget]) -> str:
    """Return the worst spill risk across register classes."""

    return max(budgets.values(), key=lambda budget: RISK_ORDER[budget.spill_risk]).spill_risk


def za_tile_capacity(accumulator_dtype: str) -> int:
    """Return a conservative ZA tile count for the accumulator element size."""

    bits = DTYPE_BITS[accumulator_dtype]
    if bits >= 64:
        return 2
    if bits >= 32:
        return 4
    if bits >= 16:
        return 8
    return 16


def estimate_general_registers(descriptor: KernelDescriptor) -> int:
    """Estimate GPR demand from addressing and kernel features."""

    needed = 5
    if descriptor.kernel_kind in {"gemm", "convolution", "filter", "stencil"}:
        needed += 2
    if descriptor.addressing_mode == "strided":
        needed += 2
    elif descriptor.addressing_mode in {"indirect", "gather_scatter"}:
        needed += 3
    if descriptor.uses_gather_scatter:
        needed += 2
    if descriptor.has_tail:
        needed += 1
    if descriptor.has_beta:
        needed += 1
    if descriptor.has_bias:
        needed += 1
    if descriptor.k_unroll > 1:
        needed += 1
    if descriptor.uses_za_tile:
        needed += 2
    return needed


def estimate_predicate_registers(descriptor: KernelDescriptor) -> int:
    """Estimate predicate register demand for SVE/SME paths."""

    if descriptor.isa == "neon":
        return 0
    needed = 1
    if descriptor.has_tail or descriptor.addressing_mode != "contiguous":
        needed += 1
    if descriptor.uses_gather_scatter:
        needed += 1
    if descriptor.uses_za_tile:
        needed += 1
    return needed


def estimate_candidate(descriptor: KernelDescriptor, shape: str) -> AllocationCandidate:
    """Estimate and score one register allocation candidate."""

    m_tiles, n_tiles = parse_shape(shape)
    dtype_bits = DTYPE_BITS[descriptor.dtype]
    accumulator_bits = DTYPE_BITS[descriptor.accumulator_dtype]
    lanes_per_vector = max(1, descriptor.vector_bits // dtype_bits)
    accumulator_lanes = max(1, descriptor.vector_bits // accumulator_bits)
    n_vectors = math.ceil(n_tiles / lanes_per_vector)
    logical_accumulators = m_tiles * n_vectors

    vector_accumulators = 0 if descriptor.uses_za_tile else logical_accumulators
    load_registers = n_vectors + 1
    if descriptor.k_unroll > 1:
        load_registers += min(2, descriptor.k_unroll - 1)
    temporary_registers = descriptor.extra_temporaries + max(2, min(4, n_vectors + 1))
    if descriptor.has_tail:
        temporary_registers += 1
    if descriptor.has_beta:
        temporary_registers += n_vectors
    if descriptor.has_bias:
        temporary_registers += 1
    if descriptor.addressing_mode == "strided":
        temporary_registers += 1
    elif descriptor.addressing_mode in {"indirect", "gather_scatter"}:
        temporary_registers += 2
    if descriptor.uses_gather_scatter:
        temporary_registers += 1

    needed_vector_registers = vector_accumulators + load_registers + temporary_registers
    reserved_vector_registers = (
        descriptor.reserve_vector_regs
        if descriptor.reserve_vector_regs is not None
        else ISA_RESERVED_VECTOR_REGS[descriptor.isa]
    )
    vector_budget = build_register_class_budget(
        name="vector",
        total=32,
        reserved=reserved_vector_registers,
        needed=needed_vector_registers,
    )

    predicate_total = 16 if descriptor.isa in {"sve", "sme"} else 0
    predicate_reserved = 3 if descriptor.isa == "sme" else 2 if descriptor.isa == "sve" else 0
    predicate_budget = build_register_class_budget(
        name="predicate",
        total=predicate_total,
        reserved=predicate_reserved,
        needed=estimate_predicate_registers(descriptor),
    )

    gpr_budget = build_register_class_budget(
        name="gpr",
        total=31,
        reserved=descriptor.reserve_general_regs,
        needed=estimate_general_registers(descriptor),
    )

    za_needed = 0
    za_total = 0
    if descriptor.uses_za_tile:
        za_total = za_tile_capacity(descriptor.accumulator_dtype)
        za_needed = max(1, math.ceil(logical_accumulators / accumulator_lanes))
    za_budget = build_register_class_budget(
        name="za_tile", total=za_total, reserved=0, needed=za_needed
    )

    budgets = {
        "vector": vector_budget,
        "predicate": predicate_budget,
        "gpr": gpr_budget,
        "za_tile": za_budget,
    }
    spill_risk = overall_risk(budgets)
    rejection_reasons = eligibility_rejections(descriptor, budgets, spill_risk)
    eligible = not rejection_reasons
    verification_actions = build_verification_actions(descriptor, spill_risk)
    verification_required = bool(verification_actions)
    pressure_headroom = average_pressure_headroom(budgets)
    codegen_complexity = estimate_codegen_complexity(descriptor, n_vectors)
    throughput_score = float(logical_accumulators)
    score = (
        throughput_score * 100.0
        + pressure_headroom * 10.0
        - codegen_complexity
        if eligible
        else -1.0
    )

    return AllocationCandidate(
        isa=descriptor.isa,
        codegen_style=descriptor.codegen_style,
        kernel_kind=descriptor.kernel_kind,
        dtype=descriptor.dtype,
        accumulator_dtype=descriptor.accumulator_dtype,
        shape=f"{m_tiles}x{n_tiles}",
        m_tiles=m_tiles,
        n_tiles=n_tiles,
        vector_bits=descriptor.vector_bits,
        lanes_per_vector=lanes_per_vector,
        total_vector_registers=vector_budget.total,
        reserved_vector_registers=vector_budget.reserved,
        available_vector_registers=vector_budget.available,
        accumulator_registers=logical_accumulators,
        load_registers=load_registers,
        temporary_registers=temporary_registers,
        needed_vector_registers=needed_vector_registers,
        available_general_registers=gpr_budget.available,
        needed_general_registers=gpr_budget.needed,
        pressure_ratio=vector_budget.pressure_ratio,
        spill_risk=spill_risk,
        recommendation=recommendation_for_candidate(spill_risk, eligible),
        register_class_budgets=budgets,
        score=round(score, 3),
        throughput_score=throughput_score,
        pressure_headroom=round(pressure_headroom, 3),
        codegen_complexity=round(codegen_complexity, 3),
        eligible=eligible,
        rejection_reasons=tuple(rejection_reasons),
        verification_required=verification_required,
        verification_actions=tuple(verification_actions),
    )


def eligibility_rejections(
    descriptor: KernelDescriptor,
    budgets: dict[str, RegisterClassBudget],
    spill_risk: str,
) -> list[str]:
    """Return rejection reasons for a candidate."""

    reasons: list[str] = []
    for name, budget in budgets.items():
        if budget.exceeded:
            reasons.append(
                f"{name} budget exceeded: needed {budget.needed}, available {budget.available}"
            )
    if not is_risk_allowed(spill_risk, descriptor.max_spill_risk):
        reasons.append(f"spill_risk {spill_risk} exceeds {descriptor.max_spill_risk}")
    if (
        descriptor.codegen_style == "intrinsics"
        and spill_risk == "high"
        and not descriptor.max_spill_risk_explicit
    ):
        reasons.append("intrinsics candidates require explicit opt-in for high spill risk")
    if descriptor.uses_za_tile:
        if descriptor.isa != "sme":
            reasons.append("ZA tile allocation requires isa=sme")
        if (
            descriptor.codegen_style == "intrinsics"
            and not descriptor.sme_abi_verified
        ):
            reasons.append("SME ZA intrinsics require verified SME ABI runtime support")
    return reasons


def build_verification_actions(descriptor: KernelDescriptor, spill_risk: str) -> list[str]:
    """Build verification steps for the selected candidate."""

    actions: list[str] = []
    if spill_risk in {"medium", "high"}:
        actions.append("compile selected kernel to assembly with benchmark target flags")
        actions.append("run register-pressure-analysis and scan for stack spills")
    if descriptor.codegen_style in {"inline_asm", "assembly"}:
        actions.append("scan clobber list against used vector, predicate, and general registers")
    if descriptor.uses_za_tile:
        actions.append("scan SME ZA asm for smstart/smstop, zero {za}, fmopa, st1w, and za clobber")
        actions.append("scan linked object for unresolved __arm_tpidr2_* or __arm_za_disable")
    return actions


def average_pressure_headroom(budgets: dict[str, RegisterClassBudget]) -> float:
    """Return average headroom across used register classes."""

    used = [budget for budget in budgets.values() if budget.total > 0 or budget.needed > 0]
    if not used:
        return 1.0
    return sum(max(0.0, 1.0 - budget.pressure_ratio) for budget in used) / len(used)


def estimate_codegen_complexity(descriptor: KernelDescriptor, n_vectors: int) -> float:
    """Estimate complexity as a small tie-breaker after throughput and headroom."""

    complexity = float(n_vectors)
    if descriptor.codegen_style == "intrinsics":
        complexity += 1.0
    if descriptor.has_tail:
        complexity += 0.5
    if descriptor.has_beta:
        complexity += 0.5
    if descriptor.has_bias:
        complexity += 0.25
    if descriptor.addressing_mode != "contiguous":
        complexity += 1.0
    if descriptor.uses_gather_scatter:
        complexity += 1.0
    if descriptor.uses_za_tile:
        complexity += 1.5
    return complexity


def recommendation_for_candidate(spill_risk: str, eligible: bool) -> str:
    """Return a concise candidate recommendation."""

    if not eligible:
        return "Candidate is not selectable under the active register-allocation policy."
    if spill_risk == "low":
        return "Register budget has comfortable headroom."
    if spill_risk == "medium":
        return "Candidate is selectable, but verify generated assembly for spills."
    if spill_risk == "high":
        return "Candidate is tight and must pass assembly spill verification."
    return "Candidate is spill-likely and must not be selected."


def selected_register_budget(candidate: AllocationCandidate) -> dict[str, Any]:
    """Return the selected candidate in the vectorization_result register_budget shape."""

    return {
        "available_vector_registers": candidate.available_vector_registers,
        "needed_vector_registers": candidate.needed_vector_registers,
        "temporary_registers": candidate.temporary_registers,
        "spill_risk": candidate.spill_risk,
        "register_class_budgets": {
            name: asdict(budget)
            for name, budget in candidate.register_class_budgets.items()
        },
    }


def rank_candidates(candidates: list[AllocationCandidate]) -> list[AllocationCandidate]:
    """Return eligible candidates from best to most conservative fallback."""

    eligible = [candidate for candidate in candidates if candidate.eligible]
    return sorted(
        eligible,
        key=lambda candidate: (
            candidate.score,
            candidate.accumulator_registers,
            candidate.pressure_headroom,
            -candidate.codegen_complexity,
        ),
        reverse=True,
    )


def select_allocation(descriptor: KernelDescriptor) -> AllocationDecision:
    """Select the best register allocation and fallback chain."""

    candidates = [
        estimate_candidate(descriptor, shape) for shape in descriptor.shape_candidates
    ]
    ranked = rank_candidates(candidates)
    if not ranked:
        return AllocationDecision(
            success=False,
            descriptor=descriptor,
            candidates=tuple(candidates),
            selected=None,
            fallback=(),
            error_message="no register allocation candidate is eligible",
        )
    return AllocationDecision(
        success=True,
        descriptor=descriptor,
        candidates=tuple(candidates),
        selected=ranked[0],
        fallback=tuple(ranked[1:]),
    )
