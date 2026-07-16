#!/usr/bin/env python3
"""
环境检测脚本
自动检测 CPU 架构、缓存层级、编译器、NUMA 配置
输出 JSON 格式，供 benchmark 和报告脚本使用
"""

import json
import subprocess
import sys
import os
import re
from typing import Optional, Dict, Any


def run_cmd(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def detect_cpu() -> Dict[str, str]:
    arch = run_cmd("uname -m")
    model = run_cmd("lscpu | grep '^Model name:' | cut -d: -f2 | sed 's/^ //' | tr -d '\\n'")
    if not model:
        model = run_cmd("cat /proc/cpuinfo | grep -i 'model name' | head -1 | cut -d: -f2 | sed 's/^ //'")
    return {
        "arch": arch,
        "model": model or "unknown",
        "vendor": detect_vendor(),
    }


def detect_vendor() -> str:
    # 从 lscpu 中提取，比 /proc/cpuinfo 更可靠
    model = run_cmd("lscpu | grep '^Model name:' | cut -d: -f2 | tr '[:upper:]' '[:lower:]' | tr -d '\\n'")
    if "intel" in model:
        return "Intel"
    elif "amd" in model or "epyc" in model:
        return "AMD"
    elif "kunpeng" in model:
        return "Huawei"
    elif "ampere" in model:
        return "Ampere"
    elif any(x in model for x in ["neoverse", "cortex", "taishan"]):
        return "ARM"
    return "unknown"


def detect_cache() -> Dict[str, str]:
    caches = {}
    output = run_cmd("lscpu")
    for line in output.splitlines():
        lower = line.strip().lower()
        # 匹配格式: "L1d cache:                       8 MiB (128 instances)"
        if "l1d" in lower and "cache" in lower:
            caches["L1d"] = extract_size(lower)
        elif "l1i" in lower and "cache" in lower:
            caches["L1i"] = extract_size(lower)
        elif "l2" in lower and "cache" in lower and "l1" not in lower:
            caches["L2"] = extract_size(lower)
        elif "l3" in lower and "cache" in lower:
            caches["L3"] = extract_size(lower)
    return caches


def extract_size(line: str) -> str:
    m = re.search(r'(\d+\.?\d*)\s*([KMGT]?i?B)', line, re.IGNORECASE)
    if m:
        num, unit = m.group(1), m.group(2)
        return f"{num} {unit}"
    return "unknown"


def detect_numa() -> Dict[str, Any]:
    numa_nodes = {}
    output = run_cmd("numactl --hardware 2>/dev/null") or run_cmd("lscpu")
    for line in output.splitlines():
        m = re.match(r'node\s+(\d+)\s+cpus:\s+(.+)', line)
        if m:
            numa_nodes[int(m.group(1))] = {
                "cpus": m.group(2).strip(),
            }
    return {"nodes": numa_nodes, "count": len(numa_nodes)}


def detect_compiler() -> Dict[str, str]:
    compilers = []
    for cc in ["g++", "clang++", "icpc"]:
        ver = run_cmd(f"{cc} --version | head -1")
        if ver:
            compilers.append({"name": cc, "version": ver})
    return {"available": compilers}


def detect_simd() -> Dict[str, bool]:
    flags = run_cmd("cat /proc/cpuinfo | grep -E '^Features|^flags' | head -1")
    flags_lower = flags.lower()
    return {
        # ARM (Kunpeng / aarch64)
        "asimd":  "asimd" in flags_lower,  # ARM SIMD (equivalent to NEON)
        "sve":    "sve" in flags_lower,    # ARM Scalable Vector Extension
        "fp16":   "fphp" in flags_lower,   # ARM half-precision
    }


def detect_os() -> Dict[str, str]:
    return {
        "os": run_cmd("uname -s"),
        "kernel": run_cmd("uname -r"),
        "nproc": run_cmd("nproc"),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="检测运行环境")
    parser.add_argument("--output", "-o", default="env.json", help="输出 JSON 文件")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印详细信息")
    args = parser.parse_args()

    env = {
        "platform": detect_os(),
        "cpu": detect_cpu(),
        "cache": detect_cache(),
        "numa": detect_numa(),
        "compiler": detect_compiler(),
        "simd": detect_simd(),
    }

    if args.verbose:
        print(json.dumps(env, indent=2, ensure_ascii=False))

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(env, f, indent=2, ensure_ascii=False)

    print(f"[OK] 环境信息已保存到 {args.output}")
    print(f"     架构: {env['cpu']['arch']}  ({env['cpu']['model']})")
    print(f"     缓存: L1d={env['cache'].get('L1d','?')}  "
          f"L2={env['cache'].get('L2','?')}  "
          f"L3={env['cache'].get('L3','?')}")
    print(f"     NUMA 节点: {env['numa']['count']}")
    print(f"     SIMD: {', '.join(k for k,v in env['simd'].items() if v)}")


if __name__ == "__main__":
    main()
