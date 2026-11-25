#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_run_manifest.py -- record one EPLB experiment run for benefit analysis.

Ties together what was deployed (template hash + parameters + three-state groups)
and how it will be measured (dataset, concurrency, rounds). Emits
``eplb_run_manifest.json``; ``vllm-eplb-benefit-analysis`` consumes it together
with the raw measurements.

This script does not modify templates and does not run benchmarks.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

SCHEMA_VERSION = 1
DEFAULT_GROUPS = ("off", "dynamic", "force")


def parse_effective_params(path):
    """Read the values that will actually take effect from the template."""
    text = open(path, encoding="utf-8").read()
    def grab(key):
        m = re.search(r"--eplb-config\.%s\s+(\d+)" % re.escape(key), text)
        return int(m.group(1)) if m else None
    return {
        "redundant_experts": grab("num_redundant_experts"),
        "window_size": grab("window_size"),
        "step_interval": grab("step_interval"),
        "dynamic_eplb": bool(re.search(r"^\s*export\s+DYNAMIC_EPLB\s*=", text, re.M)),
    }


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Write an EPLB experiment run manifest")
    ap.add_argument("--out", required=True, help="output eplb_run_manifest.json")
    ap.add_argument("--template", required=True, help="P-node run_dp_template.sh used")
    ap.add_argument("--mode", required=True,
                    choices=["dynamic", "force", "off", "check", "multiple"])
    ap.add_argument("--ep-size", type=int, default=None)
    ap.add_argument("--redundant-experts", type=int, default=None)
    ap.add_argument("--window-size", type=int, default=None)
    ap.add_argument("--step-interval", type=int, default=None)
    ap.add_argument("--dataset", default=None, help="benchmark dataset name/path")
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--rounds", type=int, default=4, help="rounds per group (default 4)")
    ap.add_argument("--groups", default=",".join(DEFAULT_GROUPS),
                    help="comma-separated groups, default off,dynamic,force")
    ap.add_argument("--branch", choices=["site-fork", "upstream"], default="site-fork")
    ap.add_argument("--service", default=None)
    ap.add_argument("--note", default=None)
    args = ap.parse_args()

    if not os.path.isfile(args.template):
        print("ERROR: template not found: %s" % args.template, file=sys.stderr)
        return 2
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    bad = [g for g in groups if g not in DEFAULT_GROUPS]
    if bad:
        print("ERROR: unknown group(s): %s" % ",".join(bad), file=sys.stderr)
        return 2
    if args.rounds < 1:
        print("ERROR: --rounds must be >= 1", file=sys.stderr)
        return 2

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "service": args.service,
        "branch": args.branch,
        "template": {
            "path": os.path.abspath(args.template),
            "sha256": file_sha256(args.template),
        },
        "mode": args.mode,
        "params": {
            "ep_size": args.ep_size,
            "redundant_experts": args.redundant_experts,
            "window_size": args.window_size,
            "step_interval": args.step_interval,
        },
        "params_effective": parse_effective_params(args.template),
        "experiment": {
            "dataset": args.dataset,
            "concurrency": args.concurrency,
            "rounds": args.rounds,
            "groups": groups,
            "drop_warmup_round": True,
        },
        "note": args.note,
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
