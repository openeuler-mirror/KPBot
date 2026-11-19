import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT = 5
AT_HWCAP, AT_HWCAP2 = 16, 26
PR_SVE_GET_VL, PR_SVE_VL_LEN_MASK = 51, 0xFFFF
VL_NOTE = "VL belongs to this probe process; no vector length is set."
FEATURE_BITS = {
    "fp": ("hwcap", 0),
    "asimd": ("hwcap", 1),
    "neon": ("hwcap", 1),
    "fp16": ("hwcap", 10),
    "dotprod": ("hwcap", 20),
    "sve": ("hwcap", 22),
    "sve2": ("hwcap2", 1),
    "svei8mm": ("hwcap2", 9),
    "svef64mm": ("hwcap2", 11),
    "i8mm": ("hwcap2", 13),
    "bf16": ("hwcap2", 14),
    "sme": ("hwcap2", 23),
    "cssc": ("hwcap2", 34),
    "sve2p1": ("hwcap2", 36),
    "sme2": ("hwcap2", 37),
}
COMPILE_PROBES = {
    "neon": (
        "armv8-a+simd",
        "arm_neon.h",
        "int16x8_t probe(int16x8_t a, int16x8_t b) { return vaddq_s16(a, b); }",
    ),
    "fp16": (
        "armv8.2-a+fp16",
        "arm_neon.h",
        "float16x8_t probe(float16x8_t a, float16x8_t b) { return vaddq_f16(a, b); }",
    ),
    "dotprod": (
        "armv8.2-a+dotprod",
        "arm_neon.h",
        "int32x4_t probe(int32x4_t a, int8x16_t b, int8x16_t c) { return vdotq_s32(a, b, c); }",
    ),
    "bf16": (
        "armv8.2-a+bf16",
        "arm_neon.h",
        "float32x4_t probe(float32x4_t a, bfloat16x8_t b, bfloat16x8_t c) { return vbfdotq_f32(a, b, c); }",
    ),
    "i8mm": (
        "armv8.2-a+i8mm",
        "arm_neon.h",
        "int32x4_t probe(int32x4_t a, int8x16_t b, int8x16_t c) { return vmmlaq_s32(a, b, c); }",
    ),
    "sve": (
        "armv8.2-a+sve",
        "arm_sve.h",
        "svint32_t probe(svint32_t a, svint32_t b) { return svadd_s32_x(svptrue_b32(), a, b); }",
    ),
    "sve2": (
        "armv8.5-a+sve2",
        "arm_sve.h",
        "svint32_t probe(svint16_t a, svint16_t b) { return svaddlb_s32(a, b); }",
    ),
    "svei8mm": (
        "armv8.2-a+sve+i8mm",
        "arm_sve.h",
        "svint32_t probe(svint32_t a, svint8_t b, svint8_t c) { return svmmla_s32(a, b, c); }",
    ),
    "svef64mm": (
        "armv8.2-a+sve+f64mm",
        "arm_sve.h",
        "svfloat64_t probe(svfloat64_t a, svfloat64_t b, svfloat64_t c) { return svmmla_f64(a, b, c); }",
    ),
    "cssc": (
        "armv8.9-a+cssc",
        "stdint.h",
        'unsigned long probe(unsigned long a) { unsigned long r; __asm__("cnt %0, %1" : "=r"(r) : "r"(a)); return r; }',
    ),
    "sve2p1": (
        "armv9.4-a+sve2p1",
        "arm_sve.h",
        'void probe(void) { __asm__ volatile("zipq1 z0.b, z1.b, z2.b" ::: "z0"); }',
    ),
    "sme": (
        "armv9-a+sme",
        "arm_sme.h",
        'void probe(void) { __asm__ volatile("smstart za\\n\\tzero {za}\\n\\tsmstop za" ::: "memory"); }',
    ),
    "sme2": (
        "armv9-a+sme2",
        "arm_sme.h",
        'void probe(void) { __asm__ volatile("smstart za\\n\\tzero {zt0}\\n\\tsmstop za" ::: "memory"); }',
    ),
}


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_command(executable, arguments, timeout=TIMEOUT, input_text=None):
    result = {
        "argv": [executable, *arguments],
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "path": None,
        "realpath": None,
        "source": "shutil.which",
        "status": "not_found",
    }
    try:
        result["path"] = shutil.which(executable)
        if result["path"] is None:
            result["error"] = "Executable not found or not executable"
            return result
        result["path"] = os.path.abspath(result["path"])
        result["realpath"] = os.path.realpath(result["path"])
        result["source"] = "subprocess.run"
        completed = subprocess.run(
            [result["path"], *arguments],
            capture_output=True,
            text=True,
            errors="replace",
            input=input_text,
            timeout=timeout,
            shell=False,
        )
        result.update(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            status="ok" if completed.returncode == 0 else "failed",
        )
        if completed.returncode != 0:
            result["error"] = "Command exited with status %s" % completed.returncode
    except subprocess.TimeoutExpired as exc:
        result.update(
            status="timeout",
            error="Command timed out after %s seconds" % timeout,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
        )
    except (OSError, ValueError) as exc:
        result.update(status="error", error=str(exc))
    return result


