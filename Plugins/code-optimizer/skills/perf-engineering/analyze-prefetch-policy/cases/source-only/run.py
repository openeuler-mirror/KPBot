#!/usr/bin/env python3
"""Run the tier A verifier self-test and write the standard case result."""

import json
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent


def run_verifier():
    result = {
        "success": False,
        "mode": "verifier-self-test",
        "case": "source-only",
        "tier": "A",
        "check_scope": "automated_structure_and_fixture_checks_only",
        "requires_manual_review": True,
        "verifier_exit_code": None,
        "errors": [],
    }
    try:
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(HERE / "verify.py"), "--self-test"],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        result["verifier_exit_code"] = process.returncode
        payload = json.loads(process.stdout)
        if not isinstance(payload, dict):
            raise ValueError("verifier output must be a JSON object")
        result.update(payload)
        rejected = payload.get("negative_reports_rejected", [])
        result["negative_reports_rejected_count"] = len(rejected) if isinstance(rejected, list) else 0
        result["success"] = (
            process.returncode == 0 and payload.get("success") is True
            and payload.get("mode") == "verifier-self-test"
            and payload.get("requires_manual_review") is True
            and payload.get("check_scope") == "automated_structure_and_fixture_checks_only"
            and isinstance(payload.get("positive_reports"), int)
            and payload["positive_reports"] > 0
            and result["negative_reports_rejected_count"] > 0
        )
        if not result["success"] and not result.get("errors"):
            result["errors"] = ["verifier self-test did not satisfy the tier A case contract"]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result["success"] = False
        result["errors"] = [str(exc)]
    return result


def main():
    result = run_verifier()
    destination = HERE / "results" / "result.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Case result written to the case directory: results/result.json")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
