"""Validate a stopped request session and summarize comparable wall intervals."""

import argparse
import json
import sys


def nanoseconds(value):
    if type(value) is not int or value < 0:
        raise ValueError("Timestamps must be nonnegative integer nanoseconds")
    return value


def union_duration(intervals):
    if not intervals:
        return 0
    ordered = sorted(intervals)
    start, end = ordered[0]
    total = 0
    for next_start, next_end in ordered[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return total + end - start


def analyze(payload):
    if not isinstance(payload, dict):
        raise ValueError("Session must be an object")
    for field in ("session_id", "clock_id"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"Missing {field}")
    if (payload.get("stopped") is not True
            or type(payload.get("active_spans")) is not int
            or payload["active_spans"] != 0):
        raise ValueError("Session is active or has unfinished spans")
    start = nanoseconds(payload.get("start_ns"))
    end = nanoseconds(payload.get("end_ns"))
    if end <= start:
        raise ValueError("Invalid session window")
    spans = payload.get("spans")
    if not isinstance(spans, list):
        raise ValueError("Spans must be a list")
    stages = {}
    identifiers = set()
    intervals = []
    for span in spans:
        if not isinstance(span, dict):
            raise ValueError("Span must be an object")
        identifier = span.get("span_id")
        stage = span.get("stage")
        if (not isinstance(identifier, str) or not identifier.strip()
                or identifier in identifiers):
            raise ValueError("Missing or duplicate span ID")
        identifiers.add(identifier)
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("Missing stage")
        if (span.get("session_id") != payload["session_id"]
                or span.get("clock_id") != payload["clock_id"]):
            raise ValueError("Mixed request sessions or clocks")
        span_start = nanoseconds(span.get("start_ns"))
        span_end = nanoseconds(span.get("end_ns"))
        if not start <= span_start < span_end <= end:
            raise ValueError("Span is incomplete or outside its request window")
        stages.setdefault(stage, []).append((span_start, span_end))
        intervals.append((span_start, span_end))
    duration = end - start
    totals = {stage: sum(b - a for a, b in rows) for stage, rows in stages.items()}
    stage_overlap = {stage: total != union_duration(stages[stage])
                     for stage, total in totals.items()}
    combined = union_duration(intervals)
    return {"session_id": payload["session_id"], "query_ns": duration,
            "capture_status": "captured" if intervals else "no_target_spans",
            "fraction_kind": "wall_interval_union",
            "overlap_observed": sum(totals.values()) > combined,
            "stage_ns": totals,
            "stage_overlap": stage_overlap,
            "stage_fraction": {stage: None if stage_overlap[stage] else total / duration
                               for stage, total in totals.items()},
            "combined_wall_union_ns": combined,
            "combined_wall_union_fraction": combined / duration if intervals else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8-sig") as stream:
            result = analyze(json.load(stream))
        print(json.dumps(result, allow_nan=False))
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
