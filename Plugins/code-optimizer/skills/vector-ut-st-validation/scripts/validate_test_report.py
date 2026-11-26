#!/usr/bin/env python3
"""Validate the hard gates of a vector UT/ST JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED = {
    "schema_version", "subject", "baseline_commit", "candidate_commit", "feature_switch",
    "platform", "build", "suites", "risk_checks", "raw_artifacts", "verdict",
}
RISK_CHECKS = {"boundary", "concurrency", "fallback", "feature_off"}


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["root must be an object"]
    errors: list[str] = []
    missing = REQUIRED - set(data)
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
    if set(data) - REQUIRED:
        errors.append(f"unknown fields: {sorted(set(data) - REQUIRED)}")
    if data.get("schema_version") != "vector-test-report.v1":
        errors.append("schema_version must be vector-test-report.v1")
    for field in ("subject", "baseline_commit", "candidate_commit"):
        if not nonempty(data.get(field)):
            errors.append(f"{field} must be non-empty")

    feature = data.get("feature_switch")
    if not isinstance(feature, dict) or not nonempty(feature.get("name")):
        errors.append("feature_switch.name must be non-empty")
    else:
        if set(feature) - {"name", "off_tested", "on_tested"}:
            errors.append("feature_switch contains unknown fields")
        if feature.get("off_tested") is not True or feature.get("on_tested") is not True:
            errors.append("feature-off and feature-on must both be tested")

    platform = data.get("platform")
    if not isinstance(platform, dict):
        errors.append("platform must be an object")
    else:
        for field in ("arch", "cpu", "os"):
            if not nonempty(platform.get(field)):
                errors.append(f"platform.{field} must be non-empty")
        if platform.get("target_hardware_tested") is not True:
            errors.append("target hardware system test is required")

    build = data.get("build")
    if not isinstance(build, dict):
        errors.append("build must be an object")
    else:
        for field in ("compiler", "flags"):
            if not nonempty(build.get(field)):
                errors.append(f"build.{field} must be non-empty")

    ready_possible = True
    suites = data.get("suites")
    if not isinstance(suites, dict):
        errors.append("suites must be an object")
        ready_possible = False
    else:
        for name in ("ut", "st"):
            suite = suites.get(name)
            if not isinstance(suite, dict):
                errors.append(f"suites.{name} must be an object")
                ready_possible = False
                continue
            values = [suite.get(field) for field in ("executed", "passed", "failed", "skipped")]
            if not all(type(value) is int and value >= 0 for value in values):
                errors.append(f"suites.{name} counters must be non-negative integers")
                ready_possible = False
                continue
            executed, passed, failed, skipped = values
            if executed < 1 or executed != passed + failed + skipped:
                errors.append(f"suites.{name} executed must equal passed+failed+skipped and be positive")
                ready_possible = False
            if failed or skipped:
                ready_possible = False

    checks = data.get("risk_checks")
    if not isinstance(checks, dict):
        errors.append("risk_checks must be an object")
        ready_possible = False
    else:
        for name in sorted(RISK_CHECKS) + [key for key in checks if key not in RISK_CHECKS]:
            check = checks.get(name)
            if not isinstance(check, dict):
                errors.append(f"risk_checks.{name} is required")
                ready_possible = False
                continue
            status = check.get("status")
            evidence = check.get("evidence")
            rationale = check.get("rationale")
            if status not in ("pass", "fail", "not_applicable"):
                errors.append(f"risk_checks.{name}.status is invalid")
                ready_possible = False
            if not isinstance(evidence, list) or not all(nonempty(item) for item in evidence):
                errors.append(f"risk_checks.{name}.evidence must be a string array")
            if not isinstance(rationale, str):
                errors.append(f"risk_checks.{name}.rationale must be a string")
            if status == "pass" and not evidence:
                errors.append(f"risk_checks.{name} pass requires evidence")
                ready_possible = False
            if status == "not_applicable" and (not nonempty(rationale) or len(rationale.strip()) < 10):
                errors.append(f"risk_checks.{name} not_applicable requires a reviewable rationale")
                ready_possible = False
            if status == "fail":
                ready_possible = False

    artifacts = data.get("raw_artifacts")
    if not isinstance(artifacts, list) or not artifacts or not all(nonempty(item) for item in artifacts):
        errors.append("raw_artifacts must be a non-empty string array")
        ready_possible = False
    verdict = data.get("verdict")
    if verdict not in ("READY", "REWORK", "BLOCKED"):
        errors.append("verdict must be READY, REWORK, or BLOCKED")
    if verdict == "READY" and not ready_possible:
        errors.append("verdict READY conflicts with failed, skipped, or missing gates")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    errors = validate(data)
    if errors:
        print("\n".join(f"ERROR: {item}" for item in errors), file=sys.stderr)
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
