"""Manifest data loading for the batch-drive pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re


def simple_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {"run": {}, "targets": []}
    section: str | None = None
    current_target: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            if section not in data:
                data[section] = {} if section != "targets" else []
            continue
        stripped = line.strip()
        if section == "targets" and stripped.startswith("- "):
            current_target = {}
            data["targets"].append(current_target)
            rest = stripped[2:].strip()
            if rest and ":" in rest:
                key, value = rest.split(":", 1)
                current_target[key.strip()] = simple_scalar(value)
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if section == "targets":
            if current_target is None:
                current_target = {}
                data["targets"].append(current_target)
            current_target[key.strip()] = simple_scalar(value)
        elif section:
            container = data.setdefault(section, {})
            if isinstance(container, dict):
                container[key.strip()] = simple_scalar(value)
    return data


def load_manifest(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError:
        return parse_simple_yaml(text)
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return parse_simple_yaml(text)
    if isinstance(loaded, dict):
        return loaded
    return parse_simple_yaml(text)
