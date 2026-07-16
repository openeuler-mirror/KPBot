#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_seconds(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"invalid duration seconds: {value}") from exc


def duration_text(seconds):
    if seconds is None:
        return ""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def main():
    parser = argparse.ArgumentParser(description="Append structured timing records for optimization workflow stages.")
    parser.add_argument("--file", required=True, help="Timing JSONL file to append")
    parser.add_argument("--stage", required=True, help="Workflow stage or optimization item name")
    parser.add_argument("--skill-name", default="", help="Optional subskill name")
    parser.add_argument("--round-name", default="", help="Optional optimization round name")
    parser.add_argument("--analysis-seconds", default="", help="Analysis duration in seconds")
    parser.add_argument("--implementation-seconds", default="", help="Implementation duration in seconds")
    parser.add_argument("--validation-seconds", default="", help="Validation duration in seconds")
    parser.add_argument("--total-seconds", default="", help="Total duration in seconds; computed when omitted")
    parser.add_argument("--status", default="recorded", help="Record status")
    parser.add_argument("--evidence-path", default="", help="Evidence path associated with this timing record")
    parser.add_argument("--note", default="", help="Optional note")
    args = parser.parse_args()

    analysis = parse_seconds(args.analysis_seconds)
    implementation = parse_seconds(args.implementation_seconds)
    validation = parse_seconds(args.validation_seconds)
    total = parse_seconds(args.total_seconds)
    if total is None:
        total = sum(value or 0.0 for value in [analysis, implementation, validation])

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": args.stage,
        "skill_name": args.skill_name,
        "round_name": args.round_name,
        "status": args.status,
        "analysis_duration": duration_text(analysis),
        "implementation_duration": duration_text(implementation),
        "validation_duration": duration_text(validation),
        "total_duration": duration_text(total),
        "analysis_seconds": analysis,
        "implementation_seconds": implementation,
        "validation_seconds": validation,
        "total_seconds": total,
        "evidence_path": args.evidence_path,
        "note": args.note,
    }

    output = Path(args.file)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
