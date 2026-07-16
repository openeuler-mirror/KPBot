#!/usr/bin/env python3
"""Combine ISA and compiler probes for before/after benchmark preflight."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ARCHES = ("neon", "sve", "sme")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine ISA and compiler probes for before/after benchmark preflight."
    )
    parser.add_argument("--arch", required=True, choices=ARCHES, help="Target architecture.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--env", action="store_true", help="Print sourceable shell env output.")
    parser.add_argument("--list", action="store_true", help="Print key=value output.")
    parser.add_argument(
        "--require-run",
        action="store_true",
        help="Exit with 2/3 when the benchmark cannot run on this host.",
    )
    return parser.parse_args()


def run_json_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise SystemExit(f"[错误] 命令执行失败: {' '.join(command)}: {stderr}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"[错误] 命令没有返回合法 JSON: {' '.join(command)}: {exc}"
        ) from exc


def host_machine() -> str:
    for command in (
        ["sysctl", "-n", "hw.optional.arm64"],
        ["uname", "-m"],
        ["sysctl", "-n", "hw.machine"],
    ):
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        value = (completed.stdout or completed.stderr).strip()
        if value:
            if command == ["sysctl", "-n", "hw.optional.arm64"]:
                if value == "1":
                    return "arm64"
                continue
            return value
    return sys.platform


def arch_flags(arch: str, host_os: str, host_machine: str, compiler_kind: str) -> list[str]:
    if arch == "neon":
        if host_os == "Darwin" and host_machine == "arm64" and compiler_kind == "clang":
            return []
        return ["-march=armv8-a+simd"]
    if arch == "sve":
        return ["-march=armv8-a+sve", "-msve-vector-bits=scalable"]
    return ["-march=armv9.2-a+sme", "-msve-vector-bits=scalable"]


def baseline_disable_flags(compiler_kind: str) -> list[str]:
    if compiler_kind == "clang":
        return ["-fno-vectorize", "-fno-slp-vectorize"]
    if compiler_kind == "gcc":
        return ["-fno-tree-vectorize", "-fno-tree-slp-vectorize"]
    return []


def build_payload(arch: str, isa_data: dict[str, Any], compiler_data: dict[str, Any]) -> dict[str, Any]:
    host = compiler_data.get("host", {})
    host_machine_value = host.get("machine") or ""
    if not host_machine_value:
        host_machine_value = host_machine()
    compiler_name = compiler_data["architectures"][arch]["recommended_compiler"]
    compiler_info = None
    for entry in compiler_data["compilers"]:
        if entry["name"] == compiler_name:
            compiler_info = entry
            break

    isa_supported = bool(isa_data["capabilities"].get(arch, False))
    native_arm64 = bool(isa_data.get("native_arm64", False))
    if not native_arm64:
        native_arm64 = host_machine_value in {"arm64", "aarch64"}
    status = "ready"
    exit_code = 0
    skip_reason = ""

    if not isa_supported:
        status = "unsupported"
        exit_code = 3
        skip_reason = f"host does not report {arch} capability"
    elif not native_arm64:
        status = "skip"
        exit_code = 2
        skip_reason = "benchmark only runs on native ARM64 hosts"
    elif compiler_info is None:
        status = "skip"
        exit_code = 2
        skip_reason = compiler_data["architectures"][arch]["reason"]

    compiler_payload = {
        "name": compiler_name,
        "path": compiler_info["path"] if compiler_info else "",
        "kind": compiler_info["kind"] if compiler_info else "",
        "banner": compiler_info["banner"] if compiler_info else "",
    }
    arch_flag_list = arch_flags(
        arch,
        host.get("os", "unknown"),
        host_machine_value,
        compiler_payload["kind"],
    )

    return {
        "arch": arch,
        "status": status,
        "exit_code": exit_code,
        "skip_reason": skip_reason,
        "host": {
            "os": host.get("os", "unknown"),
            "machine": host_machine_value,
            "native_arm64": native_arm64,
        },
        "isa": isa_data,
        "compiler": compiler_payload,
        "runnable": status == "ready",
        "common_flags": ["-std=c11", "-O3"],
        "arch_flags": arch_flag_list,
        "baseline_disable_flags": baseline_disable_flags(compiler_payload["kind"]),
        "driver_flags": [],
        "link_flags": [],
    }


def print_text(payload: dict[str, Any]) -> None:
    print(f"arch={payload['arch']}")
    print(f"status={payload['status']}")
    print(f"exit_code={payload['exit_code']}")
    print(f"skip_reason={payload['skip_reason']}")
    print(f"native_arm64={str(payload['host']['native_arm64']).lower()}")
    print(f"runnable={str(payload['runnable']).lower()}")
    print(f"compiler={payload['compiler']['name']}")
    print(f"compiler_path={payload['compiler']['path']}")
    print(f"compiler_kind={payload['compiler']['kind']}")
    print(f"common_flags={'|'.join(payload['common_flags'])}")
    print(f"arch_flags={'|'.join(payload['arch_flags'])}")
    print(f"baseline_disable_flags={'|'.join(payload['baseline_disable_flags'])}")
    print(f"driver_flags={'|'.join(payload['driver_flags'])}")
    print(f"link_flags={'|'.join(payload['link_flags'])}")


def print_env(payload: dict[str, Any]) -> None:
    """Print sourceable shell assignments for benchmark scripts."""

    fields = {
        "status": payload["status"],
        "skip_reason": payload["skip_reason"],
        "compiler_path": payload["compiler"]["path"],
        "common_flags": "|".join(payload["common_flags"]),
        "arch_flags": "|".join(payload["arch_flags"]),
        "baseline_disable_flags": "|".join(payload["baseline_disable_flags"]),
        "driver_flags": "|".join(payload["driver_flags"]),
        "link_flags": "|".join(payload["link_flags"]),
    }
    for key, value in fields.items():
        print(f"{key}={shlex.quote(str(value))}")


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    isa_data = run_json_command([str(script_dir / "detect_isa_features.sh"), "--json"])
    compiler_data = run_json_command(
        [
            str(script_dir / "detect_compiler_support.py"),
            "--json",
            "--arch",
            args.arch,
        ]
    )
    payload = build_payload(args.arch, isa_data, compiler_data)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.env:
        print_env(payload)
    else:
        print_text(payload)

    if args.require_run and payload["exit_code"] != 0:
        return payload["exit_code"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