def arm64_capabilities():
    source = "libc.getauxval (Linux arm64 UAPI)"
    caps = {key: {"value": None, "source": source} for key in ("hwcap", "hwcap2")}
    vl = {
        "source": "libc.prctl(PR_SVE_GET_VL)",
        "raw": None,
        "bytes": None,
        "flags": None,
        "note": VL_NOTE,
    }
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except (OSError, AttributeError) as exc:
        for cap in caps.values():
            cap["error"] = str(exc)
        libc = None
    if libc is not None:
        try:
            getauxval = libc.getauxval
            getauxval.argtypes = [ctypes.c_ulong]
            getauxval.restype = ctypes.c_ulong
            for key, tag in (("hwcap", AT_HWCAP), ("hwcap2", AT_HWCAP2)):
                ctypes.set_errno(0)
                value = getauxval(tag)
                err = ctypes.get_errno()
                if value == 0 and err:
                    caps[key]["error"] = str(OSError(err, os.strerror(err)))
                else:
                    caps[key]["value"] = value
        except (OSError, AttributeError) as exc:
            for cap in caps.values():
                if cap["value"] is None:
                    cap["error"] = str(exc)
    features = {
        name: (None if caps[key]["value"] is None else bool(caps[key]["value"] & (1 << bit)))
        for name, (key, bit) in FEATURE_BITS.items()
    }
    if features["sve"] is True:
        try:
            prctl = libc.prctl
            prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
            prctl.restype = ctypes.c_int
            ctypes.set_errno(0)
            raw = prctl(PR_SVE_GET_VL, 0, 0, 0, 0)
            if raw == -1:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err) if err else "prctl failed without errno")
            vl.update(raw=raw, bytes=raw & PR_SVE_VL_LEN_MASK, flags=raw & ~PR_SVE_VL_LEN_MASK)
        except (OSError, AttributeError) as exc:
            vl["error"] = str(exc)
    else:
        vl["error"] = "SVE not reported by AT_HWCAP; VL was not queried"
    return {"source": source, **caps, "features": features, "sve_vl": vl}


def probe_platform():
    result = {
        "source": "platform.uname",
        "system": None,
        "architecture": None,
        "release": None,
        "cpu": None,
    }
    try:
        info = platform.uname()
        result.update(system=info.system, architecture=info.machine, release=info.release)
    except OSError as exc:
        result["error"] = str(exc)
        return result
    if (result["system"], result["architecture"]) != ("Linux", "aarch64"):
        result["error"] = "Run this probe on the target Linux AArch64 machine"
        return result
    result["cpu"] = {
        "source": "lscpu",
        "raw": run_command("lscpu", []),
        "arm64": arm64_capabilities(),
    }
    return result


