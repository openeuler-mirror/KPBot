#!/usr/bin/env python3
"""Analyze AArch64 assembly for stack spill/reload pressure."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final


STORE_MNEMONICS: Final[set[str]] = {
    "str",
    "stur",
    "stp",
    "stnp",
    "st1",
    "st2",
    "st3",
    "st4",
}
LOAD_MNEMONICS: Final[set[str]] = {
    "ldr",
    "ldur",
    "ldp",
    "ldnp",
    "ld1",
    "ld2",
    "ld3",
    "ld4",
}
FMA_MNEMONIC_PREFIXES: Final[tuple[str, ...]] = (
    "fmla",
    "fmls",
    "fmadd",
    "fmsub",
    "fnmadd",
    "fnmsub",
    "fmopa",
    "fmops",
    "bfmmla",
    "bfdot",
    "sdot",
    "udot",
    "usdot",
    "smmla",
    "ummla",
    "usmmla",
)


@dataclass(frozen=True)
class Evidence:
    """A single assembly evidence line."""

    line: int
    kind: str
    instruction: str


@dataclass(frozen=True)
class RegisterPressureResult:
    """Structured register-pressure result."""

    success: bool
    asm_file: str
    function: str | None
    source_kind: str
    spill_store_count: int
    spill_reload_count: int
    stack_access_count: int
    fma_count: int
    spill_per_fma: float
    pressure_level: str
    evidence: list[Evidence]
    recommendation: str
    error_message: str


def strip_comment(line: str) -> str:
    """Remove common assembly comments while preserving instruction text."""

    for marker in ("//", ";"):
        if marker in line:
            line = line.split(marker, 1)[0]
    return line.strip()


def parse_mnemonic(line: str) -> str | None:
    """Extract the mnemonic from a single assembly line."""

    stripped = strip_comment(line)
    if not stripped or stripped.endswith(":") or stripped.startswith("."):
        return None
    match = re.match(r"([A-Za-z][A-Za-z0-9_.]*)\b", stripped)
    if match is None:
        return None
    return match.group(1).split(".", 1)[0].lower()


def is_stack_access(line: str, mnemonics: set[str]) -> bool:
    """Return true when line is a stack-based load/store instruction."""

    mnemonic = parse_mnemonic(line)
    if mnemonic not in mnemonics:
        return False
    return re.search(r"\[\s*sp(?:\s*,|\s*\])", strip_comment(line), re.IGNORECASE) is not None


def is_fma(line: str) -> bool:
    """Return true when line looks like an FMA or matrix multiply-accumulate instruction."""

    mnemonic = parse_mnemonic(line)
    if mnemonic is None:
        return False
    return mnemonic.startswith(FMA_MNEMONIC_PREFIXES)


def label_matches(line: str, function: str) -> bool:
    """Check if a line defines the requested function label."""

    stripped = strip_comment(line)
    candidates = {function, f"_{function}"}
    return any(re.fullmatch(rf"{re.escape(candidate)}:", stripped) for candidate in candidates)


def looks_like_global_label(line: str) -> bool:
    """Heuristically detect the next non-local function label."""

    stripped = strip_comment(line)
    if not stripped.endswith(":"):
        return False
    label = stripped[:-1]
    return bool(label) and not label.startswith((".", "L"))


def select_lines(lines: list[str], function: str | None) -> tuple[list[tuple[int, str]], str | None]:
    """Select the function range if a function name is provided."""

    numbered = list(enumerate(lines, start=1))
    if function is None:
        return numbered, None

    start_index: int | None = None
    for index, (_, line) in enumerate(numbered):
        if label_matches(line, function):
            start_index = index
            break
    if start_index is None:
        return [], f"function label not found: {function}"

    selected: list[tuple[int, str]] = []
    for line_number, line in numbered[start_index:]:
        if selected and looks_like_global_label(line):
            break
        selected.append((line_number, line))
    return selected, None


def classify_pressure(stack_access_count: int, fma_count: int, spill_per_fma: float) -> str:
    """Classify pressure level."""

    if stack_access_count == 0:
        return "none"
    if fma_count == 0:
        return "high" if stack_access_count < 8 else "severe"
    if spill_per_fma < 0.05:
        return "low"
    if spill_per_fma < 0.20:
        return "medium"
    if spill_per_fma < 0.50:
        return "high"
    return "severe"


def recommendation_for_level(level: str) -> str:
    """Return a short recommendation for a pressure level."""

    recommendations = {
        "none": "No stack spill/reload pattern found in the analyzed range.",
        "low": "Stack traffic is low; verify it is only prologue/epilogue or ABI save/restore.",
        "medium": "Inspect the hot loop and benchmark impact; consider reducing temporaries.",
        "high": "Register pressure is high; reduce tile shape, unroll factor, or live ranges.",
        "severe": "Spill density is severe; redesign the micro-kernel before trusting performance.",
        "unknown": "Assembly could not be analyzed.",
    }
    return recommendations[level]


def analyze_assembly(
    asm_path: Path, function: str | None, source_kind: str, max_evidence: int
) -> RegisterPressureResult:
    """Analyze the requested assembly file."""

    if not asm_path.exists():
        return RegisterPressureResult(
            success=False,
            asm_file=str(asm_path),
            function=function,
            source_kind=source_kind,
            spill_store_count=0,
            spill_reload_count=0,
            stack_access_count=0,
            fma_count=0,
            spill_per_fma=0.0,
            pressure_level="unknown",
            evidence=[],
            recommendation=recommendation_for_level("unknown"),
            error_message=f"assembly file does not exist: {asm_path}",
        )

    try:
        lines = asm_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        return RegisterPressureResult(
            success=False,
            asm_file=str(asm_path),
            function=function,
            source_kind=source_kind,
            spill_store_count=0,
            spill_reload_count=0,
            stack_access_count=0,
            fma_count=0,
            spill_per_fma=0.0,
            pressure_level="unknown",
            evidence=[],
            recommendation=recommendation_for_level("unknown"),
            error_message=f"failed to read assembly as UTF-8: {exc}",
        )

    selected_lines, error_message = select_lines(lines, function)
    if error_message is not None:
        return RegisterPressureResult(
            success=False,
            asm_file=str(asm_path),
            function=function,
            source_kind=source_kind,
            spill_store_count=0,
            spill_reload_count=0,
            stack_access_count=0,
            fma_count=0,
            spill_per_fma=0.0,
            pressure_level="unknown",
            evidence=[],
            recommendation=recommendation_for_level("unknown"),
            error_message=error_message,
        )

    spill_store_count = 0
    spill_reload_count = 0
    fma_count = 0
    evidence: list[Evidence] = []

    for line_number, line in selected_lines:
        clean_line = strip_comment(line)
        if not clean_line:
            continue
        if is_stack_access(line, STORE_MNEMONICS):
            spill_store_count += 1
            if len(evidence) < max_evidence:
                evidence.append(Evidence(line_number, "spill_store", clean_line))
        elif is_stack_access(line, LOAD_MNEMONICS):
            spill_reload_count += 1
            if len(evidence) < max_evidence:
                evidence.append(Evidence(line_number, "spill_reload", clean_line))
        if is_fma(line):
            fma_count += 1

    stack_access_count = spill_store_count + spill_reload_count
    spill_per_fma = round(stack_access_count / max(fma_count, 1), 4)
    pressure_level = classify_pressure(stack_access_count, fma_count, spill_per_fma)

    return RegisterPressureResult(
        success=True,
        asm_file=str(asm_path),
        function=function,
        source_kind=source_kind,
        spill_store_count=spill_store_count,
        spill_reload_count=spill_reload_count,
        stack_access_count=stack_access_count,
        fma_count=fma_count,
        spill_per_fma=spill_per_fma,
        pressure_level=pressure_level,
        evidence=evidence,
        recommendation=recommendation_for_level(pressure_level),
        error_message="",
    )


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="Analyze AArch64 assembly spill/reload pressure.")
    parser.add_argument("--asm", required=True, help="Assembly file to analyze.")
    parser.add_argument("--function", help="Optional function label to isolate.")
    parser.add_argument(
        "--source-kind",
        choices=("c_intrinsics", "inline_asm", "assembly"),
        default="assembly",
    )
    parser.add_argument("--max-evidence", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    """Run the CLI."""

    args = parse_args()
    if args.max_evidence < 0:
        raise SystemExit("--max-evidence must be non-negative")
    result = analyze_assembly(
        Path(args.asm).expanduser(),
        args.function,
        args.source_kind,
        args.max_evidence,
    )
    payload = {"register_pressure_result": asdict(result)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
