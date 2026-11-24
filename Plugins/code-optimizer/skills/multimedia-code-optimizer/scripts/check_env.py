#!/usr/bin/env python3
"""探测本机可用的执行档、编译器、QEMU，并实测运行时 SVE 向量长度。

做的是真编译 + 真运行，不是只看版本号：版本够但 `-march` 配置串不支持、
或编译期 256 位假设和运行时 VL 不一致，都是这里能抓出来的问题。

探测回答的是"这台机器能做什么"，不是"这次用哪种方式"——后者由用户在第二问里选，
本脚本只负责校验该选择是否可行。

用法:
    python3 check_env.py                       # 自动探测，打印 JSON
    python3 check_env.py --method native       # 校验"本机原生验证"是否可行
    python3 check_env.py --method qemu         # 校验"QEMU 验证"是否可行
    python3 check_env.py --out other/env.json  # 同时写文件

退出码:
    0  至少一档可用且（若可运行）VL 校验通过
    1  编译或 VL 校验失败
    2  没有任何可用的编译/运行环境
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BENCH_DIR = os.path.join(SKILL_ROOT, "assets", "bench")

MARCH = "armv9.6-a+sve2p1+sme2p2"
PROBE_FLAGS = ["-O2", "-march=" + MARCH, "-msve-vector-bits=256", "-ffreestanding"]

NATIVE_VL_PROBE = r"""
/* 用退出码回传运行时向量长度（字节）。不依赖 stdio/libc，避免探针本身成为变量。
 * cntb 是 SVE 基础指令；本机没有 SVE 时会 SIGILL，退出码 132。 */
