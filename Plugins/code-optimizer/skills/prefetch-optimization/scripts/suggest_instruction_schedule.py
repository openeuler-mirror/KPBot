#!/usr/bin/env python3
"""Suggest a simple load/FMA interleaving schedule for ARM64 kernels."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final


LOAD_PREFIXES: Final[tuple[str, ...]] = ("ldr", "ldp", "ld1", "ld2", "ld3", "ld4")
FMA_PREFIXES: Final[tuple[str, ...]] = (
    "fmla",
    "fmls",
    "fmadd",
    "fmsub",
    "fmopa",
    "bfdot",
    "bfmmla",
    "sdot",
    "udot",
    "usdot",
    "smmla",
    "ummla",
    "usmmla",
)


@dataclass(frozen=True)
class ScheduleSuggestion:
    """Instruction scheduling suggestion."""

    original_instruction_count: int
    load_count: int
    fma_count: int
    max_consecutive_loads_before: int
    max_consecutive_loads_after: int
    interleaving_applied: bool
    suggested_sequence: list[str]
    notes: list[str]


def strip_comment(line: str) -> str:
    """Strip common assembly comments."""

    for marker in ("//", ";"):
        if marker in line:
            line = line.split(marker, 1)[0]
    return line.strip()


def parse_mnemonic(line: str) -> str:
    """Extract a lower-case instruction mnemonic."""

    stripped = strip_comment(line)
    match = re.match(r"([A-Za-z][A-Za-z0-9_.]*)\b", stripped)
    if match is None:
        return ""
    return match.group(1).split(".", 1)[0].lower()


def classify(line: str) -> str:
    """Classify a line as load, fma, or other."""

    mnemonic = parse_mnemonic(line)
    if mnemonic.startswith(LOAD_PREFIXES):
        return "load"
    if mnemonic.startswith(FMA_PREFIXES):
        return "fma"
    return "other"


def max_consecutive(items: list[str], target: str) -> int:
    """Return the longest consecutive run of a classified instruction type."""

    best = 0
    current = 0
    for item in items:
        if item == target:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def parse_sequence(args: argparse.Namespace) -> list[str]:
    """Read an instruction sequence from CLI arguments."""

    if args.file:
        try:
            raw_text = Path(args.file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"failed to read sequence file: {exc}") from exc
    else:
        raw_text = args.sequence or ""
    parts = re.split(r"[;\n]", raw_text)
    return [strip_comment(part) for part in parts if strip_comment(part)]


def suggest_schedule(instructions: list[str], max_load_run: int) -> ScheduleSuggestion:
    """Interleave loads with FMA instructions using a conservative heuristic."""

    classes = [classify(instruction) for instruction in instructions]
    loads = [instruction for instruction in instructions if classify(instruction) == "load"]
    fmas = [instruction for instruction in instructions if classify(instruction) == "fma"]
    others = [instruction for instruction in instructions if classify(instruction) == "other"]

    suggested: list[str] = []
    load_index = 0
    fma_index = 0
    while load_index < len(loads) or fma_index < len(fmas):
        emitted_loads = 0
        while load_index < len(loads) and emitted_loads < max_load_run:
            suggested.append(loads[load_index])
            load_index += 1
            emitted_loads += 1
        if fma_index < len(fmas):
            suggested.append(fmas[fma_index])
            fma_index += 1
        elif load_index < len(loads):
            continue

    suggested.extend(others)
    suggested_classes = [classify(instruction) for instruction in suggested]
    max_before = max_consecutive(classes, "load")
    max_after = max_consecutive(suggested_classes, "load")
    interleaving_applied = suggested != instructions

    notes = [
        "Keep dependent FMA chains independent; this script does not prove register dependencies.",
        "Run register-pressure-analysis after changing tile shape or unroll depth.",
    ]
    if len(fmas) == 0:
        notes.append("No FMA-like instruction was found; scheduling advice is limited.")
    if max_after > max_load_run:
        notes.append("Load clustering remains above the requested threshold.")

    return ScheduleSuggestion(
        original_instruction_count=len(instructions),
        load_count=len(loads),
        fma_count=len(fmas),
        max_consecutive_loads_before=max_before,
        max_consecutive_loads_after=max_after,
        interleaving_applied=interleaving_applied,
        suggested_sequence=suggested,
        notes=notes,
    )


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Suggest a simple load/FMA interleaving schedule for an instruction sequence."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sequence", help="Instruction sequence separated by semicolons or newlines.")
    group.add_argument("--file", help="File containing one instruction per line.")
    parser.add_argument("--max-load-run", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    return parser.parse_args()


def main() -> int:
    """Run the CLI."""

    args = parse_args()
    if args.max_load_run <= 0:
        raise SystemExit("--max-load-run must be positive")
    instructions = parse_sequence(args)
    if not instructions:
        raise SystemExit("instruction sequence is empty")

    suggestion = suggest_schedule(instructions, args.max_load_run)
    payload = {"schedule_suggestion": asdict(suggestion)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"load_count: {suggestion.load_count}")
        print(f"fma_count: {suggestion.fma_count}")
        print(f"max_consecutive_loads_before: {suggestion.max_consecutive_loads_before}")
        print(f"max_consecutive_loads_after: {suggestion.max_consecutive_loads_after}")
        print("suggested_sequence:")
        for instruction in suggestion.suggested_sequence:
            print(f"  {instruction}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
