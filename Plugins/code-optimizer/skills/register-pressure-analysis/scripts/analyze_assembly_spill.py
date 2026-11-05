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
    language: str
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


def rust_mangle_prefix(func: str) -> str | None:
    """Return the legacy Rust mangled-symbol prefix for a source path name."""

    if "::" not in func:
        return func if func.startswith("_ZN") else None
    segments = func.split("::")
    if not all(segments):
        return None
    return "_ZN" + "".join(f"{len(segment)}{segment}" for segment in segments)


# Rust legacy mangling: _ZN<seg><seg>...17h<16-hex-hash>E. The hash changes on
# every recompile, so matching works on the hash-stripped symbol. The two
# helpers below mirror mca_analyze.py semantics; they are copied on purpose
# (each skill stays self-contained, no cross-skill imports).
RUST_HASH_RE: Final[re.Pattern[str]] = re.compile(r"17h[0-9a-f]{16}E$")


def strip_rust_hash(sym: str) -> str:
    """Drop a trailing 17h<hash>E suffix; return the input unchanged otherwise."""

    return RUST_HASH_RE.sub("", sym or "")


def demangle_legacy_segments(sym: str) -> list[str]:
    """Split a legacy mangled symbol (after _ZN, hash stripped) into segments.

    "_ZN6kernel4calc3dot" -> ["kernel", "calc", "dot"]. Returns [] for
    non-_ZN shapes (plain C labels) and for corrupted segment lengths.
    """

    s = strip_rust_hash(sym or "")
    if not s.startswith("_ZN"):
        return []
    s = s[3:]
    segments: list[str] = []
    while s:
        m = re.match(r"(\d+)", s)
        if not m:
            return []
        n = int(m.group(1))
        s = s[m.end():]
        if n <= 0 or len(s) < n:
            return []
        segments.append(s[:n])
        s = s[n:]
    return segments


def rust_label_matches(stripped_label: str, function: str) -> bool:
    """Check a label against a Rust source path name (legacy mangled symbols)."""

    label = stripped_label[:-1] if stripped_label.endswith(":") else stripped_label
    candidates = {label, f"_{label}"}
    if function in candidates or f"_{function}" in candidates:
        return True
    mangle_prefix = rust_mangle_prefix(function)
    if mangle_prefix is not None:
        pattern = rf"^{re.escape(mangle_prefix)}(17h[0-9a-f]+E)?$"
        if any(re.fullmatch(pattern, candidate) for candidate in candidates):
            return True
    # Segment-level tail alignment: tolerate an outer crate-name segment the
    # user did not type. A lib crate compiled from kernel.rs produces labels
    # like _ZN6kernel11microkernel7fma_4x417h<hash>E, while --function
    # microkernel::fma_4x4 builds the shorter prefix _ZN11microkernel7fma_4x4
    # that can never equal the whole label. Candidate segments matching the
    # tail of the label segments is a hit. Plain C labels yield no segments,
    # so the C-label matching path is unchanged.
    label_segments = demangle_legacy_segments(label)
    candidate_segments = demangle_legacy_segments(mangle_prefix) if mangle_prefix else []
    if candidate_segments and len(candidate_segments) <= len(label_segments):
        if label_segments[-len(candidate_segments):] == candidate_segments:
            return True
    return False


# Rust legacy mangled symbol shape, used to derive the artifact's language and
# source kind from the assembly text itself (PR #95 review-2 fix: these output
# fields must describe the analyzed artifact, not a caller's self-report).
RUST_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b_ZN(?:\d+[A-Za-z0-9_.$]+)+17h[0-9a-f]{16}E\b")
# gcc/clang/rustc wrap inline-asm blocks in "# APP" ... "# NO_APP" markers.
APP_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*#\s*APP\s*$")
NO_APP_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*#\s*NO_APP\s*$")
# C-side source-kind labels with no textual signature of their own: they cannot
# be told apart from plain assembly by text inspection, so as expectations they
# are only family-checked (accepted on rust-free artifacts, rejected on rust asm).
C_SIDE_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"c_intrinsics", "inline_asm"})


def derive_language_and_source_kind(lines: list[str]) -> tuple[str, str]:
    """Derive (language, source_kind) from the analyzed assembly text.

    language: any Rust legacy mangled symbol (_ZN...17h<16-hex>E) -> "rust",
    otherwise "c_cpp". source_kind: Rust symbols plus a "# APP" / "# NO_APP"
    wrapped inline-asm section -> "rust_asm"; Rust symbols without such a
    section -> "rust_intrinsics"; no Rust symbols -> "assembly" (C asm()
    blocks also carry APP markers, so APP alone must not imply rust_asm).
    """

    has_rust_symbol = any(RUST_SYMBOL_RE.search(line) for line in lines)
    has_app_section = (any(APP_LINE_RE.match(line) for line in lines)
                       and any(NO_APP_LINE_RE.match(line) for line in lines))
    if has_rust_symbol and has_app_section:
        source_kind = "rust_asm"
    elif has_rust_symbol:
        source_kind = "rust_intrinsics"
    else:
        source_kind = "assembly"
    language = "rust" if has_rust_symbol else "c_cpp"
    return language, source_kind


