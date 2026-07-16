#!/usr/bin/env python3
"""Probe local compiler support for NEON, SVE and SME benchmark builds."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ARCHES = ("neon", "sve", "sme")
CANDIDATES = ("cc", "clang", "gcc", "aarch64-linux-gnu-gcc")
TEST_SOURCES = {
    "neon": """#include <arm_neon.h>
void probe_neon(const float *input, float *output) {
    float32x4_t value = vld1q_f32(input);
    vst1q_f32(output, value);
}
""",
    "sve": """#include <arm_sve.h>
#include <stdint.h>
void probe_sve(const float *input, float *output, unsigned n) {
    for (unsigned i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32((uint64_t)i, (uint64_t)n);
        svfloat32_t value = svld1_f32(pg, input + i);
        svst1_f32(pg, output + i, value);
    }
}
""",
    "sme": """#include <arm_sme.h>
#include <stdint.h>
void probe_sme(const float *input, float *output, unsigned n) __arm_streaming;
void probe_sme(const float *input, float *output, unsigned n) {
    for (unsigned i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32((uint64_t)i, (uint64_t)n);
        svfloat32_t value = svld1_f32(pg, input + i);
        svst1_f32(pg, output + i, value);
    }
}
""",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe local compiler support for NEON, SVE and SME benchmark builds."
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--list", action="store_true", help="Print key=value output.")
    parser.add_argument(
        "--arch",
        choices=ARCHES,
        help="Limit the summary output to a single target architecture.",
    )
    parser.add_argument(
        "--require",
        choices=ARCHES,
        help="Exit with status 2 unless at least one compiler supports the target architecture.",
    )
    return parser.parse_args()


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def compiler_kind(name: str, banner: str) -> str:
    lower = f"{name} {banner}".lower()
    if "clang" in lower:
        return "clang"
    if "gcc" in lower or "g++" in lower:
        return "gcc"
    return "other"


def host_machine() -> str:
    for command in (
        ("sysctl", "-n", "hw.optional.arm64"),
        ("uname", "-m"),
        ("sysctl", "-n", "hw.machine"),
    ):
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        value = (completed.stdout or completed.stderr).strip()
        if value:
            if command == ("sysctl", "-n", "hw.optional.arm64"):
                if value == "1":
                    return "arm64"
                continue
            return value
    return platform.machine() or "unknown"


def arch_flags(arch: str, host_os: str, host_machine: str, kind: str) -> list[str]:
    flags: list[str] = []
    if arch == "neon":
        if not (host_os == "Darwin" and host_machine == "arm64" and kind == "clang"):
            flags.append("-march=armv8-a+simd")
        return flags
    if arch == "sve":
        return ["-march=armv8-a+sve", "-msve-vector-bits=scalable"]
    return ["-march=armv9.2-a+sme", "-msve-vector-bits=scalable"]


def extra_target_flags(compiler_name: str, host_machine: str, kind: str) -> list[str]:
    if kind == "clang" and host_machine not in ("arm64", "aarch64") and compiler_name != "cc":
        return ["--target=aarch64-linux-gnu"]
    return []


def probe_compiler(
    compiler_name: str, compiler_path: str, host_os: str, host_machine: str
) -> dict[str, Any]:
    banner = first_non_empty_line(
        subprocess.run(
            [compiler_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
    )
    if not banner:
        banner = first_non_empty_line(
            subprocess.run(
                [compiler_path, "--version"],
                check=False,
                capture_output=True,
                text=True,
            ).stderr
        )
    kind = compiler_kind(compiler_name, banner)
    result: dict[str, Any] = {
        "name": compiler_name,
        "path": compiler_path,
        "banner": banner,
        "kind": kind,
        "supported_arches": [],
        "checks": {},
    }

    command_prefix: list[str] = []
    if host_os == "Darwin" and host_machine == "arm64":
        command_prefix = ["arch", "-arm64"]

    with tempfile.TemporaryDirectory(prefix="apply-vectorization-compiler-") as tmpdir:
        tmp_root = Path(tmpdir)
        for arch in ARCHES:
            source_path = tmp_root / f"probe_{arch}.c"
            output_path = tmp_root / f"probe_{arch}.o"
            source_path.write_text(TEST_SOURCES[arch], encoding="utf-8")
            flags = extra_target_flags(compiler_name, host_machine, kind)
            flags.extend(arch_flags(arch, host_os, host_machine, kind))
            command = [
                *command_prefix,
                compiler_path,
                *flags,
                "-std=c11",
                "-O2",
                "-Werror",
                "-c",
                str(source_path),
                "-o",
                str(output_path),
            ]
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
            reason = ""
            supported = completed.returncode == 0
            if not supported:
                reason = first_non_empty_line(completed.stderr) or first_non_empty_line(
                    completed.stdout
                )
            check_result = {
                "supported": supported,
                "flags": flags,
                "reason": reason,
            }
            result["checks"][arch] = check_result
            if supported:
                result["supported_arches"].append(arch)
    return result


def select_recommendations(compilers: list[dict[str, Any]]) -> dict[str, str]:
    recommendations = {arch: "" for arch in ARCHES}
    for arch in ARCHES:
        for compiler in compilers:
            if compiler["checks"][arch]["supported"]:
                recommendations[arch] = compiler["name"]
                break
    return recommendations


def build_payload(compilers: list[dict[str, Any]], arch_filter: str | None) -> dict[str, Any]:
    machine = host_machine()
    host = {
        "os": platform.system() or "unknown",
        "machine": machine,
    }
    recommended_by_arch = select_recommendations(compilers)
    arches = [arch_filter] if arch_filter else list(ARCHES)
    architectures: dict[str, Any] = {}

    for arch in arches:
        reason = ""
        if not recommended_by_arch[arch]:
            for compiler in compilers:
                candidate_reason = compiler["checks"][arch]["reason"]
                if candidate_reason:
                    reason = candidate_reason
                    break
            if not reason:
                reason = "no compiler candidate found"
        architectures[arch] = {
            "available": bool(recommended_by_arch[arch]),
            "recommended_compiler": recommended_by_arch[arch],
            "reason": reason,
        }

    overall = ""
    for compiler in compilers:
        if compiler["supported_arches"]:
            overall = compiler["name"]
            break

    return {
        "host": host,
        "recommended_compiler": overall,
        "architectures": architectures,
        "compilers": compilers,
    }


def print_text(payload: dict[str, Any]) -> None:
    print(f"host_os={payload['host']['os']}")
    print(f"host_machine={payload['host']['machine']}")
    print(f"recommended_compiler={payload['recommended_compiler']}")
    for arch, info in payload["architectures"].items():
        status = "available" if info["available"] else "unavailable"
        print(
            f"{arch}={status} compiler={info['recommended_compiler']} reason={info['reason']}"
        )
    for index, compiler in enumerate(payload["compilers"]):
        print(f"compiler[{index}].name={compiler['name']}")
        print(f"compiler[{index}].path={compiler['path']}")
        print(f"compiler[{index}].kind={compiler['kind']}")
        print(f"compiler[{index}].banner={compiler['banner']}")
        print(
            f"compiler[{index}].supported_arches={','.join(compiler['supported_arches'])}"
        )


def main() -> int:
    args = parse_args()
    host_os = platform.system() or "unknown"
    host_machine_value = host_machine()
    seen_paths: set[str] = set()
    compilers: list[dict[str, Any]] = []

    for candidate in CANDIDATES:
        compiler_path = shutil.which(candidate)
        if compiler_path is None:
            continue
        resolved = str(Path(compiler_path).resolve())
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        compilers.append(probe_compiler(candidate, resolved, host_os, host_machine_value))

    payload = build_payload(compilers, args.arch or args.require)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)

    required_arch = args.require
    if required_arch and not payload["architectures"][required_arch]["available"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