int main(void)
{
    unsigned long v;
    __asm__ volatile("cntb %0" : "=r"(v));
    return (int)(v & 0x7f);
}
"""

VL_PROBE_MAIN = r"""
#include "bench_shim.h"
int main(void)
{
    uint64_t vl = sh_vl_bytes();
    sh_puts("VL bytes=");
    sh_put_u64(vl);
    sh_puts("\n");
    sh_exit(0);
    return 0;
}
"""


def run(cmd, timeout=60, cwd=None):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def which(name):
    path = shutil.which(name)
    return path


def probe_compile(cc, tmp):
    """用目标 -march 串真编一次，确认工具链认这个配置。"""
    src = os.path.join(tmp, "probe.c")
    obj = os.path.join(tmp, "probe.o")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("#include <arm_sve.h>\n"
                 "long f(void) { return svcntb(); }\n")
    rc, _, err = run([cc] + PROBE_FLAGS + ["-c", src, "-o", obj])
    return {
        "ok": rc == 0,
        "returncode": rc,
        "error": (err or "").strip().splitlines()[:6],
    }


def probe_runtime_vl(cc, qemu, tmp):
    """编译裸机 VL 探针并在 QEMU 里跑，读真实 cntb。"""
    main_c = os.path.join(tmp, "vlprobe.c")
    elf = os.path.join(tmp, "vlprobe.elf")
    with open(main_c, "w", encoding="utf-8") as fh:
        fh.write(VL_PROBE_MAIN)

    srcs = [
        os.path.join(BENCH_DIR, "crt0.S"),
        os.path.join(BENCH_DIR, "bench_shim.c"),
        main_c,
    ]
    missing = [s for s in srcs if not os.path.exists(s)]
    if missing:
        return {"ok": False, "error": "bench_assets_missing", "missing": missing}

    link = os.path.join(BENCH_DIR, "link.ld")
    cmd = ([cc] + PROBE_FLAGS +
           ["-DSIMD_BENCH_VL_BYTES=32", "-I", BENCH_DIR,
            "-nostdlib", "-T", link] + srcs + ["-o", elf])
    rc, _, err = run(cmd, timeout=120)
    if rc != 0:
        return {"ok": False, "error": "build_failed",
                "stderr": (err or "").strip().splitlines()[:8]}

    qcmd = [qemu, "-M", "virt", "-cpu", "max", "-icount", "shift=0",
            "-nographic", "-semihosting-config", "enable=on,target=native",
            "-kernel", elf]
    rc, out, err = run(qcmd, timeout=120)
    text = (out or "") + (err or "")
    vl = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VL bytes="):
            try:
                vl = int(line.split("=", 1)[1])
            except ValueError:
                pass
    return {
        "ok": rc == 0 and vl is not None,
        "returncode": rc,
        "runtime_vl_bytes": vl,
        "compile_bits": 256,
        "matches": (vl == 32) if vl is not None else None,
        "raw": text.strip().splitlines()[:6],
    }


def probe_native_vl(cc, tmp):
    """在本机编译并运行一个 cntb 探针，返回真实向量长度（字节）。

    只在 aarch64 Linux 上调用。编译时不带 -msve-vector-bits，否则 cntb 会被
    折叠成常数，探的就不是运行时值了。
    """
    src = os.path.join(tmp, "native_vl.c")
    exe = os.path.join(tmp, "native_vl")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(NATIVE_VL_PROBE)

    last_err = []
    for extra in ([], ["-march=armv8-a+sve"], ["-march=native"]):
        rc, _, err = run([cc, "-O1"] + extra + [src, "-o", exe], timeout=120)
        if rc == 0:
            break
        last_err = (err or "").strip().splitlines()[:4]
    else:
        return {"ok": False, "error": "native_probe_build_failed",
                "stderr": last_err}

    rc, _, err = run([exe], timeout=60)
    if rc == 0:
        return {"ok": False, "error": "cntb_returned_zero",
                "returncode": rc, "stderr": (err or "").strip()[:200]}
    if rc < 0 or rc >= 128:
        return {"ok": False, "error": "sve_not_available_or_sigill",
                "returncode": rc,
                "hint": "退出码 132 = SIGILL，说明本机不支持 SVE"}
    return {"ok": True, "runtime_vl_bytes": rc, "compile_bits": 256,
            "matches": rc == 32}


def native_feasibility(env, tmp):
    """判定"本机原生验证"是否可行，并给出具体阻塞原因。"""
    reasons = []
    if not sys.platform.startswith("linux"):
        reasons.append("host_is_not_linux(%s)" % sys.platform)
    machine = (env.get("host") or {}).get("arch", "")
    if machine not in ("aarch64", "arm64"):
        reasons.append("host_is_not_aarch64(%s)" % machine)

    cc = ""
    for name in ("gcc", "aarch64-linux-gnu-gcc"):
        path = which(name)
        if not path:
            continue
        _, target, _ = run([path, "-dumpmachine"])
        if "aarch64" in (target or ""):
            cc = path
            break
    if not cc:
        reasons.append("no_aarch64_native_compiler")

    result = {"feasible": not reasons, "compiler": cc,
              "blocking_reasons": reasons, "sve": {}}
    if cc and not reasons:
        result["sve"] = probe_native_vl(cc, tmp)
        if not result["sve"].get("ok"):
            result["feasible"] = False
            result["blocking_reasons"].append("native_sve_probe_failed")
        elif result["sve"].get("matches") is False:
            result["feasible"] = False
            result["blocking_reasons"].append(
                "native_sve_vl_is_%s_bytes_not_32"
                % result["sve"].get("runtime_vl_bytes"))
    return result


def qemu_feasibility(env, tmp):
    """判定"QEMU 验证"是否可行。"""
    reasons = []
    cc = which("aarch64-elf-gcc")
    qemu = which("qemu-system-aarch64")
    if not cc:
        reasons.append("no_aarch64-elf-gcc")
    if not qemu:
        reasons.append("no_qemu-system-aarch64")

    result = {"feasible": not reasons, "compiler": cc, "qemu": qemu,
              "blocking_reasons": reasons, "sve": {}}
    if not reasons:
        result["sve"] = probe_runtime_vl(cc, qemu, tmp)
        if not result["sve"].get("ok"):
            result["feasible"] = False
            result["blocking_reasons"].append("qemu_vl_probe_failed")
        elif result["sve"].get("matches") is False:
            result["feasible"] = False
            result["blocking_reasons"].append("qemu_sve_vl_mismatch")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--method", choices=["auto", "native", "qemu"], default="auto",
                    help="校验用户选定的验证方式是否可行；auto 只做能力探测")
    ap.add_argument("--out", default=None, help="把结果写到文件（如 other/env.json）")
    ap.add_argument("--skip-runtime", action="store_true",
                    help="只做编译探测，不在 QEMU 里跑")
    args = ap.parse_args(argv)

    env = {
        "host": {
            "os": sys.platform,
            "arch": os.uname().machine if hasattr(os, "uname") else "",
        },
        "profile": "none",
        "cc": {},
        "qemu": {},
        "sve": {},
        "perf": {"available": which("perf") is not None},
        "notes": "",
    }

    cross_cc = which("aarch64-elf-gcc")
    linux_cc = which("aarch64-linux-gnu-gcc")
    native_cc = which("gcc")
    qemu_sys = which("qemu-system-aarch64")
    qemu_user = which("qemu-aarch64")

    with tempfile.TemporaryDirectory(prefix="simd-env-") as tmp:
        chosen = None
        for label, cc in (("aarch64-elf-gcc", cross_cc),
                          ("aarch64-linux-gnu-gcc", linux_cc),
                          ("gcc", native_cc)):
            if not cc:
                continue
            rc, out, _ = run([cc, "--version"])
            version = (out or "").strip().splitlines()
            _, machine, _ = run([cc, "-dumpmachine"])
            machine = (machine or "").strip()
            info = {"path": cc, "name": label, "target": machine,
                    "version": version[0] if version else ""}
            # 只认真正面向 aarch64 的编译器。宿主上的 cc/gcc 往往是 clang，
            # 它对 armv9.6 的 -march 只会警告后忽略，probe 会假装成功。
            if "aarch64" in machine:
                info["armv9_6_probe"] = probe_compile(cc, tmp)
                if info["armv9_6_probe"]["ok"] and chosen is None:
                    chosen = label
            else:
                info["armv9_6_probe"] = {"ok": False,
                                         "error": ["not an aarch64 target"]}
            env["cc"][label] = info

        env["qemu"] = {"system": qemu_sys or "", "user": qemu_user or ""}

        if cross_cc and qemu_sys:
            env["profile"] = "cross-qemu"
            if not args.skip_runtime:
                env["sve"] = probe_runtime_vl(cross_cc, qemu_sys, tmp)
        elif linux_cc and qemu_user:
            env["profile"] = "cross-qemu-user"
            env["notes"] = ("只有 qemu-aarch64 用户态模拟：可以跑功能测试，"
                            "性能数字不可信，不要作为门控依据")
        elif native_cc and env["host"]["arch"] in ("aarch64", "arm64"):
            env["profile"] = "native"
        elif chosen:
            env["profile"] = "compile-only"
            env["notes"] = "有交叉编译器但没有 QEMU，只能做静态改写（unverified）"
        else:
            env["profile"] = "none"
            env["notes"] = ("没有可用的 ARM 编译器；本次只能做方案设计，"
                            "所有结论标 unverified")

        # 用户选定的验证方式是否真的可行。选择权在用户，这里只回答"能不能"。
        if args.method != "auto":
            detail = (native_feasibility(env, tmp) if args.method == "native"
                      else qemu_feasibility(env, tmp))
            env["requested_method"] = args.method
            env["feasible"] = detail["feasible"]
            env["method_detail"] = detail
            if not detail["feasible"]:
                env["notes"] = (env["notes"] + "；" if env["notes"] else "") + \
                    "所选验证方式不可行: " + ",".join(detail["blocking_reasons"])
        else:
            env["requested_method"] = "auto"
            env["feasible"] = env["profile"] in ("native", "cross-qemu")

    ok = env["profile"] != "none"
    vl_ok = env["sve"].get("matches", True)
    if env["sve"] and env["sve"].get("matches") is False:
        env["notes"] = (env["notes"] + "；" if env["notes"] else "") + \
            "运行时 VL 与编译期 256 位不一致，所有向量访存会被截断，必须先修 ZCR/prctl"

    feasible = env.get("feasible", ok)
    payload = {"success": bool(feasible and vl_ok), "env": env,
               "error_message": "" if (feasible and vl_ok) else (
                   "vector_length_mismatch" if not vl_ok else
                   "requested_method_not_feasible" if not feasible else
                   "no_usable_toolchain")}

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out_path = os.path.realpath(os.path.expanduser(args.out))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    if not ok or not feasible:
        return 2
    return 0 if vl_ok else 1


if __name__ == "__main__":
    sys.exit(main())