def label_matches(line: str, function: str) -> bool:
    """Check if a line defines the requested function label."""

    stripped = strip_comment(line)
    candidates = {function, f"_{function}"}
    if any(re.fullmatch(rf"{re.escape(candidate)}:", stripped) for candidate in candidates):
        return True
    return rust_label_matches(stripped, function)


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
        # No hit: list the global labels we saw (capped at 10, plus a count)
        # so the caller can self-correct, e.g. spot a missing crate-name
        # segment in the mangled candidates.
        available: list[str] = []
        for _, line in numbered:
            if looks_like_global_label(line):
                label = strip_comment(line)[:-1]
                if label and label not in available:
                    available.append(label)
        if len(available) > 10:
            shown = ", ".join(available[:10]) + f", ... (+{len(available) - 10} more)"
        else:
            shown = ", ".join(available)
        detail = f"; available labels: [{shown}]" if available else ""
        return [], f"function label not found: {function}{detail}"

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
    asm_path: Path,
    function: str | None,
    source_kind: str,
    max_evidence: int,
    language: str = "c_cpp",
) -> RegisterPressureResult:
    """Analyze the requested assembly file."""

    if not asm_path.exists():
        return RegisterPressureResult(
            success=False,
            asm_file=str(asm_path),
            function=function,
            source_kind=source_kind,
            language=language,
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
            language=language,
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
            language=language,
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
        language=language,
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
        choices=("c_intrinsics", "rust_intrinsics", "rust_asm", "inline_asm", "assembly"),
        default=None,
        help="Expected source kind of the analyzed artifact. When provided it is "
             "verified against the kind derived from the assembly text; a mismatch "
             "exits with an error naming both values. Omit to use the derived value.",
    )
    parser.add_argument(
        "--language",
        choices=("c_cpp", "rust"),
        default=None,
        help="Expected source language of the analyzed artifact. Verified against "
             "the value derived from the assembly text; a mismatch exits with an "
             "error naming both values. Omit to use the derived value.",
    )
    parser.add_argument("--max-evidence", type=int, default=20)
    return parser.parse_args()


def _expectation_failure(
    asm_path: Path,
    flag: str,
    expected: str,
    derived: str,
    language: str,
    source_kind: str,
) -> RegisterPressureResult:
    """Build the fail-closed result for a CLI expectation vs derivation mismatch."""

    return RegisterPressureResult(
        success=False,
        asm_file=str(asm_path),
        function=None,
        source_kind=source_kind,
        language=language,
        spill_store_count=0,
        spill_reload_count=0,
        stack_access_count=0,
        fma_count=0,
        spill_per_fma=0.0,
        pressure_level="unknown",
        evidence=[],
        recommendation=recommendation_for_level("unknown"),
        error_message=(
            "%s expects '%s' but the analyzed assembly derives '%s' "
            "(expected=%s, derived=%s); omit the flag to use the derived value"
            % (flag, expected, derived, expected, derived)
        ),
    )


def main() -> int:
    """Run the CLI."""

    args = parse_args()
    if args.max_evidence < 0:
        raise SystemExit("--max-evidence must be non-negative")
    asm_path = Path(args.asm).expanduser()

    # --language / --source-kind are expectations verified against values derived
    # from the analyzed assembly text (PR #95 review-2 fix). A mismatch is
    # fail-closed; omitted flags use the derived values in the output fields.
    language = args.language or "c_cpp"
    source_kind = args.source_kind or "assembly"
    language_source = "cli_verified" if args.language else "derived"
    source_kind_source = "cli_verified" if args.source_kind else "derived"
    try:
        asm_text = asm_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        asm_text = None  # analyze_assembly() emits the structured failure below
    if asm_text is not None:
        derived_language, derived_kind = derive_language_and_source_kind(asm_text.splitlines())
        for flag, expected, derived in (
            ("--language", args.language, derived_language),
            ("--source-kind", args.source_kind, derived_kind),
        ):
            if expected is None or expected == derived:
                continue
            # c_intrinsics/inline_asm carry no textual signature: accepted on
            # C-family (rust-free) artifacts, rejected on rust artifacts.
            if (flag == "--source-kind" and expected in C_SIDE_SOURCE_KINDS
                    and derived == "assembly"):
                continue
            result = _expectation_failure(
                asm_path, flag, expected, derived, derived_language, derived_kind)
            payload = {"register_pressure_result": asdict(result)}
            payload["register_pressure_result"]["language_source"] = language_source
            payload["register_pressure_result"]["source_kind_source"] = source_kind_source
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        language = derived_language
        source_kind = args.source_kind if args.source_kind else derived_kind

    result = analyze_assembly(asm_path, args.function, source_kind, args.max_evidence, language)
    payload = {"register_pressure_result": asdict(result)}
    payload["register_pressure_result"]["language_source"] = language_source
    payload["register_pressure_result"]["source_kind_source"] = source_kind_source
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
