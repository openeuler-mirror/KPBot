#!/usr/bin/env python3
"""
微架构信息探测脚本
检测CPU微架构型号、流水线参数、向量单元和缓存信息
"""

import json
import subprocess
import os
import re
import platform

def detect_microarchitecture():
    """检测CPU微架构信息"""
    result = {
        "cpu_model": "unknown",
        "vendor_id": "unknown",
        "architecture": "unknown",
        "pipeline_width": 4,  # 默认值
        "vector_pipelines": 1,  # 默认值
        "vector_width_bits": 128,  # 默认值
        "vector_width_bytes": 16,
        "supports_avx": False,
        "supports_avx2": False,
        "supports_avx512": False,
        "supports_neon": False,
        "supports_sve": False,
        "supports_sse": False,
        "supports_sse2": False,
        "supports_sse3": False,
        "supports_sse4_1": False,
        "supports_sse4_2": False,
        "cache": {
            "l1_size_kb": 32,
            "l2_size_kb": 256,
            "l3_size_kb": 8192
        }
    }

    system = platform.system()

    if system == "Linux":
        result.update(detect_linux_cpu_info())
    elif system == "Darwin":
        result.update(detect_macos_cpu_info())
    else:
        result.update(detect_generic_cpu_info())

    # 根据CPU型号设置微架构参数
    result.update(set_microarchitecture_params(result))

    return result

def detect_linux_cpu_info():
    """检测Linux CPU信息"""
    info = {}

    try:
        with open('/proc/cpuinfo', 'r') as f:
            cpuinfo = f.read()

            # 提取vendor_id (x86) 或 CPU implementer (ARM)
            vendor_match = re.search(r'vendor_id\s*:\s*(.+)', cpuinfo)
            if vendor_match:
                info["vendor_id"] = vendor_match.group(1).strip()
            else:
                impl_match = re.search(r'CPU implementer\s*:\s*(.+)', cpuinfo)
                if impl_match:
                    impl = impl_match.group(1).strip()
                    impl_map = {"0x48": "HiSilicon", "0x41": "ARM", "0x42": "Broadcom",
                                "0x43": "Cavium", "0x46": "Fujitsu", "0x4e": "NVidia",
                                "0x50": "Ampere", "0x51": "Qualcomm", "0xc0": "Ampere"}
                    info["vendor_id"] = impl_map.get(impl, f"ARM-{impl}")

            # 提取model name (x86) 或 CPU part (ARM)
            model_match = re.search(r'model name\s*:\s*(.+)', cpuinfo)
            if model_match:
                info["cpu_model"] = model_match.group(1).strip()
            else:
                part_match = re.search(r'CPU part\s*:\s*(.+)', cpuinfo)
                if part_match:
                    part = part_match.group(1).strip()
                    part_map = {"0xd03": "Kunpeng-0xd03", "0xd0c": "Kunpeng-0xd0c",
                                "0xd01": "Kunpeng-0xd01", "0xd02": "Kunpeng-0xd02",
                                "0xd40": "Kunpeng-0xd40"}
                    info["cpu_model"] = part_map.get(part, f"ARM-{part}")

            # 提取CPU architecture (ARM)
            arch_match = re.search(r'CPU architecture\s*:\s*(\d+)', cpuinfo)
            if arch_match:
                arch_ver = arch_match.group(1).strip()
                if arch_ver == "8":
                    info["architecture"] = "aarch64"

            # 提取flags (x86) 或 Features (ARM)
            flags_match = re.search(r'flags\s*:\s*(.+)', cpuinfo)
            if not flags_match:
                flags_match = re.search(r'Features\s*:\s*(.+)', cpuinfo)

            if flags_match:
                flags = flags_match.group(1).lower()
                info["supports_sse"] = "sse" in flags
                info["supports_sse2"] = "sse2" in flags
                info["supports_sse3"] = "sse3" in flags
                info["supports_sse4_1"] = "sse4_1" in flags or "sse4.1" in flags
                info["supports_sse4_2"] = "sse4_2" in flags or "sse4.2" in flags
                info["supports_avx"] = "avx" in flags
                info["supports_avx2"] = "avx2" in flags
                info["supports_avx512"] = "avx512f" in flags
                info["supports_neon"] = "neon" in flags or "asimd" in flags
                info["supports_sve"] = "sve" in flags

            # 提取缓存信息
            cache_size_match = re.search(r'cache size\s*:\s*(.+)', cpuinfo)
            if cache_size_match:
                cache_size = cache_size_match.group(1).strip()
                # 解析缓存大小

    except Exception as e:
        print(f"Warning: Could not read /proc/cpuinfo: {e}")

    # 从sysfs读取缓存信息
    try:
        # L1缓存
        l1_path = "/sys/devices/system/cpu/cpu0/cache/index0/size"
        if os.path.exists(l1_path):
            with open(l1_path, 'r') as f:
                size_str = f.read().strip()
                info["cache"] = info.get("cache", {})
                info["cache"]["l1_size_kb"] = parse_cache_size(size_str)

        # L2缓存
        l2_path = "/sys/devices/system/cpu/cpu0/cache/index2/size"
        if os.path.exists(l2_path):
            with open(l2_path, 'r') as f:
                size_str = f.read().strip()
                info["cache"] = info.get("cache", {})
                info["cache"]["l2_size_kb"] = parse_cache_size(size_str)

        # L3缓存
        l3_path = "/sys/devices/system/cpu/cpu0/cache/index3/size"
        if os.path.exists(l3_path):
            with open(l3_path, 'r') as f:
                size_str = f.read().strip()
                info["cache"] = info.get("cache", {})
                info["cache"]["l3_size_kb"] = parse_cache_size(size_str)

    except Exception as e:
        print(f"Warning: Could not read cache info from sysfs: {e}")

    return info

