#!/usr/bin/env python3
"""Verify a kernel-switch receipt and its local evidence without changing the target."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

from check_kernel import evidence_path


TERMINAL_PHASE = {"build": "built", "trial": "verified", "runtime": "verified", "restore": "restored"}
PHASES = {"baseline", "prepared", "built", "staged", "requested", "verified", "restored"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def string_field(mapping, field):
    value = mapping.get(field)
    require(isinstance(value, str) and value.strip(), f"missing text field: {field}")
    return value


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def verify_boot(record, files, request):
    findings = record["findings"]
    target = record["target"]
    boot = json.loads(files["kernel"].read_text(encoding="utf-8"))
    require((boot["scope"], boot["status"], boot["run_id"], boot["target_id"]) ==
            ("boot", "matched", record["run_id"], target["id"]), "kernel evidence status, run or target mismatch")
    checks = boot["checks"]
    release = string_field(findings, "release")
    require(checks["kernel_release"] == {"expected": release, "actual": release}, "kernel release mismatch")
    require(all(c["actual"] == c["expected"] for c in checks.values()), "kernel observation mismatch")
    current_id = uuid.UUID(boot["boot_id"])
    if target["layer"] == "host":
        string_field(findings, "previous_release")
    if target["layer"] == "host" or "previous_boot_id" in boot:
        require(checks["new_boot"]["actual"] is True and uuid.UUID(boot["previous_boot_id"]) != current_id,
                "expected a new boot")
    if findings.get("previous_release") == release or "build_id" in findings or "build_id" in request:
        identity = request if "build_id" in request else findings
        build_id = string_field(identity, "build_id").lower()
        require(checks.get("build_id") == {"expected": build_id, "actual": build_id}, "kernel build ID mismatch")
        if "build_id" in findings:
            require(string_field(findings, "build_id").lower() == build_id, "reported kernel build ID mismatch")
    next_boot = string_field(findings, "next_boot")
    require(files["next_boot"].read_text(encoding="utf-8").strip() == next_boot,
            "next boot target differs from its readback")


def verify(args):
    root = args.report.parent.resolve(strict=True)
    record = json.loads(evidence_path(root, args.report.name).read_text(encoding="utf-8"))
    require(record["contract"] == "agentenv-kernel-switch/v1", "unsupported result contract")
    string_field(record, "run_id")
    target = record["target"]
    string_field(target, "id")
    require(target["layer"] in ("host", "guest"), "invalid target layer")
    goal, phase = record["goal"], record["phase"]
    require(goal in TERMINAL_PHASE and phase in PHASES, "invalid goal or phase")
    request = {}
    if args.request:
        request_file = evidence_path(args.request.parent.resolve(strict=True), args.request.name)
        request = json.loads(request_file.read_text(encoding="utf-8"))
        require(request["contract"] == "agentenv-kernel-switch-request/v1", "unsupported request contract")
        for field in ("run_id", "goal", "target"):
            require(request[field] == record[field], f"result differs from task {field}")
    probes = request.get("probes", [])
    require(isinstance(probes, list) and all(isinstance(p, str) and p.strip() for p in probes),
            "expected named probes in the task")
    require(isinstance(record["findings"], dict), "expected findings object")
    missing = record["required_evidence"]
    require(isinstance(missing, list) and all(isinstance(s, str) and s.strip() for s in missing),
            "expected a list of evidence gaps")
    files = {}
    for name, relative in record["evidence_sources"].items():
        path = evidence_path(root, relative)
        require(path.is_file(), f"missing evidence file: {relative}")
        files[name] = path
    if record["status"] == "blocked":
        require(missing, "a blocked result must name the missing evidence or failed check")
        return record
    require(record["status"] == "completed" and not missing and phase == TERMINAL_PHASE[goal],
            "completion status, phase or evidence gaps conflict with the goal")
    findings = record["findings"]
    if "release" in request:
        require(findings.get("release") == string_field(request, "release"), "release differs from task expectation")
    if goal == "build":
        require(files["kernelrelease"].read_text().strip() == string_field(findings, "release"),
                "built release mismatch")
        actual_sha256 = digest(files["artifact"])
        require(files["artifact"].stat().st_size and actual_sha256 == string_field(findings, "artifact_sha256").lower(),
                "built artifact digest mismatch or empty artifact")
        if "artifact_sha256" in request:
            require(actual_sha256 == string_field(request, "artifact_sha256").lower(),
                    "built artifact differs from task expectation")
    elif goal in ("trial", "restore"):
        verify_boot(record, files, request)
    elif goal == "runtime":
        for field in ("previous_value", "trial_value", "effective_value"):
            string_field(findings, field)

    required = {"runtime": {"mechanism", "workload"}, "build": set()}.get(goal, {"service", "workload"})
    required.update(probes)
    if goal in ("trial", "restore") and target["layer"] == "guest":
        required.add("cold_start")
    codes = findings.get("probe_exit_codes", {})
    require(isinstance(codes, dict) and required.issubset(codes), f"required probes: {sorted(required)}")
    for name, code in codes.items():
        require(type(code) is int and code == 0 and name in files, f"failed probe or missing log: {name}")
        require(files[name].stat().st_size > 0, f"empty probe evidence: {name}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="switch-result.json in the run evidence directory")
    parser.add_argument("--request", type=Path, help="original versioned task with expected target, goal and probes")
    args = parser.parse_args()
    result = {"contract": "agentenv-kernel-switch-verification/v1"}
    try:
        record = verify(args)
        for field in ("run_id", "target", "goal", "phase", "status", "required_evidence"):
            result[field] = record[field]
        code = int(record["status"] == "blocked")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
        result.update(status="invalid", error=str(error))
        code = 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
