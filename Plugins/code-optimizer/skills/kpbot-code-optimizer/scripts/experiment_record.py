#!/usr/bin/env python3
"""Validate and persist immutable diagnostic experiment records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
ID_FIELDS = ("run_id", "point_id", "experiment_id", "hypothesis_id")
TOP_LEVEL_FIELDS = (
    "schema_version",
    *ID_FIELDS,
    "parent_state",
    "execution_context",
    "hypothesis",
    "experiment",
    "evidence",
)
EXECUTION_CONTEXT_FIELDS = ("target", "build", "workload")
HYPOTHESIS_FIELDS = ("claim", "alternative", "support_if", "refute_if")
EXPERIMENT_FIELDS = ("command", "controlled", "changed", "timeout_seconds")
EVIDENCE_FIELDS = ("path", "scope", "kind", "source")
EVIDENCE_KINDS = frozenset(("observation", "estimate"))
EVIDENCE_SOURCES = frozenset(("target", "fixture", "historical"))


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_fields(value: dict[str, Any], fields: tuple[str, ...], prefix: str = "") -> None:
    for field in fields:
        if field not in value:
            name = f"{prefix}.{field}" if prefix else field
            raise ValueError(f"missing required field: {name}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _non_empty_text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    for index, item in enumerate(value):
        _text(item, f"{field}[{index}]")
    return value


def validate_record(record: dict, evidence_root: Path) -> None:
    """Validate a version 1 record and its evidence references.

    Evidence provenance is recorded but deliberately not interpreted here. In
    particular, fixture metadata never proves target performance.
    """
    record = _mapping(record, "record")
    _require_fields(record, TOP_LEVEL_FIELDS)

    schema_version = record["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")

    for field in ID_FIELDS:
        value = _text(record[field], field)
        if ID_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{field} contains invalid characters")

    _text(record["parent_state"], "parent_state")

    execution_context = _mapping(record["execution_context"], "execution_context")
    _require_fields(execution_context, EXECUTION_CONTEXT_FIELDS, "execution_context")
    for field in EXECUTION_CONTEXT_FIELDS:
        _text(execution_context[field], f"execution_context.{field}")

    hypothesis = _mapping(record["hypothesis"], "hypothesis")
    _require_fields(hypothesis, HYPOTHESIS_FIELDS, "hypothesis")
    for field in HYPOTHESIS_FIELDS:
        _text(hypothesis[field], f"hypothesis.{field}")

    experiment = _mapping(record["experiment"], "experiment")
    _require_fields(experiment, EXPERIMENT_FIELDS, "experiment")
    _non_empty_text_list(experiment["command"], "experiment.command")
    _non_empty_text_list(experiment["controlled"], "experiment.controlled")
    _text(experiment["changed"], "experiment.changed")
    timeout = experiment["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("experiment.timeout_seconds must be a positive integer")

    evidence = record["evidence"]
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")

    root = Path(evidence_root).resolve()
    for index, raw_item in enumerate(evidence):
        prefix = f"evidence[{index}]"
        item = _mapping(raw_item, prefix)
        _require_fields(item, EVIDENCE_FIELDS, prefix)
        path_value = _text(item["path"], f"{prefix}.path")
        _text(item["scope"], f"{prefix}.scope")
        kind = _text(item["kind"], f"{prefix}.kind")
        source = _text(item["source"], f"{prefix}.source")
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"{prefix}.kind must be observation or estimate")
        if source not in EVIDENCE_SOURCES:
            raise ValueError(f"{prefix}.source must be target, fixture, or historical")

        raw_path = Path(path_value)
        resolved = (root / raw_path).resolve()
        if raw_path.is_absolute() or root not in resolved.parents or not resolved.is_file():
            raise ValueError(f"invalid evidence path: {path_value}")


def write_record(record: dict, run_dir: Path) -> Path:
    """Write one immutable final-attempt record under *run_dir*."""
    run_dir = Path(run_dir)
    validate_record(record, run_dir)
    if not record["evidence"]:
        raise ValueError("final experiment record requires evidence")

    folder = run_dir / "experiments" / record["experiment_id"]
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir()
    except FileExistsError as exc:
        raise ValueError("experiment already exists") from exc

    path = folder / "record.json"
    try:
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(
            f"incomplete experiment record retained at {folder}: {exc}"
        ) from exc
    return path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path, help="input record JSON")
    parser.add_argument("--run-dir", required=True, type=Path, help="experiment run directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        path = write_record(record, args.run_dir)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