def check_compiler(compiler, features, language, arguments):
    checks = []
    with tempfile.TemporaryDirectory(prefix="media-toolchain-") as directory:
        output = str(Path(directory) / "probe")
        source = (
            "#include <stdio.h>\nint main(void) { return puts(\"\") < 0; }\n"
            if language == "c"
            else "#include <vector>\nint main() { std::vector<int> v(1, 1); return v[0] - 1; }\n"
        )
        flags = [*arguments, "-O2"]
        result = run_command(
            compiler, [*flags, "-x", language, "-", "-o", output], input_text=source
        )
        checks.append(
            {
                "feature": "baseline",
                "language": language,
                "flags": flags,
                "source": source,
                "result": result,
            }
        )
        for feature, (arch, header, body) in COMPILE_PROBES.items():
            if features.get(feature) is not True:
                continue
            flags = [*arguments, "-O2", "-Werror", "-march=" + arch]
            source = "#include <" + header + ">\n" + body + "\n"
            result = run_command(
                compiler,
                [*flags, "-x", language, "-c", "-", "-o", output + ".o"],
                input_text=source,
            )
            checks.append(
                {
                    "feature": feature,
                    "language": language,
                    "flags": flags,
                    "source": source,
                    "result": result,
                }
            )
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe the target Linux AArch64 environment")
    parser.add_argument("--compiler", help="Agent-selected C compiler executable; no default")
    parser.add_argument("--cxx", help="Project C++ compiler, when required")
    parser.add_argument(
        "--compiler-arg",
        action="append",
        default=[],
        help="Common C toolchain argument; repeat as --compiler-arg=VALUE",
    )
    parser.add_argument(
        "--cxx-arg",
        action="append",
        default=[],
        help="Common C++ toolchain argument; repeat as --cxx-arg=VALUE",
    )
    parser.add_argument(
        "--build-tool",
        action="append",
        default=[],
        help="Required build tool executable; repeat for each tool",
    )
    parser.add_argument("--mca", help="Known llvm-mca executable; otherwise search PATH")
    args = parser.parse_args(argv)
    platform_info = probe_platform()
    compiler = None if args.compiler is None else run_command(args.compiler, ["--version"])
    cxx = None if args.cxx is None else run_command(args.cxx, ["--version"])
    tools = [run_command(tool, ["--version"]) for tool in dict.fromkeys(args.build_tool)]
    report = {
        "platform": platform_info,
        "compiler": compiler,
        "cxx": cxx,
        "build_tools": tools,
        "compile_checks": [],
        "mca": run_command("llvm-mca" if args.mca is None else args.mca, ["--version"]),
    }
    gaps = []
    if "error" in platform_info:
        gaps.append(platform_info["error"])
    else:
        arm = platform_info["cpu"]["arm64"]
        for key in ("hwcap", "hwcap2"):
            if arm[key]["value"] is None:
                gaps.append(key + ": " + arm[key].get("error", "unavailable"))
        if arm["features"]["sve"] and arm["sve_vl"]["bytes"] is None:
            gaps.append("sve_vl: " + arm["sve_vl"].get("error", "unavailable"))
        for tool, language, arguments in [
            (compiler, "c", args.compiler_arg),
            (cxx, "c++", args.cxx_arg),
        ]:
            if tool is not None and tool["status"] == "ok":
                report["compile_checks"].extend(
                    check_compiler(tool["path"], arm["features"], language, arguments)
                )
    if compiler is None:
        gaps.append("Project C compiler not selected")
    for tool in [compiler, cxx, *tools]:
        if tool is not None and tool["status"] != "ok":
            gaps.append(tool["argv"][0] + ": " + tool.get("error", tool["status"]))
    for check in report["compile_checks"]:
        if check["result"]["status"] != "ok":
            gaps.append(
                check["language"]
                + "/"
                + check["feature"]
                + ": "
                + check["result"].get("error", check["result"]["status"])
            )
    report["gaps"] = gaps
    report["toolchain_ready"] = not gaps
    report["exit_code"] = int(bool(gaps))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
