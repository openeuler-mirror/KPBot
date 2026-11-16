#!/usr/bin/env python3
"""Validate the minimal frozen DiskANN benchmark manifest contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def require(obj: dict[str, Any], key: str, context: str) -> Any:
    if key not in obj or obj[key] in (None, "", "REPLACE_ME"):
        raise ValueError(f"{context}.{key} is required")
    return obj[key]


def validate(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("root must be an object")
    if payload.get("schema_version") != "diskann-benchmark.v1":
        raise ValueError("schema_version must be diskann-benchmark.v1")
    require(payload, "baseline_id", "root")
    repo = require(payload, "repo", "root")
    binding = require(payload, "binding", "root")
    policy = require(payload, "policy", "root")
    datasets = require(payload, "datasets", "root")
    if not isinstance(repo, dict) or not isinstance(binding, dict) or not isinstance(policy, dict):
        raise ValueError("repo, binding, and policy must be objects")
    require(repo, "path", "repo")
    commit = require(repo, "commit", "repo")
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64}|unknown)", commit):
        raise ValueError("repo.commit must be full SHA or unknown")
    if commit == "unknown" and not re.fullmatch(r"sha256:[a-f0-9]{64}", str(repo.get("source_digest", ""))):
        raise ValueError("non-Git source requires source_digest sha256")
    for obj, fields in ((payload, ("baseline_id",)), (repo, ("path",)),
                        (binding, ("cpus",)), (policy, ("warmup", "page_cache"))):
        for field in fields:
            value = require(obj, field, "manifest")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
    require(binding, "cpus", "binding")
    numa = binding.get("numa_node")
    if "numa_node" not in binding or (numa is not None and (type(numa) is not int or numa < 0)):
        raise ValueError("binding.numa_node must be null or a non-negative integer")
    threads = require(binding, "threads", "binding")
    if type(threads) is not int or threads <= 0:
        raise ValueError("binding.threads must be a positive integer")
    repetitions = require(policy, "repetitions", "policy")
    if type(repetitions) is not int or repetitions < 2:
        raise ValueError("policy.repetitions must be at least 2")
    require(policy, "warmup", "policy")
    require(policy, "page_cache", "policy")
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("datasets must be a non-empty array")
    required = ("name", "index", "queries", "groundtruth", "distance", "recall_at", "search_list")
    names = set()
    for index, dataset in enumerate(datasets):
        if not isinstance(dataset, dict):
            raise ValueError(f"datasets[{index}] must be an object")
        mode = dataset.get("mode", "disk")
        if not isinstance(mode, str) or mode not in {"memory", "disk"}:
            raise ValueError("dataset mode must be memory or disk")
        fields = required + (("beam_width", "is_flat", "cache_nodes") if mode == "disk" else ())
        for key in fields:
            require(dataset, key, f"datasets[{index}]")
        for key in ("name", "index", "queries", "groundtruth", "distance"):
            if not isinstance(dataset[key], str) or not dataset[key].strip():
                raise ValueError(f"dataset {key} must be non-empty string")
        if dataset["name"] in names:
            raise ValueError("duplicate dataset name")
        names.add(dataset["name"])
        for key in ("recall_at", "search_list") + (("beam_width", "cache_nodes") if mode == "disk" else ()):
            minimum = 0 if key == "cache_nodes" else 1
            if type(dataset[key]) is not int or dataset[key] < minimum:
                raise ValueError(f"datasets[{index}].{key} must be an integer >= {minimum}")
        if mode == "disk" and not isinstance(dataset["is_flat"], bool):
            raise ValueError(f"datasets[{index}].is_flat must be boolean")
        if dataset["search_list"] < dataset["recall_at"]:
            raise ValueError("search_list must be >= recall_at")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
