#!/usr/bin/env python3
"""Static check for public vLLM-Ascend PD P2P Layerwise Mode A config.

Usage:
  python3 check_layerwise_config.py config.json
  python3 check_layerwise_config.py '<json string>'

This script only checks Mode A structure. It cannot verify runtime
kv_cache_groups / kv_caches keys; those require startup logs or the
evidence collection described in the admission workflow.
"""
from __future__ import annotations

import json
import sys
from typing import Any


def load_config(arg: str) -> dict[str, Any]:
    try:
        with open(arg, "rb") as f:
            return json.load(f)
    except OSError:
        try:
            return json.loads(arg)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                "config must be a readable JSON file or a JSON object"
            ) from exc


def children(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    extra = cfg.get("kv_connector_extra_config") or {}
    if not isinstance(extra, dict):
        return []
    items = extra.get("connectors") or []
    if not isinstance(items, list):
        return []
    return [c for c in items if isinstance(c, dict)]


def json_bool(container: dict[str, Any], key: str, errors: list[str], label: str) -> bool:
    value = container.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        errors.append(f"{label} must be JSON boolean, got {type(value).__name__}")
        return False
    return value


def check(cfg: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    connector = cfg.get("kv_connector")
    role = cfg.get("kv_role")
    raw_extra = cfg.get("kv_connector_extra_config") or {}
    if not isinstance(raw_extra, dict):
        errors.append("kv_connector_extra_config must be a JSON object")
        extra = {}
    else:
        extra = raw_extra
    kids = children(cfg)

    if role not in {"kv_producer", "kv_consumer"}:
        errors.append(f"kv_role must be kv_producer/kv_consumer, got {role!r}")

    if "layerwise_host_kv_offload" in extra:
        errors.append("layerwise_host_kv_offload is outside public Mode A; remove it")

    if connector == "MultiConnector":
        stores = [c for c in kids if c.get("kv_connector") == "AscendStoreConnector"]
        lws = [c for c in kids if c.get("kv_connector") == "MooncakeLayerwiseConnector"]
        if not lws:
            errors.append("Mode A requires a MooncakeLayerwiseConnector child")
        for lw in lws:
            le = lw.get("kv_connector_extra_config") or {}
            if isinstance(le, dict) and "use_ascend_direct" in le:
                errors.append(
                    "use_ascend_direct is not a MooncakeLayerwiseConnector option; use the V2 path docs"
                )
        for store in stores:
            se = store.get("kv_connector_extra_config") or {}
            if not isinstance(se, dict):
                errors.append("Store kv_connector_extra_config must be a JSON object")
                continue
            use_layerwise = json_bool(se, "use_layerwise", errors, "Store use_layerwise")
            if use_layerwise:
                errors.append("Store use_layerwise=true is outside public Mode A")
            if "use_layerwise_range" in se:
                errors.append("Store use_layerwise_range is outside public Mode A")
            if "backend" in se and not isinstance(se["backend"], str):
                errors.append("Store backend must be a string")
            if "load_async" in se and not isinstance(se["load_async"], bool):
                errors.append(
                    f"Store load_async must be JSON boolean, got {type(se['load_async']).__name__}"
                )
            if "use_ascend_direct" in se:
                errors.append(
                    "use_ascend_direct is not a MooncakeLayerwiseConnector option; use the V2 path docs"
                )
        if not stores:
            warnings.append("No AscendStoreConnector child; shared KV pool is not part of this config")
    elif connector == "MooncakeLayerwiseConnector":
        warnings.append(
            "Single MooncakeLayerwiseConnector: no AscendStore child; confirm whether shared KV pool is expected"
        )
    else:
        warnings.append(f"kv_connector={connector!r}; this skill targets MultiConnector/MooncakeLayerwiseConnector")

    notes.append("Runtime must confirm: kv_caches has one key per layer index (or indexer merged/absent)")
    notes.append("Runtime must confirm: num_draft_attn_layers satisfies the connector mapping")
    notes.append("Runtime must confirm: Store use_layerwise=false and backend matches the deployment")

    return errors, warnings, notes


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 check_layerwise_config.py <config.json|json-string>")
        return 2
    try:
        cfg = load_config(sys.argv[1])
    except ValueError as exc:
        print("ERRORS:")
        print(f"- {exc}")
        return 2
    if not isinstance(cfg, dict):
        print("ERRORS:")
        print("- config must be a JSON object")
        return 1
    errors, warnings, notes = check(cfg)
    print("ERRORS:")
    print("\n".join(f"- {e}" for e in errors) or "- none")
    print("WARNINGS:")
    print("\n".join(f"- {w}" for w in warnings) or "- none")
    print("RUNTIME_NOTES:")
    print("\n".join(f"- {n}" for n in notes) or "- none")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
