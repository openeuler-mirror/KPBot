#!/usr/bin/env python3
"""Write an unexecuted ABBA collection plan; never starts a benchmark or service."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--base-artifact", required=True)
    parser.add_argument("--candidate-artifact", required=True)
    parser.add_argument("--metric", choices=("summary.total_time_ms", "summary.total_time_s"), required=True)
    parser.add_argument("--minimum-reduction-pct", type=float, default=2)
    args = parser.parse_args()
    if not 0 <= args.minimum_reduction_pct < 100:
        parser.error("minimum reduction must be in [0, 100)")
    if not all(value.strip() for value in (args.template, args.environment, args.base_artifact, args.candidate_artifact)):
        parser.error("identity fields cannot be empty")
    plan = {
        "schema_version": 1, "metric": args.metric,
        "minimum_reduction_pct": args.minimum_reduction_pct,
        "workload": {"concurrency": 100, "total": 100, "mode": "create-only", "template": args.template},
        "environment_id": args.environment,
        "variants": {name: {"artifact_id": artifact, "correctness_passed": False}
                     for name, artifact in (("base", args.base_artifact), ("candidate", args.candidate_artifact))},
        "runs": [{"block": block, "position": position, "variant": variant,
                  "environment_id": args.environment,
                  "reports": [f"reports/b{block:02d}-p{position}-{variant}-r{rep}.json" for rep in range(5)]}
                 for block in range(12)
                 for position, variant in enumerate(("base", "candidate", "candidate", "base"))],
    }
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except OSError as exc:
        parser.error(str(exc))
    print(f"Wrote plan: {args.output}; collect reports and verify correctness before analysis.")


if __name__ == "__main__":
    main()