def detect_macos_cpu_info():
    """检测macOS CPU信息"""
    info = {}

    try:
        # 获取CPU型号
        model = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'],
                                       text=True).strip()
        info["cpu_model"] = model

        # 获取vendor
        vendor = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.vendor'],
                                        text=True).strip()
        info["vendor_id"] = vendor

        # 获取CPU特性
        features = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.features'],
                                          text=True).strip().lower()
        info["supports_sse"] = "sse" in features
        info["supports_sse2"] = "sse2" in features
        info["supports_sse3"] = "sse3" in features
        info["supports_sse4_1"] = "sse4.1" in features
        info["supports_sse4_2"] = "sse4.2" in features
        info["supports_avx"] = "avx1.0" in features
        info["supports_avx2"] = "avx2.0" in features

        # 获取缓存信息
        l1_size = subprocess.check_output(['sysctl', '-n', 'hw.l1icachesize'],
                                          text=True).strip()
        info["cache"] = {
            "l1_size_kb": int(l1_size) // 1024,
            "l2_size_kb": 256,  # 默认值
            "l3_size_kb": 8192  # 默认值
        }

    except Exception as e:
        print(f"Warning: Could not detect macOS CPU info: {e}")

    return info

def detect_generic_cpu_info():
    """通用CPU信息检测"""
    info = {
        "cpu_model": platform.processor(),
        "architecture": platform.machine()
    }
    return info

def parse_cache_size(size_str):
    """解析缓存大小字符串，返回KB"""
    size_str = size_str.upper()
    if 'K' in size_str:
        return int(re.search(r'(\d+)', size_str).group(1))
    elif 'M' in size_str:
        return int(re.search(r'(\d+)', size_str).group(1)) * 1024
    elif 'G' in size_str:
        return int(re.search(r'(\d+)', size_str).group(1)) * 1024 * 1024
    else:
        return int(size_str) // 1024

def set_microarchitecture_params(info):
    """根据CPU型号设置微架构参数"""
    cpu_model = info.get("cpu_model", "").lower()
    vendor = info.get("vendor_id", "").lower()

    params = {}

    # Intel处理器
    if "intel" in vendor or "genuineintel" in vendor:
        if "skylake" in cpu_model:
            params.update({
                "pipeline_width": 6,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })
        elif "haswell" in cpu_model:
            params.update({
                "pipeline_width": 4,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })
        elif "broadwell" in cpu_model:
            params.update({
                "pipeline_width": 4,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })
        elif "cascadelake" in cpu_model or "icelake" in cpu_model:
            params.update({
                "pipeline_width": 6,
                "vector_pipelines": 2,
                "vector_width_bits": 512 if info.get("supports_avx512") else 256
            })
        elif "sapphirerapids" in cpu_model:
            params.update({
                "pipeline_width": 8,
                "vector_pipelines": 2,
                "vector_width_bits": 512 if info.get("supports_avx512") else 256
            })
        else:
            # 通用Intel处理器
            params.update({
                "pipeline_width": 4,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })

    # AMD处理器
    elif "amd" in vendor or "authenticamd" in vendor:
        if "zen" in cpu_model or "ryzen" in cpu_model:
            params.update({
                "pipeline_width": 6,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })
        elif "epyc" in cpu_model:
            params.update({
                "pipeline_width": 6,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })
        else:
            # 通用AMD处理器
            params.update({
                "pipeline_width": 4,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_avx2") else 128
            })

    # ARM / Kunpeng 处理器
    elif "arm" in vendor or "aarch64" in info.get("architecture", "") or "hisilicon" in vendor or "kunpeng" in cpu_model:
        if "kunpeng" in cpu_model:
            params.update({
                "pipeline_width": 4,
                "vector_pipelines": 2,
                "vector_width_bits": 256 if info.get("supports_sve") else 128
            })
        elif info.get("supports_sve"):
            params.update({
                "pipeline_width": 4,
                "vector_pipelines": 2,
                "vector_width_bits": 256
            })
        elif info.get("supports_neon"):
            params.update({
                "pipeline_width": 2,
                "vector_pipelines": 1,
                "vector_width_bits": 128
            })
        else:
            params.update({
                "pipeline_width": 1,
                "vector_pipelines": 1,
                "vector_width_bits": 64
            })

    # Apple Silicon
    elif "apple" in vendor:
        params.update({
            "pipeline_width": 6,
            "vector_pipelines": 2,
            "vector_width_bits": 256
        })

    # 更新向量宽度字节数
    if "vector_width_bits" in params:
        params["vector_width_bytes"] = params["vector_width_bits"] // 8

    return params

def main():
    """主函数"""
    arch_info = detect_microarchitecture()
    print(json.dumps(arch_info, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
