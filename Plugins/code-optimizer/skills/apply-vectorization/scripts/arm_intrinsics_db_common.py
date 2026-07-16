#!/usr/bin/env python3
"""Shared helpers for the structured Arm intrinsics knowledge base."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_DB_DIR = SKILL_DIR / "references" / "arm_intrinsics_db"
DEFAULT_INSTRUCTION_ASSET_DIR = SKILL_DIR / "references" / "arm_instruction_assets"
DEFAULT_MANUAL_DIR = SKILL_DIR / "docs" / "arm-intrinsics-manual"
SNAPSHOT_FILES = ("neon.json", "sve.json", "sme.json", "attributes.json")
REQUIRED_RECORD_FIELDS = (
    "id",
    "kind",
    "isa",
    "group_path",
    "display_name",
    "intrinsic_names",
    "instruction_names",
    "prototype",
    "header",
    "feature_macros",
    "required_function_attributes",
    "operand_constraints",
    "immediate_constraints",
    "vectorization_role",
    "tail_policy",
    "usage_template",
    "correctness_rules",
    "anti_patterns",
    "related_items",
    "source",
)
SUPPORTED_ISAS = ("neon", "sve", "sve2", "sme", "sme2")


class DatabaseError(RuntimeError):
    """Raised when the structured knowledge base is missing or malformed."""


def load_json(path: Path) -> Any:
    """Load a UTF-8 JSON file."""

    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def save_json(path: Path, payload: Any) -> None:
    """Write JSON with a stable UTF-8 representation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2, sort_keys=False)
        file_obj.write("\n")


def record_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """Build a stable key for record ordering."""

    return (
        record["isa"],
        "/".join(record["group_path"]),
        record["kind"],
        record["display_name"],
        record["id"],
    )


def validate_record_shape(record: dict[str, Any]) -> None:
    """Validate the required record shape used by the repository tools."""

    missing = [field_name for field_name in REQUIRED_RECORD_FIELDS if field_name not in record]
    if missing:
        raise DatabaseError(
            f"record {record.get('id', '<unknown>')} missing required fields: {missing}"
        )
    if record["isa"] not in SUPPORTED_ISAS:
        raise DatabaseError(f"record {record['id']} uses unsupported isa: {record['isa']}")
    if not isinstance(record["group_path"], list) or not record["group_path"]:
        raise DatabaseError(f"record {record['id']} must provide a non-empty group_path")
    if not isinstance(record["source"], dict):
        raise DatabaseError(f"record {record['id']} must provide a source object")
    for key in ("title", "url", "section", "retrieved_at"):
        if key not in record["source"] or not record["source"][key]:
            raise DatabaseError(f"record {record['id']} source missing key: {key}")


def load_index(db_dir: Path = DEFAULT_DB_DIR) -> dict[str, Any]:
    """Load the index.json metadata for the knowledge base."""

    index_path = db_dir / "index.json"
    if not index_path.exists():
        raise DatabaseError(f"missing database index: {index_path}")
    return load_json(index_path)


def load_records(db_dir: Path = DEFAULT_DB_DIR) -> list[dict[str, Any]]:
    """Load and validate all snapshot records."""

    records: list[dict[str, Any]] = []
    for snapshot_name in SNAPSHOT_FILES:
        snapshot_path = db_dir / snapshot_name
        if not snapshot_path.exists():
            raise DatabaseError(f"missing database snapshot: {snapshot_path}")
        payload = load_json(snapshot_path)
        if not isinstance(payload, list):
            raise DatabaseError(f"snapshot {snapshot_name} must contain a list of records")
        for record in payload:
            validate_record_shape(record)
            records.append(record)
    return sorted(records, key=record_sort_key)


def page_name_for_record(record: dict[str, Any]) -> str:
    """Map a record to its generated manual page."""

    if record["isa"] == "neon":
        return "neon"
    if record["isa"] in ("sve", "sve2"):
        return "sve"
    return "sme"


def group_path_without_page(record: dict[str, Any]) -> list[str]:
    """Return the nested group path without the top-level ISA bucket."""

    group_path = list(record["group_path"])
    if group_path and group_path[0] in {"neon", "sve", "sme", "rules"}:
        return group_path[1:]
    return group_path


def format_group_label(value: str) -> str:
    """Convert a slug-like group token to a readable heading."""

    stripped = value.replace("_", " ").replace("-", " ").strip()
    if not stripped:
        return value
    if stripped.isupper():
        return stripped
    return stripped.title()
